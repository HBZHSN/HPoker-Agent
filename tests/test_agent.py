import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

import app
from agent import CONTEXT_BYTES, SOCIAL_TOOLS, PokerAgent, RemoteError, validated_action
from poker_math import equity, evaluate, outs
from storage import Store


def snapshot(hand=1, street="PREFLOP", history=None):
    return {"room_id": "test", "host_player_id": "hero", "config": {"room_name": "test"},
            "table": {"hand_number": hand, "street": street, "small_blind": 5, "big_blind": 10,
                      "board_cards": [], "board_cards_2": [], "total_pot": 15, "pots": [],
                      "current_turn_seat": 0, "turn_count": hand, "server_time": time.time(),
                      "turn_started_at": time.time(), "current_turn_duration": 30,
                      "dealer_seat": 0, "sb_seat": 0, "bb_seat": 1,
                      "seats": [{"player_id": "hero", "name": "Hero", "seat_index": 0, "chips": 995,
                                 "hole_cards": [{"notation": "As"}, {"notation": "Ah"}], "has_cards": True,
                                 "is_folded": False, "is_all_in": False},
                                {"player_id": "villain", "name": "Villain", "seat_index": 1, "chips": 990,
                                 "hole_cards": [], "has_cards": True, "is_folded": False, "is_all_in": False}],
                      "legal_actions": {"can_check": True, "can_fold": True, "can_raise": True,
                                        "min_raise_to": 20, "max_raise_to": 1000, "can_all_in": True,
                                        "all_in_amount": 1000}, "action_history": history or [],
                      "hand_results": [], "ready_player_ids": []}}


