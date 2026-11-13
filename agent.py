"""One asynchronous HPoker player; reception stays live during model calls."""

import asyncio
import json
import random
import time
import uuid
import unicodedata
from collections import deque
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import websockets

from poker_math import card_text
from prompts import REFLECTION_SYSTEM, SOCIAL_SYSTEM, STYLES, SYSTEM, choose_style

# UTF-8 bytes bound token count conservatively and leave room within the configured 310K context.
CONTEXT_BYTES = 200_000
INPUT_BYTES = 240_000
THINKING_TOKENS = 512
DECISION_SECONDS = 14
REFLECTION_SECONDS = 90
REFLECTION_THINKING_TOKENS = 2048
SOCIAL_INTERVAL = 5
SOCIAL_WINDOW_SECONDS = 5
REFLECTION_INTERVAL = 10


def tool(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(required), "additionalProperties": False}}}


TOOLS = [
    tool("poker_action", "在当前轮次行动一次；amount 为本轮总目标金额，不是增量。",
         {"action": {"type": "string", "enum": ["CHECK", "CALL", "BET", "RAISE", "FOLD", "ALL_IN"]},
          "amount": {"type": "integer", "minimum": 0}, "reason": {"type": "string"},
          "plan": {"type": "string", "maxLength": 600, "description": "本手后续打法与桌上形象；发言只点评上一手或回应公开聊天，不参与当前手策略，省略保持共同计划。"}}, ("action", "reason")),
    tool("get_players", "取得当前玩家的累积画像与可见牌桌信息。", {}),
    tool("remember_note", "持久保存或更新观察；subject 为当前玩家 ID 或 self。单主题覆盖旧笔记。",
         {"subject": {"type": "string"}, "note": {"type": "string", "maxLength": 400}}, ("subject", "note")),
    tool("recall_memory", "回忆当前玩家或自己的持久笔记。", {}),
    tool("table_action", "在对应阶段选择 RIT、补码或退出；wait 表示保持现状。",
         {"action": {"type": "string", "enum": ["rit", "rebuy", "leave", "wait"]},
          "choice": {"type": "integer", "enum": [1, 2]}, "reason": {"type": "string"}}, ("action", "reason")),
]

REFLECTION_TOOL = tool("finish_reflection", "保存复盘结论、自主更新或删除记忆。",
    {"summary": {"type": "string", "maxLength": 1500},
     "plan": {"type": "string", "maxLength": 600, "description": "本手对局与桌上形象的共同计划；发言不参与当前手策略，省略保持原计划，空串清除。"},
     "updates": {"type": "array", "maxItems": 10, "items": {
         "type": "object", "properties": {"subject": {"type": "string"},
                                          "note": {"type": "string", "maxLength": 400}},
         "required": ["subject", "note"], "additionalProperties": False}},
     "delete_subjects": {"type": "array", "maxItems": 10, "items": {"type": "string"}}},
    ("summary", "updates", "delete_subjects"))

SOCIAL_TOOLS = [
    tool("send_text", "发送一句随意的纯文字聊天：开局点评上一手，或回应其他玩家的公开发言。不得讨论当前手牌或行动。",
         {"message": {"type": "string", "maxLength": 120}, "reason": {"type": "string", "maxLength": 240}},
         ("message", "reason")),
]


def validated_text(message):
    if not isinstance(message, str):
        raise ValueError("聊天必须是文字")
    message = " ".join(message.split())
    if not 1 <= len(message) <= 120:
        raise ValueError("聊天应为 1–120 字符")
    if any(unicodedata.category(c) == "So" or c in "\ufe0f\u20e3" or 0x1F3FB <= ord(c) <= 0x1F3FF for c in message):
        raise ValueError("聊天仅允许纯文字，不允许表情字符")
    return message


def validated_action(arguments, legal, small_blind):
    action = arguments.get("action")
    if action not in ("CHECK", "CALL", "BET", "RAISE", "FOLD", "ALL_IN"):
        raise ValueError("未知操作")
    if not legal.get("can_" + action.lower()):
        raise ValueError("当前操作不合法")
    amount = 0
    if action in ("BET", "RAISE"):
        amount = arguments.get("amount")
        if type(amount) is not int:
            raise ValueError("下注目标必须是整数")
        low = legal["min_bet" if action == "BET" else "min_raise_to"]
        high = legal["max_bet" if action == "BET" else "max_raise_to"]
        if not low <= amount <= high or (amount != high and amount % small_blind):
            raise ValueError(f"目标必须在 {low}–{high} 且按小盲 {small_blind} 步进；全下用 ALL_IN")
    elif action == "ALL_IN":
        amount = legal.get("all_in_amount", 0)
    return {"action": action, "amount": amount}


class PokerAgent:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store
        self.http = httpx.AsyncClient(trust_env=False, timeout=15)
        self.poker_http = httpx.AsyncClient(proxy=settings["proxy_url"], trust_env=False, timeout=15)
        self.user = None
        self.token = None
        self.room_id = None
        self.room = {}
        self.ws = None
        self.running = False
        self.stopping = False
        self.status = "未登录"
        self.error = None
        self.session = ""
        self.receiver = self.decision = self.departure = None
        self.reflection = None
        self.social_decision = None
        self.social_status = "待命"
        self.social_wake = asyncio.Event()
        self.social_context = None
        self.social_background = {}
        self.pending_social_chat = None
        self.social_started = 0
        self.shared_plan = None
        self.coordination_version = 0
        self.agent_activity = deque(maxlen=20)
        self.reflection_status = "待命"
        self.last_reflection = None
        self.last_social_decision = None
        self.initial_state = asyncio.Event()
        self.snapshot_at = time.monotonic()
        self.actions = []
        self.hand_decisions = []
        self.last_history = []
        self.hand_number = None
        self.complete_hand = False
        self.hand_style = "balanced"
        self.completed_keys = set()
        self.chats = deque(maxlen=80)
        self.last_social = 0
        self.analysis = None
        self.last_decision = None
        self.metrics = {"model_calls": 0, "decisions": 0, "fallbacks": 0,
                        "hands": 0, "net_chips": 0, "input_chars": 0, "input_tokens": None}
        self.session_tokens = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                               "reasoning_tokens": 0, "total_tokens": 0}
        self.hand_tokens = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                            "reasoning_tokens": 0, "total_tokens": 0}
        self.historical_tokens = self.store.token_summary()
        self.profiles = self.store.profiles(self.settings["server_url"])

    def record_usage(self, usage):
        if not usage:
            return
        p = usage.get("prompt_tokens") or 0
        c = usage.get("completion_tokens") or 0
        r = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        t = usage.get("total_tokens") or (p + c)
        for target in (self.session_tokens, self.hand_tokens, self.historical_tokens):
            target["calls"] += 1
            target["prompt_tokens"] += p
            target["completion_tokens"] += c
            target["reasoning_tokens"] += r
            target["total_tokens"] += t

    def log(self, kind, **data):
        self.store.event(self.session, kind, data)
        roles = {"decision": "decision", "fallback": "decision", "table_decision": "decision",
                 "social_decision": "social", "reflection": "reflection"}
        if kind not in roles or (kind == "social_decision" and
                not any(r.get("submitted") for r in data.get("results", {}).values())):
            return
        self.agent_activity.append({"agent": roles[kind], "kind": kind, "timestamp": time.time(),
            **{k: data[k] for k in ("hand", "commented_hand", "trigger", "street", "turn_count", "action", "amount", "reason", "message", "show", "results", "summary", "plan", "plan_applied")
               if k in data}})
        # Chat and long-term notes are read next time; they must not restart a pending betting request.
        if kind in ("decision", "fallback", "table_decision") or data.get("plan_applied"):
            self.coordination_version += 1

    async def api(self, method, path, **kwargs):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        response = await self.poker_http.request(method, self.settings["server_url"] + path, headers=headers, **kwargs)
        if response.is_error:
            try:
                detail = response.json().get("detail", "请求失败")
            except ValueError:
                detail = "远端服务未返回 JSON"
            raise RemoteError(str(detail)[:300], response.status_code)
        return response.json()

    async def login(self):
        result = await self.api("POST", "/api/auth/login", json={
            "username": self.settings["username"], "password": self.settings["password"]})
        self.token, self.user = result["token"], result["user"]
        self.status, self.error = "已登录 · 待命", None
        return self.user

    def me(self):
        return next((p for p in self.room.get("table", {}).get("seats", [])
                     if p and self.user and p["player_id"] == self.user["user_id"]), None)

    def key(self):
        table = self.room.get("table", {})
        me = self.me()
        if not me or self.room.get("is_ended"):
            return None
        street = table.get("street")
        number = table.get("hand_number", 0)
        if street == "RIT_DECISION" and me["player_id"] in table.get("rit_voters", []) \
                and me["player_id"] not in table.get("rit_votes", {}):
            return (number, "rit")
        if street in ("IDLE", "HAND_END"):
            return (number, "between") if me["chips"] == 0 else None
        if not me.get("is_folded") and not me.get("is_all_in") and table.get("current_turn_seat") == me["seat_index"] and any(
                v for k, v in (table.get("legal_actions") or {}).items() if k.startswith("can_")):
            return (number, street, table.get("turn_count"))
        return None

    async def start(self, room_id):
        if self.running or self.stopping or self.room_id:
            raise ValueError("请先停止当前对局并完成离座")
        if not self.token:
            await self.login()
        snapshot = await self.api("GET", "/api/rooms/" + quote(room_id, safe=""))
        if snapshot.get("is_ended"):
            raise ValueError("房间已结束")
        self.session = uuid.uuid4().hex
        self.room_id, self.room = room_id, {}
        self.actions, self.last_history = [], []
        self.hand_decisions = []
        self.hand_number = None
        self.completed_keys.clear()
        self.chats.clear()
        self.analysis = self.last_decision = None
        self.last_reflection = None
        self.last_social_decision = None
        self.social_status = "待命"
        self.social_wake.clear()
        self.social_context = None
        self.social_background = {}
        self.pending_social_chat = None
        self.social_started = 0
        self.shared_plan = None
        self.coordination_version += 1
        self.agent_activity.clear()
        self.reflection_status = "待命"
        self.metrics = dict.fromkeys(self.metrics, 0)
        self.session_tokens = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                               "reasoning_tokens": 0, "total_tokens": 0}
        self.hand_tokens = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                            "reasoning_tokens": 0, "total_tokens": 0}
        self.error = None
        self.initial_state.clear()
        self.running, self.status = True, "连接房间"
        self.receiver = asyncio.create_task(self.receive())
        try:
            await asyncio.wait_for(self.initial_state.wait(), 10)
            if not self.me():
                raise ValueError(self.error or "未能入座：房间可能已满")
        except BaseException:
            await self.stop()
            raise
        self.log("session_start", room_id=room_id, style=self.settings["style"],
                 username=self.user["username"])
        self.start_background()

    def start_background(self):
        if not self.reflection or self.reflection.done():
            self.reflection = asyncio.create_task(self.reflect())
        if not self.social_decision or self.social_decision.done():
            self.social_decision = asyncio.create_task(self.social_loop())

    async def send(self, event, payload=None):
        if not self.ws:
            raise ConnectionError("牌桌连接已断开")
        await self.ws.send(json.dumps({"event": event, "payload": payload or {}}, ensure_ascii=False))

    async def receive(self):
        parsed = urlsplit(self.settings["server_url"])
        path = parsed.path.rstrip("/") + f"/ws/{quote(self.room_id, safe='')}/{quote(self.user['user_id'], safe='')}"
        url = urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc, path, "", ""))
        failures = 0
        while self.running or self.stopping:
            try:
                async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {self.token}"},
                                              proxy=self.settings["proxy_url"], ping_interval=15, ping_timeout=15,
                                              max_size=2_000_000) as ws:
                    self.ws = ws
                    self.status = "等待离座" if self.stopping else "观察牌桌"
                    async for raw in ws:
                        message = json.loads(raw)
                        event, payload = message.get("event"), message.get("payload", {})
                        if event == "ROOM_STATE":
                            failures = 0
                            self.observe(payload)
                            self.initial_state.set()
                            if self.stopping:
                                table = payload["table"]
                                if table.get("street") == "RIT_DECISION" and self.user["user_id"] in table.get("rit_voters", []) \
                                        and self.user["user_id"] not in table.get("rit_votes", {}):
                                    await self.send("RIT_CHOICE", {"choice": 1})
                            else:
                                await self.advance()
                        elif event in ("CHAT_MESSAGE", "EMOJI_REACTION"):
                            self.observe_chat(event, payload)
                        elif event == "ERROR_MESSAGE":
                            self.error = str(payload.get("message", "服务器拒绝操作"))[:300]
                            self.log("error", message=self.error)
                        elif event in ("ROOM_DELETED", "PLAYER_KICKED", "SETTLEMENT_REPORT"):
                            if event == "PLAYER_KICKED" and payload.get("player_id") != self.user["user_id"]:
                                continue
                            self.log("room_end", event=event, payload=payload)
                            asyncio.create_task(self.stop())
                            return
                    if self.running or self.stopping:
                        raise ConnectionError("牌桌连接被关闭")
            except asyncio.CancelledError:
                return
            except Exception as exc:
                failures += 1
                self.complete_hand = False
                self.completed_keys.discard(self.key())
                await self.cancel_decision()
                self.error = f"牌桌断线（{type(exc).__name__}），正在重连"
                self.log("connection", message=self.error, attempt=failures)
                self.status = "重连中"
                if failures >= 3:
                    self.initial_state.set()
                    asyncio.create_task(self.stop())
                    return
                await asyncio.sleep(failures)
            finally:
                self.ws = None

    def observe_chat(self, event, payload):
        chat = {k: payload[k] for k in ("player_id", "name", "message", "emoji") if k in payload}
        chat["hand"] = self.hand_number
        self.chats.append(chat)
        self.log("chat", **chat)
        if event == "CHAT_MESSAGE" and self.running and self.settings["social"] and self.me() and \
                not self.room.get("is_ended") and chat.get("player_id") and \
                chat["player_id"] != self.user["user_id"] and \
                isinstance(chat.get("message"), str) and chat["message"].strip():
            # Keep the latest prompt; recent public chat retains intervening messages.
            self.pending_social_chat = {k: chat[k] for k in ("player_id", "name", "message") if k in chat}
            self.social_wake.set()

    def observe(self, room):
        previous = self.room.get("table", {})
        self.snapshot_at = time.monotonic()
        self.room = room
        table = room["table"]
        if any(previous.get(k) != table.get(k) for k in ("street", "turn_count", "board_cards")):
            self.analysis = None
        number = table.get("hand_number", 0)
        if number != self.hand_number:
            self.social_context = None
            # Freeze historical notes before this hand's reflection can write private tactics.
            self.social_background = {"memory_notes": self.store.memories(
                self.settings["server_url"], self.settings["username"]), "profiles": self.profiles}
            self.social_started = time.monotonic()
            if previous.get("street") == "HAND_END" and previous.get("hand_number") == number - 1 and \
                    table.get("street") == "PREFLOP" and all(
                        a["action"] in ("POST_SB", "POST_BB") for a in table.get("action_history", [])):
                # Only public facts from the finished hand reach the speaking model.
                self.social_context = {"hero_id": self.user["user_id"], "previous_hand": {
                    "hand": number - 1, "complete_observation": self.complete_hand,
                    "board": [card_text(c) for c in previous.get("board_cards") or []],
                    "board_2": [card_text(c) for c in previous.get("board_cards_2") or []],
                    "players": [{"player_id": p["player_id"], "name": p["name"],
                                 "shown_cards": [card_text(c) for c in p.get("shown_cards") or []]}
                                for p in previous.get("seats", []) if p],
                    "actions": [{k: a[k] for k in ("player_id", "action", "amount", "street")} for a in self.actions],
                    "results": [{k: r.get(k) for k in ("player_id", "name", "net_profit")}
                                for r in previous.get("hand_results", [])]},
                    "previous_chat": [c for c in list(self.chats)[-30:] if c.get("hand") == number - 1]}
                self.social_wake.set()
            self.hand_number = number
            self.actions, self.last_history = [], []
            self.hand_decisions = []
            self.hand_tokens = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                "reasoning_tokens": 0, "total_tokens": 0}
            self.completed_keys.clear()
            self.analysis = None
            self.shared_plan = None
            self.coordination_version += 1
            self.complete_hand = table.get("street") == "PREFLOP" and all(
                a.get("action") in ("POST_SB", "POST_BB") for a in table.get("action_history", []))
            self.hand_style = choose_style(self.settings["style"], self.settings["mix_weights"])
            self.log("hand_start", room_id=self.room_id, hand=number, style=self.hand_style,
                     seats=[{k: p.get(k) for k in ("player_id", "name", "seat_index", "chips")}
                            for p in table.get("seats", []) if p],
                     hole_cards=(self.me() or {}).get("hole_cards", []),
                     dealer=table.get("dealer_seat"), complete_observation=self.complete_hand)
        history = table.get("action_history", [])
        overlap = 0
        for size in range(min(len(self.last_history), len(history)), 0, -1):
            if self.last_history[-size:] == history[:size]:
                overlap = size
                break
        for item in history[overlap:]:
            action = dict(item)
            prior = [a["amount"] for a in self.actions if a["street"] == action["street"]
                     and a["action"] in ("BET", "RAISE", "ALL_IN")]
            high = max(prior, default=table.get("big_blind", 0) if action["street"] == "PREFLOP" else 0)
            action["aggressive"] = action["action"] in ("BET", "RAISE") or (
                action["action"] == "ALL_IN" and action["amount"] > high)
            action["timestamp"] = time.time()
            self.actions.append(action)
            self.log("action", room_id=self.room_id, hand=number, **action)
        self.last_history = history
        if previous.get("street") != table.get("street") or previous.get("board_cards") != table.get("board_cards"):
            self.log("street", hand=number, street=table.get("street"),
                     board=table.get("board_cards"), total_pot=table.get("total_pot"))
        end_key = (number, "recorded")
        if table.get("street") == "HAND_END" and end_key not in self.completed_keys:
            self.completed_keys.add(end_key)
            self.store.record_hand(self.settings["server_url"], self.room_id, table, self.actions, self.complete_hand)
            self.profiles = self.store.profiles(self.settings["server_url"])
            self.metrics["hands"] += 1
            result = next((r for r in table.get("hand_results", []) if r["player_id"] == self.user["user_id"]), {})
            self.metrics["net_chips"] += result.get("net_profit", 0)
            self.log("hand_end", hand=number, results=table.get("hand_results", []),
                     total_pot=table.get("total_pot"))

    async def cancel_decision(self):
        if self.decision and not self.decision.done() and self.decision is not asyncio.current_task():
            self.decision.cancel()
            await asyncio.gather(self.decision, return_exceptions=True)
        self.decision = None

    async def advance(self):
        if not self.running:
            return
        if self.room.get("is_ended"):
            asyncio.create_task(self.stop())
            return
        me = self.me()
        if not me:
            self.error = "账户未在座位上"
            return
        key = self.key()
        table = self.room["table"]
        if self.decision and not self.decision.done():
            if getattr(self.decision, "turn_key", None) == key:
                return
            await self.cancel_decision()
        table = self.room["table"]
        if table["street"] in ("IDLE", "HAND_END") and me["chips"] > 0:
            await self.ready_if_allowed()
            start_key = (table["hand_number"], "start")
            playable = [p for p in table["seats"] if p and p["chips"] > 0 and not p.get("is_sitting_out")]
            if table["street"] == "IDLE" and self.room["host_player_id"] == me["player_id"] \
                    and len(playable) >= 2 and start_key not in self.completed_keys:
                self.completed_keys.add(start_key)
                await self.send("START_GAME")
                return
        if key and key not in self.completed_keys:
            self.completed_keys.add(key)
            self.decision = asyncio.create_task(self.decide(key))
            self.decision.turn_key = key
        elif not self.decision or self.decision.done():
            self.status = "观察牌桌"

    async def ready_if_allowed(self):
        table, me = self.room.get("table", {}), self.me()
        if not self.running or not me or me["chips"] <= 0 or table.get("street") not in ("IDLE", "HAND_END"):
            return
        ready_key = (self.hand_number, "ready")
        if me["player_id"] not in table.get("ready_player_ids", []) and ready_key not in self.completed_keys:
            self.completed_keys.add(ready_key)
            try:
                await self.send("PLAYER_READY", {"ready": True})
            except Exception:
                self.completed_keys.discard(ready_key)
                raise

    async def social_loop(self):
        while self.running:
            self.social_wake.clear()
            if self.settings["social"] and self.ws and self.me() and not self.room.get("is_ended"):
                try:
                    await self.choose_social("chat" if self.pending_social_chat else "new_hand")
                except Exception as exc:
                    self.log("social_error", message=f"发言任务失败（{type(exc).__name__}），等待下次触发")
            self.social_status = "等待新一手或聊天" if self.settings["social"] else "已关闭"
            try:
                await asyncio.wait_for(self.social_wake.wait(), SOCIAL_INTERVAL)
            except TimeoutError:
                pass
        self.social_status = "已停止"

    def social_window_open(self, hand):
        return bool(self.running and self.settings["social"] and self.social_context and self.me() and
                    not self.room.get("is_ended") and hand == self.hand_number and
                    self.room["table"]["street"] == "PREFLOP" and not self.hand_decisions and
                    time.monotonic() - self.social_started < SOCIAL_WINDOW_SECONDS and
                    all(a["action"] in ("POST_SB", "POST_BB") for a in self.actions))

    async def choose_social(self, trigger):
        if not self.running or not self.settings["social"] or not self.me() or self.room.get("is_ended"):
            return
        if trigger == "chat":
            if not self.pending_social_chat:
                return
            incoming = self.pending_social_chat
            self.pending_social_chat = None
            roster = {p["player_id"]: p["name"] for p in self.room["table"]["seats"] if p}
            roster.setdefault(incoming["player_id"], incoming.get("name", ""))
            profiles = self.social_background.get("profiles", {})
            context = {"hero_id": self.user["user_id"], "incoming_chat": incoming,
                "recent_chat": [{k: c[k] for k in ("player_id", "name", "message") if k in c}
                                for c in list(self.chats)[-30:] if isinstance(c.get("message"), str) and c["message"].strip()],
                "players": [{"player_id": player, "name": name,
                    "profile": {k: profiles.get(player, {}).get(k) for k in (
                        "observed_hands", "complete_hands", "vpip_pct", "pfr_pct", "aggression_factor", "style")}}
                    for player, name in roster.items()],
                "memory_notes": [{"subject": n["subject"], "note": n["note"]}
                                 for n in self.social_background.get("memory_notes", [])
                                 if n["subject"] == "self" or n["subject"] in roster]}
        elif trigger == "new_hand":
            if not self.social_window_open(self.hand_number) or (self.hand_number, "social_requested") in self.completed_keys:
                return
            self.completed_keys.add((self.hand_number, "social_requested"))
            context = json.loads(json.dumps(self.social_context, ensure_ascii=False))
        else:
            raise ValueError("未知发言触发点")
        started = time.monotonic()
        room_id, hand = self.room_id, self.hand_number
        self.social_status = "回应聊天" if trigger == "chat" else "点评上一手"
        topic = {"trigger": trigger}
        if trigger == "new_hand":
            topic["commented_hand"] = context["previous_hand"]["hand"]
        try:
            context["trigger"] = trigger
            context["chat_cooldown_seconds"] = 0 if trigger == "chat" else max(0, round(25 - (time.monotonic() - self.last_social), 1))
            while len(json.dumps(context, ensure_ascii=False).encode("utf-8")) > CONTEXT_BYTES:
                chat_key = "recent_chat" if trigger == "chat" else "previous_chat"
                if context[chat_key]:
                    context[chat_key].pop(0)
                elif context.get("previous_hand", {}).get("actions"):
                    context["previous_hand"]["actions"].pop(0)
                    context["previous_hand"]["omitted_older_actions"] = context["previous_hand"].get("omitted_older_actions", 0) + 1
                elif context.get("memory_notes"):
                    context["memory_notes"].pop()
                else:
                    raise ValueError("公开发言输入超过预算")
            timeout = 4.2 if trigger == "chat" else min(4.2, SOCIAL_WINDOW_SECONDS - (started - self.social_started))
            async with asyncio.timeout(timeout):
                self.metrics["model_calls"] += 1
                response = await self.http.post(self.settings["model_url"] + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.settings["api_key"]}, timeout=timeout,
                    json={"model": self.settings["model"], "messages": [
                        {"role": "system", "content": SOCIAL_SYSTEM + "\n说话偏好（不能改变发言范围）：" + self.settings["custom_prompt"]},
                        {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                        "tools": SOCIAL_TOOLS, "tool_choice": "auto", "temperature": .7,
                        "max_tokens": 512, "reasoning_effort": "low", "thinking_token_budget": 64})
                response.raise_for_status()
                data = response.json()
                calls = data["choices"][0]["message"].get("tool_calls") or []
                usage = data.get("usage") or {}
                self.record_usage(usage)
                self.log("model_call", agent="social", hand=hand, phase=trigger, round=1,
                         elapsed=round(time.monotonic() - started, 2), prompt_tokens=usage.get("prompt_tokens"),
                         completion_tokens=usage.get("completion_tokens"),
                         reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                         finish=data["choices"][0].get("finish_reason"), tools=[c["function"]["name"] for c in calls])
                if not calls:
                    if data["choices"][0].get("finish_reason") == "length":
                        raise ValueError("社交输出被截断")
                    self.last_social_decision = {"hand": hand, **topic,
                        "show": "none", "results": {},
                        "reason": (data["choices"][0]["message"].get("content") or "选择保持安静")[:240],
                        "elapsed": round(time.monotonic() - started, 2)}
                    self.log("social_decision", **self.last_social_decision)
                if len(calls) > 1:
                    raise ValueError("一次发言最多发送一句话")
                for call in calls:
                    if trigger == "new_hand" and not self.social_window_open(hand):
                        raise ValueError("新一手开局发言窗口已结束，丢弃评论")
                    name = call["function"]["name"]
                    args = json.loads(call["function"]["arguments"])
                    if not isinstance(args, dict):
                        raise ValueError("工具参数必须是对象")
                    if name != "send_text":
                        raise ValueError("发言 agent 只允许纯文字聊天，不允许亮牌或其他操作")
                    await self.apply_social(args, room_id, hand, started, trigger)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = str(exc)[:160] if isinstance(exc, (ValueError, KeyError, TypeError)) else ""
            self.log("social_error", hand=hand, **topic, message=f"即时社交未执行（{type(exc).__name__}）{detail}")
        finally:
            self.social_status = "等待新一手或聊天" if self.running else "已停止"

    async def apply_social(self, args, room_id, hand, started, trigger="new_hand"):
        if not isinstance(args, dict) or set(args) - {"reason", "message"}:
            raise ValueError("发言 agent 只允许纯文字评论，不允许亮牌或其他参数")
        reason = args.get("reason")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 240:
            raise ValueError("社交理由须为 1–240 字符")
        social = {"message": validated_text(args.get("message"))}
        if room_id != self.room_id or not self.running or not self.settings["social"] or \
                not self.me() or self.room.get("is_ended"):
            return
        if trigger == "new_hand":
            if not self.social_window_open(hand):
                return
        elif trigger != "chat":
            raise ValueError("未知发言触发点")
        if trigger == "new_hand" and time.monotonic() - self.last_social < 25:
            result = {"error": "社交冷却中"}
        else:
            await self.send("CHAT_MESSAGE", social)
            self.last_social = time.monotonic()
            result = {"submitted": True}
        self.last_social_decision = {"hand": hand, "trigger": trigger,
                                    **({"commented_hand": hand - 1} if trigger == "new_hand" else {}),
                                    "reason": reason.strip(), "show": "none", **social,
                                    "results": {"communication": result}, "elapsed": round(time.monotonic() - started, 2)}
        self.log("social_decision", **self.last_social_decision)

    async def reflect(self):
        while self.running:
            if not self.ws or not self.me() or self.room.get("is_ended"):
                await asyncio.sleep(REFLECTION_INTERVAL)
                continue
            job = {"trigger": "loop", "room_id": self.room_id, "hand": self.hand_number, "style": self.hand_style,
                   "version": self.coordination_version, "context": json.loads(json.dumps(self.context(), ensure_ascii=False))}
            self.reflection_status = f"第 {job['hand']} 手 · 后台反思中"
            started = time.monotonic()
            self.log("reflection_start", hand=job["hand"], trigger=job["trigger"], room_id=job["room_id"])
            try:
                async with asyncio.timeout(REFLECTION_SECONDS):
                    notes = self.store.memories(self.settings["server_url"], self.settings["username"])
                    payload = {"trigger": job["trigger"], "reflection": job["context"],
                               "memory_notes": notes,
                               "recent_hand_details": self.store.hand_details(self.settings["server_url"], self.user["user_id"]),
                               "agent_activity": self.store.session_activity(self.session)}
                    # Old decision audits contain client calculations; keep only their actions and reasons.
                    for record in payload["agent_activity"]:
                        record["data"].pop("betting", None)
                    payload["reflection"].pop("memory_notes", None)
                    # Trim older evidence without deleting the stored records.
                    while len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > CONTEXT_BYTES:
                        if payload["recent_hand_details"]:
                            payload["recent_hand_details"].pop()
                        elif payload["agent_activity"]:
                            payload["agent_activity"].pop(0)
                        elif notes:
                            notes.pop()
                        else:
                            break
                        payload["omitted_older_reflection_records"] = payload.get("omitted_older_reflection_records", 0) + 1
                    messages = [{"role": "system", "content": REFLECTION_SYSTEM +
                                 "\n本手风格：" + STYLES[job["style"]]["prompt"] +
                                 "\n用户偏好：" + self.settings["custom_prompt"]},
                                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
                    for attempt in range(2):
                        self.metrics["model_calls"] += 1
                        call_started = time.monotonic()
                        response = await self.http.post(self.settings["model_url"] + "/chat/completions",
                            headers={"Authorization": "Bearer " + self.settings["api_key"]},
                            timeout=REFLECTION_SECONDS,
                            json={"model": self.settings["model"], "messages": messages,
                                  "tools": [REFLECTION_TOOL], "tool_choice": "auto", "temperature": .7,
                                  "max_tokens": 4096, "reasoning_effort": "high",
                                  "thinking_token_budget": REFLECTION_THINKING_TOKENS})
                        response.raise_for_status()
                        data = response.json()
                        choice = data["choices"][0]
                        usage = data.get("usage") or {}
                        self.record_usage(usage)
                        message = choice["message"]
                        calls = message.get("tool_calls") or []
                        self.log("model_call", agent="reflection", hand=job["hand"], phase=job["trigger"],
                                 round=attempt + 1, elapsed=round(time.monotonic() - call_started, 2),
                                 prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
                                 reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                                 finish=choice.get("finish_reason"), tools=[c["function"]["name"] for c in calls])
                        if not calls:
                            messages.append({"role": "user", "content": "请调用 finish_reflection 保存复盘结论及选择的记忆维护，不要只输出文字。"})
                            continue
                        if len(calls) != 1 or calls[0]["function"]["name"] != "finish_reflection":
                            raise ValueError("反思 agent 只能调用 finish_reflection")
                        call = calls[0]
                        try:
                            args = json.loads(call["function"]["arguments"])
                            await self.finish_reflection(args, job, started)
                            break
                        except (ValueError, KeyError, TypeError) as exc:
                            messages.extend([{"role": "assistant", "content": message.get("content"), "tool_calls": calls},
                                             {"role": "tool", "tool_call_id": call["id"],
                                              "content": json.dumps({"error": str(exc)[:200]}, ensure_ascii=False)}])
                    else:
                        raise ValueError("未能保存有效复盘")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log("reflection_error", hand=job["hand"], trigger=job["trigger"],
                         message=f"后台反思失败（{type(exc).__name__}），已有记忆保留",
                         elapsed=round(time.monotonic() - started, 2))
            self.reflection_status = "循环等待" if self.running else "已停止"
            await asyncio.sleep(REFLECTION_INTERVAL)
        self.reflection_status = "待命" if self.running else "已停止"

    async def finish_reflection(self, args, job, started):
        if not isinstance(args, dict):
            raise ValueError("反思结果必须是对象")
        if set(args) - {"summary", "updates", "delete_subjects", "plan"}:
            raise ValueError("反思 agent 只维护摘要和记忆，交流由社交 agent 执行")
        summary, updates, deletions = args.get("summary"), args.get("updates"), args.get("delete_subjects")
        if not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 1500:
            raise ValueError("复盘摘要须为 1–1500 字符")
        plan = args.get("plan")
        if "plan" in args and (not isinstance(plan, str) or len(plan.strip()) > 600):
            raise ValueError("共同计划须为最多 600 字符的文字")
        if not isinstance(updates, list) or not isinstance(deletions, list) or max(len(updates), len(deletions)) > 10:
            raise ValueError("记忆更新和清理须为数组，各最多 10 条")
        old_notes = self.store.memories(self.settings["server_url"], self.settings["username"])
        known = {n["subject"] for n in old_notes}
        allowed = {"self", *known, *(p["player_id"] for p in job["context"]["players"])}
        subjects = []
        for item in updates:
            if not isinstance(item, dict) or not isinstance(item.get("subject"), str) or item["subject"] not in allowed:
                raise ValueError("只能更新自己的或已知玩家的记忆")
            if not isinstance(item.get("note"), str) or not 1 <= len(item["note"].strip()) <= 400:
                raise ValueError("记忆笔记须为 1–400 字符")
            item["note"] = item["note"].strip()
            subjects.append(item["subject"])
        if any(not isinstance(s, str) or s not in known for s in deletions):
            raise ValueError("只能清理本账号已有记忆")
        if len(set(subjects)) != len(subjects) or len(set(deletions)) != len(deletions) or set(subjects) & set(deletions):
            raise ValueError("同一主题不能重复更新或同时更新与删除")
        if not self.running:
            return
        self.store.revise_memories(self.settings["server_url"], self.settings["username"], updates, deletions)
        plan_applied = plan is not None and job["room_id"] == self.room_id and job["hand"] == self.hand_number and \
            job["context"]["table"]["street"] == self.room["table"]["street"] and \
            job["context"]["table"].get("turn_count") == self.room["table"].get("turn_count") and \
            job.get("version") == self.coordination_version
        if plan_applied:
            self.shared_plan = {"hand": job["hand"], "street": self.room["table"]["street"],
                                "source": "reflection", "plan": plan.strip()} if plan.strip() else None
        self.last_reflection = {"hand": job["hand"], "room_id": job["room_id"], "trigger": job["trigger"],
                                "summary": summary.strip(), "updates": updates, "delete_subjects": deletions,
                                "plan": plan, "plan_applied": plan_applied,
                                "previous_notes": [n for n in old_notes if n["subject"] in set(subjects + deletions)],
                                "elapsed": round(time.monotonic() - started, 2)}
        self.log("reflection", **self.last_reflection)

    def players(self):
        result = []
        table = self.room.get("table", {})
        seated = [p for p in table.get("seats", []) if p]
        order = sorted((p["seat_index"] for p in seated),
                       key=lambda seat: (seat - (table.get("bb_seat") or 0)) % max(1, len(table.get("seats", []))))
        early = [seat for seat in order if seat not in (table.get("dealer_seat"), table.get("sb_seat"), table.get("bb_seat"))]
        labels = {1: ["UTG"], 2: ["UTG", "CO"], 3: ["UTG", "HJ", "CO"],
                  4: ["UTG", "MP", "HJ", "CO"], 5: ["UTG", "UTG+1", "MP", "HJ", "CO"],
                  6: ["UTG", "UTG+1", "UTG+2", "MP", "HJ", "CO"]}
        positions = dict(zip(early, labels.get(len(early), [])))
        for player in table.get("seats", []):
            if not player:
                continue
            p = {k: player.get(k) for k in ("player_id", "name", "seat_index", "chips", "is_folded", "is_all_in",
                                          "current_round_bet", "last_action", "vpip", "hands_played", "shown_cards")}
            stats = self.profiles.get(player["player_id"], {})
            p["observed"] = {k: stats.get(k) for k in ("observed_hands", "complete_hands", "vpip_pct", "pfr_pct",
                                                     "aggression_factor", "aggressive", "passive", "folds", "style", "net_chips")}
            p["position"] = "BTN" if player["seat_index"] == table.get("dealer_seat") else (
                "SB" if player["seat_index"] == table.get("sb_seat") else (
                    "BB" if player["seat_index"] == table.get("bb_seat") else positions.get(player["seat_index"], "其他位置")))
            if player["seat_index"] == table.get("dealer_seat") == table.get("sb_seat"):
                p["position"] = "BTN/SB"
            result.append(p)
        return result

    def betting_context(self):
        table, me = self.room["table"], self.me()
        call = min((table.get("legal_actions") or {}).get("call_amount", 0), me["chips"])
        seats = [p for p in table["seats"] if p]
        opponents = [p for p in seats if p.get("has_cards") and not p["is_folded"] and p["player_id"] != me["player_id"]]
        paid = me.get("current_round_bet", 0)
        cap = paid + call
        # A live caller can contest earlier streets; this street's excess above their cap cannot be won.
        previous_pot = max(0, table.get("total_pot", 0) - sum(p.get("current_round_bet", 0) for p in seats))
        eligible_pot = previous_pot + sum(min(p.get("current_round_bet", 0), cap) for p in seats) + call
        bb = table["big_blind"]
        fraction = call / me["chips"] if me["chips"] else 0
        return {"call_amount": call, "call_bb": call / bb, "hero_stack_bb": (me["chips"] + paid) / bb,
                "call_stack_fraction": fraction, "call_is_all_in": call > 0 and call == me["chips"],
                "eligible_pot_after_call": eligible_pot,
                "excluded_opponent_excess": max(0, table.get("total_pot", 0) + call - eligible_pot),
                "pot_odds": call / eligible_pot if call and eligible_pot else 0,
                "spr_after_call": (me["chips"] - call) / eligible_pot if eligible_pot else None,
                "effective_stacks_bb": {p["player_id"]: min(me["chips"] + paid,
                    p["chips"] + p.get("current_round_bet", 0)) / bb for p in opponents},
                "max_opponent_wager_bb": max((p.get("current_round_bet", 0) / bb for p in opponents), default=0),
                "facing_all_in": any(p.get("is_all_in") and p.get("current_round_bet", 0) > paid for p in opponents),
                "note": "赔率只计算跟注后可争夺的筹码，排除对手超额投入；随机范围权益不是面对加注/全下范围的真实胜率或EV。"}

    def context(self):
        table, me = self.room["table"], self.me()
        summaries = []
        for street in ("PREFLOP", "FLOP", "TURN", "RIVER"):
            moves = [a for a in self.actions if a["street"] == street]
            if moves:
                summaries.append({"street": street, "actions": len(moves),
                                  "raises": sum(a["aggressive"] for a in moves),
                                  "last_aggressor": next((a["player_id"] for a in reversed(moves) if a["aggressive"]), None)})
        context = {"phase": self.key(), "room_id": self.room_id,
                   "observation_scope": "players[].observed: server_all_rooms; room_observed: current_room_recorded_hands, not session",
                   "room_observed": self.store.profiles(self.settings["server_url"], self.room_id).get(me["player_id"], {}),
                   "hero": {"player_id": me["player_id"], "hole_cards": [card_text(c) for c in me["hole_cards"]],
                                                  "chips": me["chips"], "seat_index": me["seat_index"]},
                   "table": {k: table.get(k) for k in ("hand_number", "street", "board_cards", "board_cards_2",
                                                       "total_pot", "pots", "small_blind", "big_blind",
                                                        "dealer_seat", "current_turn_seat", "turn_count", "legal_actions", "rit_votes", "next_hand_at")},
                   "players": self.players(), "street_summaries": summaries,
                   "current_hand_actions": [{k: v for k, v in a.items() if k != "timestamp"} for a in self.actions],
                   "recent_hand_summaries": self.store.recent_hands(self.settings["server_url"], me["player_id"],
                                                                  {p["player_id"] for p in self.players()}),
                   "recent_chat": list(self.chats)[-30:],
                   "own_decisions": [{k: v for k, v in d.items() if k != "betting"} for d in self.hand_decisions],
                   "complete_observation": self.complete_hand,
                   "hand_results": table.get("hand_results", []),
                   "coordination": {"version": self.coordination_version, "shared_plan": self.shared_plan,
                                    "recent_activity": list(self.agent_activity)[-12:]},
                   "memory_notes": self.relevant_memories()}
        for key in ("board_cards", "board_cards_2"):
            context["table"][key] = [card_text(c) for c in context["table"][key] or []]
        # Keep the current hand intact unless unusually large; older summaries go first.
        while len(json.dumps(context, ensure_ascii=False).encode("utf-8")) > CONTEXT_BYTES:
            if context["recent_hand_summaries"]:
                context["recent_hand_summaries"].pop()
            elif context["recent_chat"]:
                context["recent_chat"].pop(0)
            elif context["current_hand_actions"]:
                context["current_hand_actions"].pop(0)
                context["omitted_older_current_actions"] = context.get("omitted_older_current_actions", 0) + 1
            elif len(context["memory_notes"]) > 1:
                context["memory_notes"].pop()
            else:
                break
        return context

    def remaining_time(self):
        table = self.room["table"]
        if not self.key() or len(self.key()) < 3:
            return 20.0
        server_now = table.get("server_time", time.time())
        deadline = (table.get("turn_started_at") or server_now) + table.get("current_turn_duration", 15)
        return max(0.2, deadline - server_now - (time.monotonic() - self.snapshot_at) - 0.8)

    async def decide(self, key):
        started = time.monotonic()
        self.status = "模型思考中"
        style = STYLES[self.hand_style]
        system = SYSTEM + "\n本手风格：" + style["prompt"] + "\n用户偏好：" + self.settings["custom_prompt"]
        action_tool = "poker_action" if len(key) >= 3 else "table_action"
        names = {action_tool}
        tools = [t for t in TOOLS if t["function"]["name"] in names]
        acted = False
        cancelled = False
        try:
            async with asyncio.timeout(min(DECISION_SECONDS, self.remaining_time())):
                messages = [{"role": "system", "content": system},
                            {"role": "user", "content": json.dumps(self.context(), ensure_ascii=False)}]
                for round_number in range(3):
                    if not self.running or self.key() != key:
                        return
                    version = self.coordination_version
                    messages[1] = {"role": "user", "content": json.dumps(self.context(), ensure_ascii=False)}
                    if len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False).encode("utf-8")) > INPUT_BYTES:
                        messages = [messages[0], {"role": "user", "content": json.dumps(self.context(), ensure_ascii=False)
                                    + "\n此前工具结果已汇总到以上数据；请立即选择当前阶段的行动。"}]
                    self.metrics["model_calls"] += 1
                    self.metrics["input_chars"] = len(json.dumps(messages, ensure_ascii=False))
                    call_started = time.monotonic()
                    response = await self.http.post(self.settings["model_url"] + "/chat/completions",
                        headers={"Authorization": "Bearer " + self.settings["api_key"]}, timeout=self.remaining_time(),
                        json={"model": self.settings["model"], "messages": messages, "tools": tools,
                              "tool_choice": {"type": "function", "function": {"name": action_tool}},
                              "temperature": 0.7, "max_tokens": 1200,
                              "reasoning_effort": "low", "thinking_token_budget": THINKING_TOKENS})
                    response.raise_for_status()
                    data = response.json()
                    usage = data.get("usage") or {}
                    self.record_usage(usage)
                    self.metrics["input_tokens"] = usage.get("prompt_tokens")
                    message = data["choices"][0]["message"]
                    calls = (message.get("tool_calls") or [])[:6]
                    self.log("model_call", agent="decision", hand=key[0], phase=key[1], round=round_number + 1,
                             elapsed=round(time.monotonic() - call_started, 2),
                             prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
                             reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                             finish=data["choices"][0].get("finish_reason"),
                             tools=[c["function"]["name"] for c in calls])
                    if version != self.coordination_version:
                        messages = messages[:2]
                        continue
                    if not calls:
                        messages.append({"role": "user", "content": "请现在调用当前阶段的行动工具，不要只输出文字。"})
                        continue
                    assistant = {"role": "assistant", "content": message.get("content"), "tool_calls": calls}
                    messages.append(assistant)
                    for call in calls[:6]:
                        name = call["function"]["name"]
                        arguments = {}
                        try:
                            if name not in names:
                                raise ValueError("对局 agent 只能调用当前阶段的行动工具；胜率、赔率和 outs 由你根据原始局面自行判断，没有计算工具")
                            arguments = json.loads(call["function"]["arguments"])
                            if not isinstance(arguments, dict):
                                raise ValueError("工具参数必须是对象")
                            if acted:
                                result = {"error": "当前阶段已执行操作"}
                            else:
                                result, is_action = await self.execute(name, arguments, key, started, version)
                                acted = acted or is_action
                        except (ValueError, KeyError, TypeError) as exc:
                            result = {"error": str(exc)[:200]}
                        self.log("tool", name=name, arguments=arguments, result=result)
                        messages.append({"role": "tool", "tool_call_id": call["id"],
                                         "content": json.dumps(result, ensure_ascii=False)})
                    if acted:
                        return
                    if round_number >= 1:
                        # Keep the current tool exchange valid and discard older analysis exchanges.
                        messages = messages[:2] + [assistant] + messages[-len(calls[:6]):]
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:
            self.log("model_error", message=f"模型调用失败：{type(exc).__name__}",
                     elapsed=round(time.monotonic() - started, 2))
            self.error = f"模型调用失败（{type(exc).__name__}），采用保底操作"
        finally:
            if not cancelled and not acted and self.running and self.key() == key:
                await self.fallback(key)
            if not cancelled and self.running and key[1] == "between":
                await self.ready_if_allowed()
            if self.running:
                self.status = "观察牌桌"

    async def execute(self, name, args, key, started, version=None):
        if name == "remember_note" and self.running:
            subject, note = args.get("subject"), args.get("note")
            allowed = {"self", *(p["player_id"] for p in self.players())}
            if subject not in allowed or not isinstance(note, str) or not 1 <= len(note.strip()) <= 400:
                raise ValueError("记忆主题须为 self 或当前玩家 ID，笔记为 1–400 字符")
            self.store.remember(self.settings["server_url"], self.settings["username"], subject, note.strip())
            return {"saved": True, "subject": subject}, False
        if not self.running or self.key() != key:
            return {"error": "轮次已变化，丢弃过期决策"}, False
        table, me = self.room["table"], self.me()
        if name == "get_players":
            return {"players": self.players()}, False
        if name == "recall_memory":
            return {"notes": self.relevant_memories()}, False
        reason = str(args.get("reason", ""))[:240]
        if name == "poker_action":
            if len(key) < 3:
                raise ValueError("当前不是下注轮次")
            plan = args.get("plan")
            if "plan" in args and (not isinstance(plan, str) or len(plan.strip()) > 600):
                raise ValueError("共同计划须为最多 600 字符的文字")
            payload = validated_action(args, table.get("legal_actions") or {}, table["small_blind"])
            delay = min(max(0, random.uniform(0.6, 1.8) - (time.monotonic() - started)),
                        max(0, self.remaining_time() - 0.2))
            await asyncio.sleep(delay)
            if not self.running or self.key() != key:
                return {"error": "轮次已变化"}, False
            if version is not None and version != self.coordination_version:
                return {"error": "共享计划已更新，需要重新决定"}, False
            payload = validated_action(args, self.room["table"]["legal_actions"], table["small_blind"])
            await self.send("PLAYER_ACTION", payload)
            if plan is not None:
                self.shared_plan = {"hand": key[0], "street": key[1], "source": "decision", "plan": plan.strip()} if plan.strip() else None
            self.metrics["decisions"] += 1
            self.last_decision = {**payload, "reason": reason, "hand": key[0], "style": self.hand_style,
                                  "street": key[1], "turn_count": key[2], "plan": plan,
                                  "elapsed": round(time.monotonic() - started, 2)}
            self.hand_decisions.append(dict(self.last_decision))
            self.log("decision", **self.last_decision)
            return {"submitted": True, **payload}, True
        if name == "table_action":
            action = args.get("action")
            if action == "rit" and key[1] == "rit":
                if args.get("choice") not in (1, 2):
                    raise ValueError("RIT 选择应为 1 或 2")
                await self.send("RIT_CHOICE", {"choice": args["choice"]})
            elif action == "rebuy" and table["street"] in ("IDLE", "HAND_END") and me["chips"] == 0:
                await self.send("REBUY")
            elif action == "leave":
                self.running = False
                asyncio.create_task(self.stop())
            elif action == "wait" and key[1] == "between" and me["chips"] > 0:
                pass
            else:
                raise ValueError("当前阶段不支持该桌面操作")
            self.log("table_decision", action=action, reason=reason, hand=key[0])
            return {"submitted": True, "action": action}, True
        raise ValueError("未知工具")

    async def fallback(self, key):
        if not self.running or self.key() != key:
            return
        if len(key) >= 3:
            legal = self.room["table"].get("legal_actions") or {}
            action = "CHECK" if legal.get("can_check") else "FOLD"
            if not legal.get("can_" + action.lower()):
                return
            await self.send("PLAYER_ACTION", {"action": action, "amount": 0})
        elif key[1] == "rit":
            action = "RIT_1"
            await self.send("RIT_CHOICE", {"choice": 1})
        elif (self.me() or {}).get("chips") == 0:
            action = "LEAVE"
            asyncio.create_task(self.stop())
        else:
            return
        self.metrics["fallbacks"] += 1
        turn = key[2] if len(key) >= 3 else None
        self.log("fallback", action=action, reason="模型超时或未给出合法操作", hand=key[0], street=key[1], turn_count=turn)
        self.hand_decisions.append({"action": action, "reason": "模型超时或未给出合法操作",
                                    "hand": key[0], "street": key[1], "turn_count": turn, "fallback": True})

    async def stop(self):
        if self.stopping:
            return
        self.stopping = bool(self.room_id)
        self.running = False
        await self.cancel_decision()
        if self.social_decision and not self.social_decision.done():
            self.social_decision.cancel()
            await asyncio.gather(self.social_decision, return_exceptions=True)
        self.social_status = "已停止"
        if self.reflection and not self.reflection.done() and self.reflection is not asyncio.current_task():
            self.reflection.cancel()
            await asyncio.gather(self.reflection, return_exceptions=True)
        self.reflection_status = "已停止"
        if not self.room_id:
            self.status = "已停止"
            return
        self.stopping, self.status = True, "正在退出房间"
        if self.room.get("table", {}).get("street") == "RIT_DECISION" and self.ws:
            try:
                await self.send("RIT_CHOICE", {"choice": 1})
            except Exception:
                pass
        await self.try_leave()
        if self.stopping and (not self.departure or self.departure.done()):
            self.departure = asyncio.create_task(self.wait_to_leave())

    async def try_leave(self):
        try:
            snapshot = await self.api("GET", "/api/rooms/" + quote(self.room_id, safe=""))
            seated = any(p and p["player_id"] == self.user["user_id"] for p in snapshot["table"]["seats"])
            if seated:
                await self.api("POST", "/api/rooms/" + quote(self.room_id, safe="") + "/leave")
        except RemoteError as exc:
            if exc.code != 404:
                self.status = "等待本手结束后离座" if exc.code == 409 else "离座失败 · 正在重试"
                self.error = str(exc)
                return
        except Exception as exc:
            self.status, self.error = "离座失败 · 正在重试", f"退出请求失败（{type(exc).__name__}）"
            return
        self.log("session_stop", room_id=self.room_id)
        self.stopping = False
        self.room_id = None
        if self.receiver and self.receiver is not asyncio.current_task():
            self.receiver.cancel()
            await asyncio.gather(self.receiver, return_exceptions=True)
        if self.ws:
            await self.ws.close()
        self.ws = None
        self.room = {}
        self.status, self.error = "已停止 · 已退出房间", None

    async def wait_to_leave(self):
        while self.stopping:
            await asyncio.sleep(1)
            await self.try_leave()

    def snapshot(self):
        return {"running": self.running, "stopping": self.stopping, "status": self.status,
                "error": self.error, "user": self.user, "room_id": self.room_id,
                "room": self.room, "style": self.hand_style, "metrics": self.metrics,
                "tokens": {
                    "session": dict(self.session_tokens),
                    "hand": dict(self.hand_tokens),
                    "total": dict(self.historical_tokens)
                },
                "analysis": self.analysis, "last_decision": self.last_decision,
                "reflection_status": self.reflection_status, "last_reflection": self.last_reflection,
                "social_status": self.social_status,
                "last_social_decision": self.last_social_decision,
                "shared_plan": self.shared_plan,
                "betting": self.betting_context() if self.me() else None,
                "players": self.players(), "logs": self.store.log_tail(),
                "memories": self.store.memories(self.settings["server_url"], self.settings["username"])}

    def relevant_memories(self):
        subjects = {"self", *(p["player_id"] for p in self.players())}
        notes = [{"subject": item["subject"], "note": item["note"]} for item in
                self.store.memories(self.settings["server_url"], self.settings["username"])
                if item["subject"] in subjects]
        return sorted(notes, key=lambda item: item["subject"] != "self")

    async def close(self):
        await self.stop()
        if self.stopping:
            try:
                await asyncio.wait_for(asyncio.shield(self.departure), 8)
            except TimeoutError:
                self.log("error", message="服务退出时全下牌局尚未结束，服务端将按断线规则离座")
        for task in (self.receiver, self.departure):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.receiver, self.departure) if t), return_exceptions=True)
        await self.http.aclose()
        await self.poker_http.aclose()


class RemoteError(RuntimeError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code