def model_response(name=None, arguments=None):
    message = {"content": "当前无需交流"}
    if name:
        message["tool_calls"] = [{"id": name, "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments)}}]
    return httpx.Response(200, request=httpx.Request("POST", "http://model"),
                          json={"choices": [{"finish_reason": "tool_calls" if name else "stop", "message": message}]})


async def wait_until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(.005)


class CalculationTests(unittest.TestCase):
    def test_hand_order_and_kickers(self):
        self.assertEqual(evaluate("As 2d 3h 4c 5s 9h Td".split()), (4, 5))
        self.assertEqual(evaluate("As Ah Ad Ks Kh Kd 2s".split()), (6, 14, 13))
        self.assertEqual(evaluate("As Ah Ks Kh Qs Qh 2s".split()), (2, 14, 13, 12))
        self.assertGreater(evaluate("As Ks Qs Js Ts 2d 3h".split()), evaluate("Ah Ad Ac As Ks 2d 3h".split()))
        self.assertGreater(evaluate("As Ks 9s 8s 6s 2d 3h".split()), evaluate("Ah Kh 9h 8h 5h 2d 3s".split()))

    def test_random_equity_and_multiway_tie_share(self):
        result = equity(["As", "Ah"], [], iterations=1200, seed=42)
        self.assertTrue(0.80 < result["equity"] < 0.90)
        result = equity(["2c", "3d"], ["As", "Ks", "Qs", "Js", "Ts"], opponents=3, seed=42)
        self.assertEqual(result["equity"], .25)
        self.assertEqual(result["tie_rate"], 1)
        exact = equity(["2c", "3d"], ["As", "Ks", "Qs", "Js", "Ts"])
        self.assertEqual(exact["method"], "exact")
        self.assertEqual(exact["equity"], .5)

    def test_draws_and_invalid_inputs(self):
        result = outs(["As", "Ks"], ["2s", "7s", "9h"])
        self.assertEqual(len(result["categories"]["同花"]), 9)
        self.assertNotIn("As", result["cards"])
        for hole, board in [(["As", "As"], []), (["ZZ", "Kh"], []), (["As", "Kh"], ["2c"])]:
            with self.assertRaises(ValueError):
                equity(hole, board)

    def test_legal_raise_target(self):
        legal = snapshot()["table"]["legal_actions"]
        self.assertEqual(validated_action({"action": "RAISE", "amount": 35}, legal, 5)["amount"], 35)
        for value in (19, 21, 1001, True, "30"):
            with self.assertRaises(ValueError):
                validated_action({"action": "RAISE", "amount": value}, legal, 5)
        with self.assertRaises(ValueError):
            validated_action({"action": "CALL"}, legal, 5)


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.folder.name) / "test.sqlite3")
        self.agent = PokerAgent(app.Settings().model_dump(), self.store)
        self.agent.user = {"user_id": "hero", "username": "tester"}
        self.agent.room_id, self.agent.session = "test", "session"
        self.agent.running = True
        self.agent.send = AsyncMock()
        self.agent.observe(snapshot())

    async def asyncTearDown(self):
        self.agent.running = self.agent.stopping = False
        for task in (self.agent.decision, self.agent.departure, self.agent.reflection, self.agent.social_decision):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await self.agent.http.aclose()
        await self.agent.poker_http.aclose()
        self.store.db.close()
        self.folder.cleanup()

    def begin_next_hand(self, hand=2):
        previous = snapshot(hand=hand - 1, street="HAND_END", history=[
            {"player_id": "villain", "action": "ALL_IN", "amount": 1000, "street": "PREFLOP"},
            {"player_id": "hero", "action": "FOLD", "amount": 0, "street": "PREFLOP"}])
        previous["table"]["board_cards"] = [{"notation": "2c"}, {"notation": "4h"}, {"notation": "7s"}]
        previous["table"]["seats"][0]["hole_cards"] = [{"notation": "Qs"}, {"notation": "Qh"}]
        previous["table"]["seats"][1]["shown_cards"] = [{"notation": "9h"}, {"notation": "9d"}]
        previous["table"]["hand_results"] = [{"player_id": "hero", "name": "Hero", "net_profit": -20,
                                                "hand_desc": "未公开私牌不能交给发言 agent"}]
        self.agent.observe(previous)
        self.agent.observe(snapshot(hand=hand))

    async def test_overlapping_history_records_each_action_once(self):
        actions = [{"player_id": "hero" if i % 2 else "villain", "action": "RAISE", "amount": 20 + i * 10,
                    "street": "PREFLOP"} for i in range(15)]
        for count in range(1, 16):
            room = snapshot(history=actions[max(0, count - 10):count])
            room["table"]["turn_count"] = count
            self.agent.observe(room)
            self.agent.observe(room)
        self.assertEqual(len(self.agent.actions), 15)
        ended = snapshot(street="HAND_END", history=actions[-10:])
        self.agent.observe(ended)
        self.agent.observe(ended)
        self.assertEqual(self.agent.metrics["hands"], 1)
        self.assertEqual(len(self.store.history()), 1)
        self.assertEqual(len(self.store.history()[0]["data"]["actions"]), 15)

    async def test_blind_posts_do_not_make_a_full_hand_partial(self):
        blinds = [{"player_id": "hero", "action": "POST_SB", "amount": 5, "street": "PREFLOP"},
                  {"player_id": "villain", "action": "POST_BB", "amount": 10, "street": "PREFLOP"}]
        self.agent.observe(snapshot(hand=2, history=blinds))
        self.assertTrue(self.agent.complete_hand)
        self.agent.observe(snapshot(hand=2, street="HAND_END", history=blinds))
        self.assertEqual(self.store.profiles(self.agent.settings["server_url"])["hero"]["complete_hands"], 1)

    async def test_room_observation_does_not_mix_rooms_servers_or_partial_vpip(self):
        server = self.agent.settings["server_url"]
        table = snapshot(street="HAND_END")["table"]
        table["hand_results"] = [{"player_id": "hero", "net_profit": -10}]
        call = [{"player_id": "hero", "street": "PREFLOP", "action": "CALL", "amount": 5}]
        self.store.record_hand(server, "test", table, call, True)
        table["hand_number"] = 2
        self.store.record_hand(server, "test", table, call, False)
        table["hand_results"][0]["net_profit"] = 100
        self.store.record_hand(server, "other-room", table, [], True)
        self.store.record_hand("other-server", "test", table, [], True)
        context = self.agent.context()
        stats = context["room_observed"]
        self.assertEqual(context["room_id"], "test")
        self.assertEqual(stats["observed_hands"], 2)
        self.assertEqual(stats["complete_hands"], 1)
        self.assertEqual(stats["vpip"], 1)
        self.assertEqual(stats["vpip_pct"], 100)
        self.assertEqual(stats["net_chips"], -20)
        self.assertEqual(self.store.profiles(server)["hero"]["net_chips"], 80)
        self.assertEqual(self.store.profiles(server, "missing"), {})

    async def test_social_tool_has_a_cooldown(self):
        self.begin_next_hand()
        comment = {"message": "上把这也能中啊", "reason": "吐槽上一手"}
        await self.agent.apply_social(comment, "test", 2, time.monotonic())
        self.assertTrue(self.agent.last_social_decision["results"]["communication"]["submitted"])
        self.agent.send.assert_awaited_once_with("CHAT_MESSAGE", {"message": comment["message"]})
        await self.agent.apply_social(comment, "test", 2, time.monotonic())
        self.assertIn("error", self.agent.last_social_decision["results"]["communication"])
        self.assertEqual(self.agent.send.await_count, 1)

    async def test_chat_replies_bypass_cooldown_in_model_input_and_send(self):
        self.begin_next_hand()
        self.agent.last_social = time.monotonic()
        self.agent.http.post = AsyncMock(side_effect=[
            model_response("send_text", {"message": "急啥", "reason": "回应第一句"}),
            model_response("send_text", {"message": "少套我话", "reason": "回应第二句"}),
            model_response()])
        for message in ("怎么不说话", "手里啥牌"):
            self.agent.observe_chat("CHAT_MESSAGE", {"player_id": "villain", "message": message})
            await self.agent.choose_social("chat")
            self.assertTrue(self.agent.last_social_decision["results"]["communication"]["submitted"])
        self.assertEqual([call.args for call in self.agent.send.await_args_list], [
            ("CHAT_MESSAGE", {"message": "急啥"}), ("CHAT_MESSAGE", {"message": "少套我话"})])
        for call in self.agent.http.post.await_args_list:
            context = json.loads(call.kwargs["json"]["messages"][1]["content"])
            self.assertEqual(context["chat_cooldown_seconds"], 0)
        await self.agent.choose_social("new_hand")
        context = json.loads(self.agent.http.post.await_args.kwargs["json"]["messages"][1]["content"])
        self.assertGreater(context["chat_cooldown_seconds"], 0)
        await self.agent.apply_social({"message": "上把真能中", "reason": "点评上一手"}, "test", 2, time.monotonic())
        self.assertIn("error", self.agent.last_social_decision["results"]["communication"])
        self.assertEqual(self.agent.send.await_count, 2)

    async def test_memory_persists_isolated_and_context_is_bounded(self):
        result, acted = await self.agent.execute("remember_note", {"subject": "self", "note": "保持稳健形象"},
                                                 self.agent.key(), time.monotonic())
        self.assertTrue(result["saved"])
        self.assertFalse(acted)
        self.assertEqual(self.store.memories("another-server", self.agent.settings["username"]), [])
        self.assertEqual(self.store.memories(self.agent.settings["server_url"], "another-account"), [])
        self.agent.actions = [{"player_id": "hero", "street": "PREFLOP", "action": "RAISE", "amount": i,
                               "aggressive": True} for i in range(300)]
        context = self.agent.context()
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False).encode("utf-8")), CONTEXT_BYTES)
        self.assertEqual(len(context["current_hand_actions"]), 300)
        self.assertEqual(context["street_summaries"][0]["actions"], 300)
        self.assertEqual(context["memory_notes"][0]["note"], "保持稳健形象")
        reopened = Store(Path(self.folder.name) / "test.sqlite3")
        self.assertEqual(reopened.memories(self.agent.settings["server_url"], self.agent.settings["username"])[0]["note"], "保持稳健形象")
        reopened.db.close()

    async def test_context_keeps_relevant_recent_hands_and_trims_old_summaries_first(self):
        self.agent.observe(snapshot(street="HAND_END"))
        self.agent.observe(snapshot(hand=2))
        context = self.agent.context()
        self.assertEqual(context["recent_hand_summaries"][0]["hand"], 1)
        self.assertEqual(context["recent_hand_summaries"][0]["hero_cards"], ["As", "Ah"])
        self.assertEqual(self.store.recent_hands(self.agent.settings["server_url"], "another-player", {"hero"}), [])
        self.agent.actions = [{"player_id": "hero", "street": "PREFLOP", "action": "CALL", "amount": 10,
                               "aggressive": False} for _ in range(120)]
        with patch('agent.CONTEXT_BYTES', 10000):
            context = self.agent.context()
            self.assertEqual(context["recent_hand_summaries"], [])
            self.assertGreater(len(context["current_hand_actions"]), 24)
            self.assertLessEqual(len(json.dumps(context, ensure_ascii=False).encode('utf-8')), 10000)

    async def test_stale_decision_cannot_send(self):
        key = self.agent.key()
        self.agent.room["table"]["turn_count"] += 1
        result, acted = await self.agent.execute("poker_action", {"action": "CHECK"}, key, time.monotonic())
        self.assertFalse(acted)
        self.assertIn("error", result)
        self.agent.send.assert_not_called()

    async def test_model_failure_uses_check_without_spending_chips(self):
        self.agent.http.post = AsyncMock(side_effect=httpx.ReadTimeout("test timeout"))
        await self.agent.decide(self.agent.key())
        self.agent.send.assert_awaited_once_with("PLAYER_ACTION", {"action": "CHECK", "amount": 0})
        self.assertEqual(self.agent.metrics["fallbacks"], 1)
        self.assertEqual(self.agent.hand_decisions[-1]["turn_count"], self.agent.key()[2])

    async def test_thinking_budget_and_raw_state_are_in_first_request(self):
        response = httpx.Response(200, request=httpx.Request("POST", "http://model"), json={
            "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
                {"id": "action", "type": "function", "function": {"name": "poker_action",
                    "arguments": json.dumps({"action": "CHECK", "reason": "免费观察下一张牌"})}}]}}],
            "usage": {"prompt_tokens": 1500, "completion_tokens": 310,
                      "completion_tokens_details": {"reasoning_tokens": 256}}})
        self.agent.http.post = AsyncMock(return_value=response)
        self.agent.analysis = {"equity": .99, "pot_odds": .05, "outs": {"count": 9}}
        with patch("agent.random.uniform", return_value=0), patch.object(
                self.agent, "betting_context", side_effect=AssertionError("模型输入不能调用赔率计算")):
            await self.agent.decide(self.agent.key())
        payload = self.agent.http.post.await_args.kwargs["json"]
        self.assertEqual(payload["thinking_token_budget"], 512)
        self.assertEqual(payload["max_tokens"], 1200)
        self.assertEqual(payload["reasoning_effort"], "low")
        context = json.loads(payload["messages"][1]["content"])
        for key in ("calculation", "equity", "outs", "pot_odds", "betting", "effective_stacks"):
            self.assertNotIn(key, context)
        self.assertEqual(context["hero"]["hole_cards"], ["As", "Ah"])
        self.assertEqual(context["table"]["big_blind"], 10)
        self.assertEqual(context["table"]["legal_actions"], snapshot()["table"]["legal_actions"])
        self.assertIn("players", context)
        self.assertIn("memory_notes", context)
        self.assertEqual({t["function"]["name"] for t in payload["tools"]},
                         {"poker_action"})
        self.assertEqual(payload["tool_choice"], {"type": "function", "function": {"name": "poker_action"}})
        self.agent.send.assert_awaited_once_with("PLAYER_ACTION", {"action": "CHECK", "amount": 0})
        self.assertNotIn("betting", self.agent.hand_decisions[-1])
        timing = next(e["data"] for e in self.store.log_tail() if e["kind"] == "model_call")
        self.assertEqual(timing["reasoning_tokens"], 256)

    async def test_removed_calculation_tool_is_rejected_and_action_retried(self):
        def response(name, arguments):
            return httpx.Response(200, request=httpx.Request("POST", "http://model"), json={
                "choices": [{"message": {"tool_calls": [{"id": name, "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)}}]}}]})
        self.agent.http.post = AsyncMock(side_effect=[response("calculate_equity", {"iterations": 100}),
                                                       response("poker_action", {"action": "CHECK"})])
        with patch("agent.random.uniform", return_value=0), patch.object(
                self.agent, "execute", wraps=self.agent.execute) as execute:
            await self.agent.decide(self.agent.key())
        self.assertEqual([call.args[0] for call in execute.await_args_list], ["poker_action"])
        second = self.agent.http.post.await_args_list[1].kwargs["json"]
        self.assertEqual(second["tool_choice"], {"type": "function", "function": {"name": "poker_action"}})
        self.assertTrue(any("error" in json.loads(m["content"]) for m in second["messages"] if m["role"] == "tool"))
        self.agent.send.assert_awaited_once_with("PLAYER_ACTION", {"action": "CHECK", "amount": 0})
        with self.assertRaisesRegex(ValueError, "未知工具"):
            await self.agent.execute("calculate_equity", {}, self.agent.key(), time.monotonic())

    async def test_whole_decision_timeout_includes_model_wait(self):
        async def slow_request(*args, **kwargs):
            await asyncio.sleep(1)
        self.agent.http.post = AsyncMock(side_effect=slow_request)
        self.agent.analysis = {"equity": .85}
        with patch("agent.DECISION_SECONDS", .02):
            started = time.monotonic()
            await self.agent.decide(self.agent.key())
        self.assertLess(time.monotonic() - started, .5)
        self.agent.send.assert_awaited_once_with("PLAYER_ACTION", {"action": "CHECK", "amount": 0})
        self.assertEqual(self.agent.metrics["fallbacks"], 1)

    async def test_cancellation_never_emits_fallback(self):
        self.agent.http.post = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.agent.decide(self.agent.key())
        self.agent.send.assert_not_called()

    async def test_hand_end_ready_does_not_call_decision_model(self):
        self.agent.observe(snapshot(street="HAND_END"))
        self.agent.settings["social"] = False
        self.agent.http.post = AsyncMock()
        await self.agent.advance()
        await self.agent.advance()
        self.assertIsNone(self.agent.key())
        self.agent.http.post.assert_not_called()
        self.agent.send.assert_awaited_once_with("PLAYER_READY", {"ready": True})

    async def test_decision_only_runs_when_our_action_is_needed(self):
        self.agent.decide = AsyncMock()
        for hand, state in enumerate(("IDLE", "waiting", "folded", "all_in", "our_turn", "rit", "busted"), 2):
            room = snapshot(hand=hand, street="IDLE" if state in ("IDLE", "busted") else "PREFLOP")
            if state == "waiting":
                room["table"]["current_turn_seat"] = 1
            if state in ("folded", "all_in"):
                room["table"]["seats"][0]["is_folded" if state == "folded" else "is_all_in"] = True
            if state == "rit":
                room["table"].update(street="RIT_DECISION", rit_voters=["hero"], rit_votes={})
            if state == "busted":
                room["table"]["seats"][0]["chips"] = 0
            self.agent.observe(room)
            key = self.agent.key()
            before = self.agent.decide.await_count
            await self.agent.advance()
            if self.agent.decision:
                await self.agent.decision
            await self.agent.advance()
            self.assertEqual(self.agent.decide.await_count - before, int(state in ("our_turn", "rit", "busted")), state)
            if state in ("our_turn", "rit", "busted"):
                self.agent.decide.assert_awaited_with(key)

    async def test_reflection_continues_while_next_hand_decides(self):
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []
        async def post(*args, **kwargs):
            payload = kwargs["json"]
            requests.append(payload)
            reflection = payload["tools"][0]["function"]["name"] == "finish_reflection"
            social = payload["tools"][0]["function"]["name"] == "send_text"
            if social:
                return httpx.Response(200, request=httpx.Request("POST", "http://model"), json={
                    "choices": [{"finish_reason": "stop", "message": {"content": "当前无需交流"}}]})
            if reflection:
                entered.set()
                await release.wait()
            name = "finish_reflection" if reflection else "poker_action"
            arguments = ({"summary": "弃牌符合当前证据，后续仍需观察。", "updates": [
                {"subject": "self", "note": "保持有选择的入池范围。"}], "delete_subjects": []}
                if reflection else {"action": "CHECK", "reason": "免费观察"})
            return httpx.Response(200, request=httpx.Request("POST", "http://model"), json={
                "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
                    {"id": name, "type": "function", "function": {"name": name,
                        "arguments": json.dumps(arguments)}}]}}]})
        self.agent.http.post = AsyncMock(side_effect=post)
        folded = snapshot()
        folded["table"]["seats"][0]["is_folded"] = True
        folded["table"]["current_turn_seat"] = 1
        folded["table"]["legal_actions"] = {}
        self.agent.observe(folded)
        self.agent.ws = AsyncMock()
        self.agent.start_background()
        await self.agent.advance()
        await asyncio.wait_for(entered.wait(), 1)
        reflection_task = self.agent.reflection
        await self.agent.advance()
        self.assertEqual(sum(p["tools"][0]["function"]["name"] == "finish_reflection" for p in requests), 1)
        self.agent.observe(snapshot(hand=2))
        with patch("agent.random.uniform", return_value=0):
            await self.agent.advance()
            await asyncio.wait_for(self.agent.decision, 1)
        self.assertFalse(reflection_task.done())
        self.agent.send.assert_awaited_once_with("PLAYER_ACTION", {"action": "CHECK", "amount": 0})
        release.set()
        await wait_until(lambda: self.agent.last_reflection is not None)
        self.assertFalse(reflection_task.done())
        self.assertEqual(self.agent.last_reflection["hand"], 1)
        self.assertEqual(self.agent.last_decision["hand"], 2)
        self.assertEqual(next(p for p in requests if p["tools"][0]["function"]["name"] == "finish_reflection")["thinking_token_budget"], 2048)
        self.assertEqual(next(p for p in requests if p["tools"][0]["function"]["name"] == "poker_action")["thinking_token_budget"], 512)
        self.assertEqual(self.agent.relevant_memories()[0]["note"], "保持有选择的入池范围。")

    async def test_reflection_loops_and_social_runs_once_per_new_hand(self):
        requests, busy, maximum = {"social": [], "reflection": []}, {"social": 0, "reflection": 0}, {}
        async def post(*args, **kwargs):
            payload = kwargs["json"]
            role = "reflection" if payload["tools"][0]["function"]["name"] == "finish_reflection" else "social"
            requests[role].append(json.loads(payload["messages"][1]["content"]))
            busy[role] += 1
            maximum[role] = max(maximum.get(role, 0), busy[role])
            await asyncio.sleep(.005)
            busy[role] -= 1
            return model_response("finish_reflection", {"summary": "持续观察", "updates": [
                {"subject": "self", "note": "继续保持稳健形象"}], "delete_subjects": []}) if role == "reflection" else model_response()
        self.agent.http.post = AsyncMock(side_effect=post)
        self.agent.ws = AsyncMock()
        self.begin_next_hand()
        with patch("agent.SOCIAL_INTERVAL", .02), patch("agent.REFLECTION_INTERVAL", .02):
            self.agent.start_background()
            tasks = self.agent.social_decision, self.agent.reflection
            self.agent.start_background()
            self.assertEqual(tasks, (self.agent.social_decision, self.agent.reflection))
            await wait_until(lambda: len(requests["social"]) == 1 and len(requests["reflection"]) >= 2)
            self.assertEqual(len(requests["social"]), 1)
            self.begin_next_hand(3)
            await wait_until(lambda: len(requests["social"]) == 2)
        self.assertEqual(maximum, {"social": 1, "reflection": 1})
        self.assertEqual(requests["reflection"][1]["memory_notes"][0]["note"], "继续保持稳健形象")
        self.assertEqual([r["previous_hand"]["hand"] for r in requests["social"]], [1, 2])
        self.assertNotIn("memory_notes", requests["social"][1])
        self.assertFalse(any(task.done() for task in tasks))

    async def test_disabled_or_disconnected_background_agents_do_not_request_models(self):
        self.agent.http.post = AsyncMock()
        with patch("agent.SOCIAL_INTERVAL", .01), patch("agent.REFLECTION_INTERVAL", .01):
            self.agent.start_background()
            await asyncio.sleep(.03)
            self.agent.http.post.assert_not_called()
            self.agent.settings["social"] = False
            self.agent.ws = AsyncMock()
            self.agent.reflection.cancel()
            await asyncio.gather(self.agent.reflection, return_exceptions=True)
            await asyncio.sleep(.03)
            self.agent.http.post.assert_not_called()
            self.assertEqual(self.agent.social_status, "已关闭")

    async def test_reflection_memory_cleanup_is_scoped_and_audited(self):
        server, account = self.agent.settings["server_url"], self.agent.settings["username"]
        self.store.remember(server, account, "old-player", "已经过期的猜测")
        self.store.remember("other-server", account, "old-player", "不可删除")
        self.store.remember(server, "other-account", "old-player", "不可删除")
        job = {"hand": 1, "room_id": "test", "trigger": "hand_end", "context": self.agent.context()}
        await self.agent.finish_reflection({"summary": "更新自身策略，移除过期猜测。", "updates": [
            {"subject": "self", "note": "  根据真实样本调整范围。  "}],
            "delete_subjects": ["old-player"]}, job, time.monotonic())
        notes = self.store.memories(server, account)
        self.assertEqual(notes[0]["subject"], "self")
        self.assertEqual(notes[0]["note"], "根据真实样本调整范围。")
        self.assertEqual(len(self.store.memories("other-server", account)), 1)
        self.assertEqual(len(self.store.memories(server, "other-account")), 1)
        self.assertEqual(self.agent.last_reflection["previous_notes"][0]["note"], "已经过期的猜测")
        self.agent.send.assert_not_called()

    async def test_invalid_reflection_batch_preserves_all_memory(self):
        server, account = self.agent.settings["server_url"], self.agent.settings["username"]
        self.store.remember(server, account, "self", "已有策略")
        self.store.remember(server, account, "old-player", "已有观察")
        job = {"hand": 1, "room_id": "test", "trigger": "hand_end", "context": self.agent.context()}
        with self.assertRaises(ValueError):
            await self.agent.finish_reflection({"summary": "复盘", "updates": [
                {"subject": "self", "note": "新策略"}, {"subject": "unknown", "note": "不允许"}],
                "delete_subjects": ["old-player"]}, job, time.monotonic())
        self.assertEqual({n["subject"]: n["note"] for n in self.store.memories(server, account)},
                         {"self": "已有策略", "old-player": "已有观察"})
        self.assertIsNone(self.agent.last_reflection)

    async def test_old_reflection_can_update_memory_in_new_hand(self):
        job = {"hand": 1, "room_id": "test", "trigger": "fold", "context": self.agent.context()}
        self.agent.observe(snapshot(hand=2))
        await self.agent.finish_reflection({"summary": "保留可用观察。", "updates": [],
                                           "delete_subjects": []}, job, time.monotonic())
        self.agent.send.assert_not_called()
        self.assertEqual(self.agent.last_reflection["hand"], 1)

    async def test_reflection_cannot_send_social_actions(self):
        job = {"hand": 1, "room_id": "test", "trigger": "fold", "context": self.agent.context()}
        with self.assertRaises(ValueError):
            await self.agent.finish_reflection({"summary": "总结", "updates": [], "delete_subjects": [],
                                               "emoji": "👍"}, job, time.monotonic())
        self.agent.send.assert_not_called()

    async def test_stop_cancels_all_agents(self):
        self.agent.reflection = asyncio.create_task(asyncio.sleep(1))
        self.agent.decision = asyncio.create_task(asyncio.sleep(1))
        self.agent.social_decision = asyncio.create_task(asyncio.sleep(1))
        reflection = self.agent.reflection
        decision = self.agent.decision
        social = self.agent.social_decision
        self.agent.api = AsyncMock(side_effect=[snapshot(), {"success": True}])
        await self.agent.stop()
        self.assertTrue(reflection.cancelled())
        self.assertTrue(decision.cancelled())
        self.assertTrue(social.cancelled())

    async def test_social_only_accepts_one_text_comment(self):
        self.begin_next_hand()
        for choice in ("first", "second", "both", "hide"):
            with self.assertRaises(ValueError):
                await self.agent.apply_social({"show": choice, "reason": "展示"}, "test", 2, time.monotonic())
        self.agent.send.assert_not_called()
        self.agent.http.post = AsyncMock(return_value=model_response("show_cards", {"cards": "both", "reason": "展示"}))
        await self.agent.choose_social("new_hand")
        self.agent.send.assert_not_called()
        self.assertEqual(self.store.log_tail()[-1]["kind"], "social_error")

    async def test_social_does_not_run_without_observed_previous_hand_or_on_late_join(self):
        self.agent.http.post = AsyncMock(return_value=model_response())
        await self.agent.choose_social("new_hand")
        self.agent.observe(snapshot(street="HAND_END"))
        self.agent.observe(snapshot(hand=2, history=[
            {"player_id": "villain", "action": "RAISE", "amount": 30, "street": "PREFLOP"}]))
        await self.agent.choose_social("new_hand")
        self.agent.http.post.assert_not_called()
        self.agent.send.assert_not_called()

    async def test_delayed_comment_cannot_cross_hand_or_room(self):
        self.begin_next_hand()
        comment = {"reason": "上一手评论", "message": "上把推得够猛"}
        await self.agent.apply_social(comment, "other-room", 2, time.monotonic())
        await self.agent.apply_social(comment, "test", 1, time.monotonic())
        self.begin_next_hand(3)
        await self.agent.apply_social(comment, "test", 2, time.monotonic())
        self.agent.send.assert_not_called()

    async def test_previous_hand_comment_is_independent_of_busy_reflection(self):
        self.begin_next_hand()
        self.agent.reflection = asyncio.create_task(asyncio.sleep(1))
        self.agent.http.post = AsyncMock(return_value=model_response("send_text", {
            "reason": "调侃上一手公开下注", "message": "上把推得够猛"}))
        await self.agent.choose_social("new_hand")
        self.assertFalse(self.agent.reflection.done())
        self.agent.send.assert_awaited_once_with("CHAT_MESSAGE", {"message": "上把推得够猛"})

    async def test_ready_does_not_wait_for_social(self):
        self.agent.observe(snapshot(street="HAND_END"))
        self.agent.social_decision = asyncio.create_task(asyncio.sleep(1))
        await self.agent.ready_if_allowed()
        self.agent.send.assert_awaited_once_with("PLAYER_READY", {"ready": True})
        self.assertFalse(self.agent.social_decision.done())

    async def test_social_timeout_is_not_retried_until_next_hand(self):
        self.begin_next_hand()
        self.agent.ws = AsyncMock()
        self.agent.http.post = AsyncMock(side_effect=httpx.ReadTimeout("test timeout"))
        with patch("agent.SOCIAL_INTERVAL", .01):
            self.agent.social_decision = asyncio.create_task(self.agent.social_loop())
            await wait_until(lambda: self.agent.http.post.await_count == 1)
            await asyncio.sleep(.03)
            self.assertEqual(self.agent.http.post.await_count, 1)
            self.begin_next_hand(3)
            await wait_until(lambda: self.agent.http.post.await_count == 2)
        self.agent.send.assert_not_called()
        self.assertFalse(self.agent.social_decision.done())

    async def test_social_loop_wakes_only_for_new_hand(self):
        self.agent.ws = AsyncMock()
        self.agent.http.post = AsyncMock(return_value=model_response())
        self.agent.social_decision = asyncio.create_task(self.agent.social_loop())
        await asyncio.sleep(.02)
        self.agent.http.post.assert_not_called()
        self.agent.observe(snapshot(street="HAND_END"))
        await self.agent.advance()
        self.agent.http.post.assert_not_called()
        self.begin_next_hand()
        await wait_until(lambda: self.agent.http.post.await_count == 1)
        self.agent.send.assert_awaited_once_with("PLAYER_READY", {"ready": True})

    async def test_social_receives_only_previous_public_facts_and_shares_comment_reason(self):
        self.agent.chats.append({"hand": 1, "player_id": "villain", "message": "上一手交流"})
        self.begin_next_hand()
        self.agent.shared_plan = {"hand": 2, "plan": "本手秘密打法"}
        self.agent.analysis = {"equity": .99, "note": "本手秘密计算"}
        self.store.remember(self.agent.settings["server_url"], self.agent.settings["username"], "self", "内部记忆秘密")
        self.agent.chats.append({"hand": 2, "message": "本手聊天秘密"})
        self.agent.http.post = AsyncMock(return_value=model_response("send_text", {
            "message": "上把吓唬你的", "reason": "只调侃上一手，不谈当前牌"}))
        await self.agent.choose_social("new_hand")
        social = json.loads(self.agent.http.post.await_args.kwargs["json"]["messages"][1]["content"])
        self.assertEqual(social["previous_hand"]["hand"], 1)
        self.assertEqual(social["previous_hand"]["board"], ["2c", "4h", "7s"])
        self.assertEqual(social["previous_hand"]["players"][1]["shown_cards"], ["9h", "9d"])
        self.assertEqual(social["previous_hand"]["actions"][-1]["action"], "FOLD")
        self.assertEqual(social["previous_hand"]["results"][0]["net_profit"], -20)
        self.assertEqual(social["previous_chat"][0]["message"], "上一手交流")
        raw = json.dumps(social, ensure_ascii=False)
        for secret in ("As", "Ah", "Qs", "Qh", "未公开私牌", "本手秘密", "本手聊天", "内部记忆"):
            self.assertNotIn(secret, raw)
        self.assertEqual(set(social), {"hero_id", "previous_hand", "previous_chat", "trigger", "chat_cooldown_seconds"})
        activity = self.agent.context()["coordination"]["recent_activity"][-1]
        self.assertEqual(activity["commented_hand"], 1)
        self.assertEqual(activity["reason"], "只调侃上一手，不谈当前牌")
        self.assertEqual(self.agent.shared_plan["plan"], "本手秘密打法")

    async def test_stale_reflection_does_not_overwrite_current_plan(self):
        for change in ("action", "turn", "hand"):
            job = {"room_id": "test", "hand": self.agent.hand_number, "trigger": "loop",
                   "version": self.agent.coordination_version, "context": self.agent.context()}
            self.agent.shared_plan = {"hand": self.agent.hand_number, "plan": "当前计划"}
            if change == "action":
                self.agent.log("decision", hand=self.agent.hand_number, action="CHECK", reason="新行动")
            elif change == "turn":
                self.agent.room["table"]["turn_count"] += 1
            else:
                self.agent.observe(snapshot(hand=2))
                self.agent.shared_plan = {"hand": 2, "plan": "当前计划"}
            await self.agent.finish_reflection({"summary": "旧局面反思", "plan": "过时计划", "updates": [],
                "delete_subjects": []}, job, time.monotonic())
            self.assertEqual(self.agent.shared_plan["plan"], "当前计划", change)
            self.assertFalse(self.agent.last_reflection["plan_applied"], change)

    async def test_social_discards_reply_after_first_action_even_before_server_ack(self):
        self.begin_next_hand()
        async def post(*args, **kwargs):
            with patch("agent.random.uniform", return_value=0):
                await self.agent.execute("poker_action", {"action": "CHECK"}, self.agent.key(), time.monotonic())
            return model_response("send_text", {"message": "上把推得够猛", "reason": "上一手评论"})
        self.agent.http.post = AsyncMock(side_effect=post)
        await self.agent.choose_social("new_hand")
        self.agent.send.assert_awaited_once_with("PLAYER_ACTION", {"action": "CHECK", "amount": 0})
        self.assertEqual(self.store.log_tail()[-1]["kind"], "social_error")

    async def test_social_requests_once_and_cannot_send_after_window(self):
        self.begin_next_hand()
        self.agent.http.post = AsyncMock(return_value=model_response())
        await self.agent.choose_social("new_hand")
        await self.agent.choose_social("new_hand")
        self.agent.http.post.assert_awaited_once()
        self.agent.social_started -= 6
        await self.agent.apply_social({"message": "上把挺猛", "reason": "迟到评论"}, "test", 2, time.monotonic())
        self.agent.send.assert_not_called()

    async def test_public_chat_triggers_reply_with_only_frozen_history_and_profiles(self):
        server, account = self.agent.settings["server_url"], self.agent.settings["username"]
        self.store.remember(server, account, "self", "说话有点不服输")
        self.store.remember(server, account, "villain", "喜欢斗嘴")
        self.store.remember(server, account, "absent", "无关玩家秘密")
        self.store.remember(server, "other-account", "self", "其他账号秘密")
        self.agent.profiles = {"villain": {"observed_hands": 20, "style": "松凶"}}
        self.agent.observe(snapshot(hand=2, street="FLOP", history=[
            {"player_id": "villain", "action": "RAISE", "amount": 300, "street": "FLOP"}]))
        self.agent.room["table"]["board_cards"] = [{"notation": "Ks"}, {"notation": "Td"}, {"notation": "6h"}]
        self.store.remember(server, account, "self", "本手秘密战术")
        self.agent.profiles = {"villain": {"observed_hands": 21, "style": "本手新画像"}}
        self.agent.shared_plan = {"hand": 2, "plan": "当前共同计划秘密"}
        self.agent.analysis = {"equity": .99, "note": "当前计算秘密"}
        self.agent.log("decision", hand=2, action="CALL", reason="当前行动秘密")
        version = self.agent.coordination_version
        self.agent.ws = AsyncMock()
        self.agent.http.post = AsyncMock(return_value=model_response("send_text", {
            "message": "急啥，少套我话", "reason": "回嘴，不承诺行动"}))
        self.agent.social_decision = asyncio.create_task(self.agent.social_loop())
        self.agent.observe_chat("CHAT_MESSAGE", {"player_id": "villain", "name": "Villain", "message": "怂了吧"})
        await wait_until(lambda: self.agent.send.await_count == 1)
        payload = self.agent.http.post.await_args.kwargs["json"]
        context = json.loads(payload["messages"][1]["content"])
        self.assertEqual(context["trigger"], "chat")
        self.assertEqual(context["incoming_chat"]["message"], "怂了吧")
        self.assertEqual(context["recent_chat"][-1], context["incoming_chat"])
        self.assertEqual(context["memory_notes"], [
            {"subject": "villain", "note": "喜欢斗嘴"}, {"subject": "self", "note": "说话有点不服输"}])
        self.assertEqual(context["players"][1]["profile"]["style"], "松凶")
        self.assertEqual(set(context), {"hero_id", "incoming_chat", "recent_chat", "players", "memory_notes",
                                        "trigger", "chat_cooldown_seconds"})
        raw = json.dumps(context, ensure_ascii=False)
        for secret in ("As", "Ah", "Ks", "Td", "6h", "hole_cards", "chips", "street", "hand", "actions",
                       "本手秘密", "本手新画像", "当前共同", "当前计算", "当前行动", "无关玩家", "其他账号"):
            self.assertNotIn(secret, raw.replace("observed_hands", "observed").replace("complete_hands", "complete"))
        self.assertIn("禁止人机一样的发言", payload["messages"][0]["content"])
        self.agent.send.assert_awaited_once_with("CHAT_MESSAGE", {"message": "急啥，少套我话"})
        self.assertEqual(self.agent.coordination_version, version)
        self.assertEqual(self.agent.agent_activity[-1]["trigger"], "chat")
        self.assertNotIn("commented_hand", self.agent.last_social_decision)

    async def test_own_chat_emoji_and_empty_messages_do_not_trigger(self):
        for event, payload in [
            ("CHAT_MESSAGE", {"player_id": "hero", "message": "自己的回显"}),
            ("EMOJI_REACTION", {"player_id": "villain", "emoji": "smile"}),
            ("CHAT_MESSAGE", {"player_id": "villain", "message": "  "}),
            ("CHAT_MESSAGE", {"player_id": "villain", "message": None}),
            ("CHAT_MESSAGE", {"message": "系统消息"}),
        ]:
            self.agent.observe_chat(event, payload)
            self.assertIsNone(self.agent.pending_social_chat)
            self.assertFalse(self.agent.social_wake.is_set())
        self.agent.settings["social"] = False
        self.agent.observe_chat("CHAT_MESSAGE", {"player_id": "villain", "message": "关闭发言"})
        self.assertIsNone(self.agent.pending_social_chat)
        self.assertEqual(len(self.agent.chats), 6)

    async def test_chat_requests_are_serial_and_bursts_use_latest_message(self):
        entered, release = asyncio.Event(), asyncio.Event()
        requests, busy, maximum = [], 0, 0
        async def post(*args, **kwargs):
            nonlocal busy, maximum
            busy += 1
            maximum = max(maximum, busy)
            requests.append(json.loads(kwargs["json"]["messages"][1]["content"]))
            if len(requests) == 1:
                entered.set()
                await release.wait()
            busy -= 1
            return model_response()
        self.agent.ws = AsyncMock()
        self.agent.http.post = AsyncMock(side_effect=post)
        self.agent.social_decision = asyncio.create_task(self.agent.social_loop())
        self.agent.observe_chat("CHAT_MESSAGE", {"player_id": "villain", "message": "第一句"})
        await asyncio.wait_for(entered.wait(), 1)
        for message in ("第二句", "第三句"):
            self.agent.observe_chat("CHAT_MESSAGE", {"player_id": "villain", "message": message})
        release.set()
        await wait_until(lambda: len(requests) == 2)
        self.assertEqual(maximum, 1)
        self.assertEqual([r["incoming_chat"]["message"] for r in requests], ["第一句", "第三句"])
        self.assertEqual([c["message"] for c in requests[1]["recent_chat"]], ["第一句", "第二句", "第三句"])
        self.agent.send.assert_not_called()

    async def test_chat_reply_survives_hand_change_but_not_room_or_stop(self):
        self.agent.observe_chat("CHAT_MESSAGE", {"player_id": "villain", "message": "少装了"})
        async def post(*args, **kwargs):
            self.agent.observe(snapshot(hand=2, street="TURN"))
            return model_response("send_text", {"message": "你倒挺会说", "reason": "回应调侃"})
        self.agent.http.post = AsyncMock(side_effect=post)
        await self.agent.choose_social("chat")
        self.agent.send.assert_awaited_once_with("CHAT_MESSAGE", {"message": "你倒挺会说"})
        self.agent.send.reset_mock()
        args = {"message": "少装了", "reason": "回嘴"}
        await self.agent.apply_social(args, "other-room", 1, time.monotonic(), "chat")
        self.agent.running = False
        await self.agent.apply_social(args, "test", 1, time.monotonic(), "chat")
        self.agent.running = True
        self.agent.settings["social"] = False
        await self.agent.apply_social(args, "test", 1, time.monotonic(), "chat")
        self.agent.send.assert_not_called()

    async def test_chat_timeout_is_consumed_and_next_message_can_trigger(self):
        self.agent.ws = AsyncMock()
        self.agent.http.post = AsyncMock(side_effect=httpx.ReadTimeout("test timeout"))
        with patch("agent.SOCIAL_INTERVAL", .01):
            self.agent.social_decision = asyncio.create_task(self.agent.social_loop())
            self.agent.observe_chat("CHAT_MESSAGE", {"player_id": "villain", "message": "第一句"})
            await wait_until(lambda: self.agent.http.post.await_count == 1)
            await asyncio.sleep(.03)
            self.assertIsNone(self.agent.pending_social_chat)
            self.assertEqual(self.agent.http.post.await_count, 1)
            self.agent.observe_chat("CHAT_MESSAGE", {"player_id": "villain", "message": "第二句"})
            await wait_until(lambda: self.agent.http.post.await_count == 2)
        self.agent.send.assert_not_called()

    async def test_betting_executor_cannot_send_chat(self):
        self.begin_next_hand()
        with self.assertRaisesRegex(ValueError, "未知工具"):
            await self.agent.execute("send_text", {"message": "我跟"}, self.agent.key(), time.monotonic())
        self.agent.send.assert_not_called()

    async def test_chat_context_budget_keeps_latest_message_and_frozen_source(self):
        self.store.remember(self.agent.settings["server_url"], self.agent.settings["username"], "self", "历史印象" * 80)
        self.agent.observe(snapshot(hand=2, street="FLOP"))
        for message in ("公开聊天" * 100, "历史闲聊" * 100, "别装了"):
            self.agent.observe_chat("CHAT_MESSAGE", {"player_id": "villain", "message": message})
        self.agent.http.post = AsyncMock(return_value=model_response())
        with patch("agent.CONTEXT_BYTES", 1000):
            await self.agent.choose_social("chat")
        raw = self.agent.http.post.await_args.kwargs["json"]["messages"][1]["content"]
        self.assertLessEqual(len(raw.encode("utf-8")), 1000)
        self.assertEqual(json.loads(raw)["incoming_chat"]["message"], "别装了")
        self.assertEqual(len(self.agent.chats), 3)
        self.assertEqual(len(self.agent.social_background["memory_notes"]), 1)

    async def test_social_discards_delayed_reply_when_window_or_hand_changes(self):
        for change in ("time", "opponent_action", "street", "next_hand"):
            with self.subTest(change=change):
                self.begin_next_hand(self.agent.hand_number + 1)
                self.agent.send.reset_mock()
                async def post(*args, **kwargs):
                    if change == "time":
                        self.agent.social_started -= 6
                    elif change == "opponent_action":
                        self.agent.actions.append({"player_id": "villain", "action": "RAISE", "amount": 30,
                                                   "street": "PREFLOP"})
                    elif change == "street":
                        self.agent.room["table"]["street"] = "FLOP"
                    else:
                        self.begin_next_hand(self.agent.hand_number + 1)
                    return model_response("send_text", {"message": "上把挺猛", "reason": "上一手评论"})
                self.agent.http.post = AsyncMock(side_effect=post)
                await self.agent.choose_social("new_hand")
                self.agent.send.assert_not_called()

    async def test_social_context_budget_keeps_public_results_and_marks_omitted_actions(self):
        self.begin_next_hand()
        self.agent.social_context["previous_hand"]["actions"] *= 100
        self.agent.http.post = AsyncMock(return_value=model_response())
        with patch("agent.CONTEXT_BYTES", 1500):
            await self.agent.choose_social("new_hand")
        raw = self.agent.http.post.await_args.kwargs["json"]["messages"][1]["content"]
        context = json.loads(raw)
        self.assertLessEqual(len(raw.encode("utf-8")), 1500)
        self.assertGreater(context["previous_hand"]["omitted_older_actions"], 0)
        self.assertEqual(context["previous_hand"]["results"][0]["net_profit"], -20)
        self.assertEqual(len(self.agent.social_context["previous_hand"]["actions"]), 200)

    async def test_decision_refreshes_context_after_shared_plan_changes(self):
        requests = []
        async def post(*args, **kwargs):
            requests.append(json.loads(kwargs["json"]["messages"][1]["content"]))
            if len(requests) == 1:
                job = {"room_id": "test", "hand": 1, "trigger": "loop", "version": self.agent.coordination_version,
                       "context": self.agent.context()}
                await self.agent.finish_reflection({"summary": "更新共同策略", "plan": "慢打保持弱势形象",
                    "updates": [], "delete_subjects": []}, job, time.monotonic())
            return model_response("poker_action", {"action": "CHECK", "reason": "结合最新计划"})
        self.agent.http.post = AsyncMock(side_effect=post)
        with patch("agent.random.uniform", return_value=0):
            await self.agent.decide(self.agent.key())
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1]["coordination"]["shared_plan"]["plan"], "慢打保持弱势形象")
        self.agent.send.assert_awaited_once_with("PLAYER_ACTION", {"action": "CHECK", "amount": 0})

    async def test_chat_and_long_memory_do_not_restart_pending_decision(self):
        self.begin_next_hand()
        job = {"room_id": "test", "hand": 2, "trigger": "loop", "version": self.agent.coordination_version,
               "context": self.agent.context()}
        # A completed reflection from an earlier turn may update notes but cannot change this turn's plan.
        job["context"]["table"]["turn_count"] -= 1
        async def post(*args, **kwargs):
            await self.agent.apply_social({"message": "上把又来这招", "reason": "调侃，不承诺行动"}, "test", 2, time.monotonic())
            await self.agent.finish_reflection({"summary": "旧局面观察", "plan": "过时建议",
                "updates": [{"subject": "self", "note": "样本不足，不能当成随机全下范围"}],
                "delete_subjects": []}, job, time.monotonic())
            return model_response("poker_action", {"action": "CHECK", "reason": "当前选择"})
        self.agent.http.post = AsyncMock(side_effect=post)
        with patch("agent.random.uniform", return_value=0):
            await self.agent.decide(self.agent.key())
        self.agent.http.post.assert_awaited_once()
        self.assertEqual(self.agent.send.await_count, 2)
        self.assertFalse(self.agent.last_reflection["plan_applied"])
        self.assertEqual(self.agent.metrics["fallbacks"], 0)
        context = self.agent.context()
        self.assertTrue(any("样本不足" in n["note"] for n in context["memory_notes"]))
        self.assertTrue(any(a.get("reason") == "调侃，不承诺行动" for a in context["coordination"]["recent_activity"]))

    async def test_reflection_sees_full_hands_all_notes_and_private_agent_audit(self):
        actions = [{"player_id": "villain", "action": "RAISE", "amount": 30, "street": "PREFLOP"}]
        self.agent.observe(snapshot(street="HAND_END", history=actions))
        self.agent.observe(snapshot(hand=2))
        server, account = self.agent.settings["server_url"], self.agent.settings["username"]
        self.store.remember(server, account, "absent-player", "历史玩家观察")
        self.store.remember(server, "other-account", "other", "其他账号秘密")
        self.store.event("other-session", "social_decision", {"reason": "其他会话秘密"})
        self.agent.log("social_decision", hand=1, message="我有同花", reason="故意诈唬，其实没有同花",
                       results={"communication": {"submitted": True}})
        self.agent.http.post = AsyncMock(return_value=model_response("finish_reflection", {
            "summary": "依据完整记录复盘", "updates": [], "delete_subjects": []}))
        self.agent.ws = AsyncMock()
        self.agent.reflection = asyncio.create_task(self.agent.reflect())
        await wait_until(lambda: self.agent.last_reflection is not None)
        payload = json.loads(self.agent.http.post.await_args.kwargs["json"]["messages"][1]["content"])
        self.assertEqual(payload["memory_notes"][0]["subject"], "absent-player")
        self.assertNotIn("absent-player", {n["subject"] for n in self.agent.context()["memory_notes"]})
        self.assertEqual(payload["recent_hand_details"][0]["actions"][0]["player_id"], "villain")
        self.assertEqual(payload["agent_activity"][0]["data"]["reason"], "故意诈唬，其实没有同花")
        self.assertNotIn("其他账号秘密", json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("其他会话秘密", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(self.store.hand_details("other-server", "hero"), [])
        self.assertEqual(self.store.hand_details(server, "other-player"), [])

    def deep_call_room(self, hole=("Qs", "2s"), chips=1000, call=17430):
        room = snapshot()
        table = room["table"]
        table["big_blind"] = 20
        table["total_pot"] = 40 + call
        table["seats"][0].update(chips=chips, current_round_bet=20,
                                  hole_cards=[{"notation": card} for card in hole])
        table["seats"][1].update(chips=0, current_round_bet=20 + call, is_all_in=True)
        table["legal_actions"].update(can_check=False, can_call=True, call_amount=call,
                                      all_in_amount=chips + 20)
        return room

    async def test_allin_price_excludes_unmatched_opponent_chips(self):
        self.agent.observe(self.deep_call_room())
        context = self.agent.context()
        betting = self.agent.snapshot()["betting"]
        self.assertEqual(betting["call_amount"], 1000)
        self.assertEqual(betting["call_bb"], 50)
        self.assertEqual(betting["eligible_pot_after_call"], 2040)
        self.assertEqual(betting["excluded_opponent_excess"], 16430)
        self.assertAlmostEqual(betting["pot_odds"], 1000 / 2040)
        self.assertEqual(betting["effective_stacks_bb"]["villain"], 51)
        self.assertTrue(betting["call_is_all_in"])
        self.assertEqual(betting["spr_after_call"], 0)
        self.assertNotIn("betting", context)
        self.assertNotIn("pot_odds", context)
        self.assertEqual(context["table"]["legal_actions"]["call_amount"], 17430)
        self.assertEqual(context["players"][1]["current_round_bet"], 17450)

    async def test_old_computed_betting_data_does_not_return_through_history(self):
        old_betting = {"pot_odds": .05, "equity": .99, "outs": {"count": 9}, "call_bb": 50}
        self.agent.analysis = old_betting
        self.agent.hand_decisions.append({"action": "CALL", "reason": "过去的判断", "betting": old_betting})
        self.store.event("session", "decision", {"hand": 1, "action": "CALL", "reason": "历史判断", "betting": old_betting})
        self.agent.ws = AsyncMock()
        self.agent.http.post = AsyncMock(return_value=model_response("finish_reflection", {
            "summary": "重新依据原始局面复盘", "updates": [], "delete_subjects": []}))
        self.agent.reflection = asyncio.create_task(self.agent.reflect())
        await wait_until(lambda: self.agent.last_reflection is not None)
        payload = json.loads(self.agent.http.post.await_args.kwargs["json"]["messages"][1]["content"])
        for source in (self.agent.context(), payload):
            raw = json.dumps(source, ensure_ascii=False)
            for key in ("calculation", "betting", "pot_odds", "equity", "outs", "call_bb", "effective_stacks"):
                self.assertNotIn('"' + key + '":', raw)
        self.assertEqual(payload["agent_activity"][0]["data"]["reason"], "历史判断")
        self.assertEqual(payload["reflection"]["own_decisions"][0]["action"], "CALL")
        stored = next(e for e in self.store.log_tail() if e["kind"] == "decision")
        self.assertEqual(stored["data"]["betting"], old_betting)

    async def test_multiway_price_keeps_previous_pot_and_capped_folded_chips(self):
        room = snapshot(street="TURN")
        table = room["table"]
        table["total_pot"] = 1240
        table["legal_actions"].update(can_check=False, can_call=True, call_amount=180)
        table["seats"][0].update(current_round_bet=20)
        table["seats"][1].update(current_round_bet=500)
        table["seats"].extend([
            {"player_id": "short", "name": "Short", "seat_index": 2, "chips": 0, "current_round_bet": 70,
             "has_cards": True, "is_folded": False, "is_all_in": True},
            {"player_id": "folded", "name": "Folded", "seat_index": 3, "chips": 100, "current_round_bet": 450,
             "has_cards": True, "is_folded": True, "is_all_in": False}])
        self.agent.observe(room)
        betting = self.agent.betting_context()
        self.assertEqual(betting["eligible_pot_after_call"], 870)
        self.assertEqual(betting["excluded_opponent_excess"], 550)
        self.assertAlmostEqual(betting["pot_odds"], 180 / 870)

    async def test_legal_weak_preflop_calls_and_allins_are_not_intercepted(self):
        for hole in (("Qs", "2s"), ("Qh", "2s"), ("Jd", "5d")):
            self.agent.observe(self.deep_call_room(hole=hole))
            for action in ("CALL", "ALL_IN"):
                with self.subTest(hole=hole, action=action), patch("agent.random.uniform", return_value=0):
                    result, acted = await self.agent.execute("poker_action", {"action": action, "reason": "模型自主选择"},
                                                            self.agent.key(), time.monotonic())
                    self.assertTrue(result["submitted"])
                    self.assertTrue(acted)
        self.assertEqual(self.agent.send.await_count, 6)

    async def test_call_scale_keeps_bb_with_very_deep_remaining_stack(self):
        self.agent.observe(self.deep_call_room(chips=100000, call=1500))
        betting = self.agent.betting_context()
        self.assertAlmostEqual(betting["call_stack_fraction"], .015)
        self.assertEqual(betting["call_bb"], 75)

    async def test_call_scale_keeps_half_stack_fraction(self):
        self.agent.observe(self.deep_call_room(chips=800, call=400))
        betting = self.agent.betting_context()
        self.assertEqual(betting["call_bb"], 20)
        self.assertEqual(betting["call_stack_fraction"], .5)

    async def test_cheap_weak_call_and_premium_deep_call_remain_available(self):
        for hole, call in ((("Qs", "2s"), 20), (("As", "Ah"), 17430)):
            self.agent.observe(self.deep_call_room(hole=hole, call=call))
            with patch("agent.random.uniform", return_value=0):
                result, acted = await self.agent.execute("poker_action", {"action": "CALL", "reason": "根据范围判断"},
                                                        self.agent.key(), time.monotonic())
            self.assertTrue(result["submitted"])
            self.assertTrue(acted)
        self.assertEqual(self.agent.send.await_count, 2)

    async def test_model_owns_legal_choice_for_weak_deep_hand(self):
        for action, amount in (("CALL", 0), ("ALL_IN", 1020), ("FOLD", 0)):
            with self.subTest(action=action):
                self.agent.observe(self.deep_call_room())
                self.agent.analysis = {"equity": .99}
                self.agent.send.reset_mock()
                self.agent.http.post = AsyncMock(return_value=model_response("poker_action", {
                    "action": action, "reason": "模型独立判断范围与价格"}))
                with patch("agent.random.uniform", return_value=0):
                    await self.agent.decide(self.agent.key())
                self.agent.send.assert_awaited_once_with("PLAYER_ACTION", {"action": action, "amount": amount})
                self.agent.http.post.assert_awaited_once()
                self.assertEqual(self.agent.metrics["fallbacks"], 0)

    async def test_repeated_illegal_raises_still_fall_back(self):
        self.agent.observe(self.deep_call_room())
        self.agent.analysis = {"equity": .99}
        self.agent.http.post = AsyncMock(return_value=model_response("poker_action", {"action": "RAISE", "amount": 21,
                                                                                                  "reason": "非法步长"}))
        with patch("agent.random.uniform", return_value=0):
            await self.agent.decide(self.agent.key())
        self.agent.send.assert_awaited_once_with("PLAYER_ACTION", {"action": "FOLD", "amount": 0})
        self.assertEqual(self.agent.http.post.await_count, 3)
        self.assertEqual(self.agent.metrics["fallbacks"], 1)

    async def test_ready_send_failure_can_retry_without_social(self):
        self.agent.observe(snapshot(street="HAND_END"))
        self.agent.send = AsyncMock(side_effect=[ConnectionError("socket lost"), None])
        self.agent.http.post = AsyncMock()
        with self.assertRaises(ConnectionError):
            await self.agent.ready_if_allowed()
        await self.agent.ready_if_allowed()
        self.assertEqual(self.agent.send.await_count, 2)
        self.agent.http.post.assert_not_called()

    async def test_social_rejects_multiple_messages_before_sending(self):
        self.begin_next_hand()
        response = model_response("send_text", {"message": "上把挺猛", "reason": "调侃上一手"})
        data = response.json()
        data["choices"][0]["message"]["tool_calls"] *= 2
        self.agent.http.post = AsyncMock(return_value=httpx.Response(200, request=response.request, json=data))
        await self.agent.choose_social("new_hand")
        self.agent.send.assert_not_called()
        self.assertEqual(self.store.log_tail()[-1]["kind"], "social_error")

    async def test_only_previous_hand_text_tool_and_no_emoji_broadcast(self):
        self.assertEqual([t["function"]["name"] for t in SOCIAL_TOOLS], ["send_text"])
        for args in ({"reason": "交流", "message": "Hi", "emoji": "👍"},
                     {"reason": "交流", "message": "Hi 👍"}):
            with self.assertRaises(ValueError):
                await self.agent.apply_social(args, "test", 1, time.monotonic())
        self.agent.send.assert_not_called()

    async def test_stop_leaves_even_if_socket_is_down(self):
        self.agent.api = AsyncMock(side_effect=[snapshot(), {"success": True}])
        await self.agent.stop()
        self.assertFalse(self.agent.running)
        self.assertFalse(self.agent.stopping)
        self.assertIsNone(self.agent.room_id)
        self.assertEqual(self.agent.api.await_args_list[1].args, ("POST", "/api/rooms/test/leave"))

    async def test_allin_departure_is_not_reported_as_completed(self):
        self.agent.api = AsyncMock(side_effect=[snapshot(), RemoteError("全下不能离座", 409)])
        await self.agent.stop()
        self.assertFalse(self.agent.running)
        self.assertTrue(self.agent.stopping)
        self.assertEqual(self.agent.room_id, "test")
        self.agent.api = AsyncMock(side_effect=[snapshot(street="HAND_END"), {"success": True}])
        await self.agent.try_leave()
        self.assertFalse(self.agent.stopping)

    def test_token_tracking_and_snapshot(self):
        initial_total = self.agent.snapshot()["tokens"]["total"]["total_tokens"]
        self.assertEqual(self.agent.snapshot()["tokens"]["session"]["total_tokens"], 0)
        self.assertEqual(self.agent.snapshot()["tokens"]["hand"]["total_tokens"], 0)

        usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                 "completion_tokens_details": {"reasoning_tokens": 15}}
        self.agent.record_usage(usage)

        tokens = self.agent.snapshot()["tokens"]
        self.assertEqual(tokens["session"]["prompt_tokens"], 100)
        self.assertEqual(tokens["session"]["completion_tokens"], 20)
        self.assertEqual(tokens["session"]["reasoning_tokens"], 15)
        self.assertEqual(tokens["session"]["total_tokens"], 120)
        self.assertEqual(tokens["hand"]["total_tokens"], 120)
        self.assertEqual(tokens["total"]["total_tokens"], initial_total + 120)

        # Observe next hand should reset hand_tokens but keep session_tokens
        self.agent.observe(snapshot(hand=2))
        self.assertEqual(self.agent.snapshot()["tokens"]["hand"]["total_tokens"], 0)
        self.assertEqual(self.agent.snapshot()["tokens"]["session"]["total_tokens"], 120)


class ApiTests(unittest.TestCase):
    def test_public_defaults_and_local_configuration(self):
        defaults = app.Settings().model_dump()
        example = json.loads((app.ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(example, defaults)
        self.assertEqual(defaults["server_url"], "https://poker.example.com")
        self.assertEqual(defaults["model_url"], "http://127.0.0.1:8000/v1")
        self.assertEqual((defaults["password"], defaults["api_key"]), ("", ""))
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.local.json"
            with patch.object(app, "CONFIG_PATH", config), \
                    patch.dict("os.environ", {"POKER_PASSWORD": "", "POKER_MODEL_KEY": ""}):
                self.assertEqual(app.read_settings(), defaults)
                local = {"server_url": "https://table.example.com", "model_url": "http://127.0.0.1:9000/v1",
                         "username": "local-player", "password": "local-password", "api_key": "local-api-key"}
                config.write_text(json.dumps(local), encoding="utf-8")
                self.assertEqual(app.read_settings(), {**defaults, **local})
                with patch.dict("os.environ", {"POKER_PASSWORD": "env-password", "POKER_MODEL_KEY": "env-api-key"}):
                    self.assertEqual(app.read_settings(), {**defaults, **local, "password": "env-password",
                                                          "api_key": "env-api-key"})

    def test_secrets_validation_and_cross_site_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.local.json"
            config.write_text(json.dumps({"password": "secret-password", "api_key": "secret-api-key"}), encoding="utf-8")
            with patch.object(app, "ROOT", Path(directory)), patch.object(app, "CONFIG_PATH", config), TestClient(app.app) as client:
                response = client.get("/api/settings")
                self.assertNotIn("secret-password", response.text)
                self.assertNotIn("secret-api-key", response.text)
                self.assertTrue(response.json()["settings"]["has_password"])
                response = client.post("/api/stop", headers={"Origin": "https://evil.example"})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(client.get("/api/status", headers={"Host": "evil.example"}).status_code, 403)
                status_res = client.get("/api/status")
                self.assertEqual(status_res.status_code, 200)
                self.assertIn("tokens", status_res.json())
                self.assertIn("session", status_res.json()["tokens"])
                self.assertIn("total", status_res.json()["tokens"])
                response = client.put("/api/settings", json={"style": "mixed", "mix_weights": {"tag": 0},
                                                              "password": "secret-password", "api_key": "secret-api-key"})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("secret-password", response.text)
                self.assertNotIn("secret-api-key", response.text)
                self.assertEqual(client.post("/api/calculate", json={"hole": ["As", "As"]}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
