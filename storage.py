"""Session evidence and observed player statistics, without credentials."""

import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, timestamp REAL, session TEXT, kind TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS hands (
                server TEXT, room TEXT, number INTEGER, timestamp REAL, data TEXT,
                PRIMARY KEY(server, room, number));
            CREATE TABLE IF NOT EXISTS player_hands (
                server TEXT, room TEXT, number INTEGER, player TEXT, name TEXT,
                vpip INTEGER, pfr INTEGER, aggressive INTEGER, passive INTEGER,
                folds INTEGER, profit INTEGER, complete INTEGER,
                PRIMARY KEY(server, room, number, player));
            CREATE TABLE IF NOT EXISTS memories (
                server TEXT, account TEXT, subject TEXT, note TEXT, updated REAL,
                PRIMARY KEY(server, account, subject));
        """)

    def event(self, session, kind, data):
        with self.db:
            cursor = self.db.execute("INSERT INTO events(timestamp,session,kind,data) VALUES(?,?,?,?)",
                                     (time.time(), session, kind, json.dumps(data, ensure_ascii=False)))
        return cursor.lastrowid

    def log_tail(self, limit=80):
        rows = self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in reversed(rows)]

    def record_hand(self, server, room, table, actions, complete):
        results = {r["player_id"]: r for r in table.get("hand_results", [])}
        number = table["hand_number"]
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO hands VALUES(?,?,?,?,?)", (
                server, room, number, time.time(), json.dumps({"table": table, "actions": actions,
                                                             "complete_observation": complete}, ensure_ascii=False)))
            for seat in table.get("seats", []):
                if not seat or not seat.get("has_cards"):
                    continue
                player = seat["player_id"]
                moves = [a for a in actions if a.get("player_id") == player]
                pre = [a for a in moves if a.get("street") == "PREFLOP"]
                self.db.execute("INSERT OR IGNORE INTO player_hands VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                    server, room, number, player, seat["name"],
                    int(any(a["action"] in ("CALL", "BET", "RAISE", "ALL_IN") for a in pre)),
                    int(any(a["action"] in ("BET", "RAISE") or
                            (a["action"] == "ALL_IN" and a.get("aggressive", False)) for a in pre)),
                    sum(a["action"] in ("BET", "RAISE") or
                        (a["action"] == "ALL_IN" and a.get("aggressive", False)) for a in moves),
                    sum(a["action"] == "CALL" or
                        (a["action"] == "ALL_IN" and not a.get("aggressive", False)) for a in moves),
                    sum(a["action"] == "FOLD" for a in moves),
                    results.get(player, {}).get("net_profit", 0), int(complete)))

    def profiles(self, server, room=None):
        rows = self.db.execute("""SELECT player, MAX(name) AS name, COUNT(*) AS observed_hands,
            SUM(complete) AS complete_hands, SUM(CASE WHEN complete THEN vpip ELSE 0 END) AS vpip,
            SUM(CASE WHEN complete THEN pfr ELSE 0 END) AS pfr, SUM(aggressive) AS aggressive,
            SUM(passive) AS passive, SUM(folds) AS folds, SUM(profit) AS net_chips
            FROM player_hands WHERE server=? AND (? IS NULL OR room=?) GROUP BY player""",
            (server, room, room)).fetchall()
        profiles = {}
        for row in rows:
            p = dict(row)
            count = p["complete_hands"]
            p["vpip_pct"] = round(100 * p["vpip"] / count, 1) if count else None
            p["pfr_pct"] = round(100 * p["pfr"] / count, 1) if count else None
            p["aggression_factor"] = round(p["aggressive"] / p["passive"], 2) if p["passive"] else None
            if count < 10:
                p["style"] = "样本不足"
            elif p["vpip_pct"] < 22:
                p["style"] = "紧凶" if p["pfr_pct"] >= 15 else "紧弱"
            else:
                p["style"] = "松凶" if p["pfr_pct"] >= 22 else "松弱"
            profiles[p["player"]] = p
        return profiles

    def history(self, limit=30):
        rows = self.db.execute("SELECT * FROM hands ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def remember(self, server, account, subject, note):
        with self.db:
            self.db.execute("INSERT INTO memories VALUES(?,?,?,?,?) ON CONFLICT(server,account,subject) "
                            "DO UPDATE SET note=excluded.note, updated=excluded.updated",
                            (server, account, subject, note, time.time()))

    def memories(self, server, account):
        return [dict(row) for row in self.db.execute(
            "SELECT subject,note,updated FROM memories WHERE server=? AND account=? ORDER BY updated DESC",
            (server, account)).fetchall()]

    def revise_memories(self, server, account, updates, deletions):
        with self.db:
            for subject in deletions:
                self.db.execute("DELETE FROM memories WHERE server=? AND account=? AND subject=?",
                                (server, account, subject))
            for item in updates:
                self.db.execute("INSERT INTO memories VALUES(?,?,?,?,?) ON CONFLICT(server,account,subject) "
                                "DO UPDATE SET note=excluded.note, updated=excluded.updated",
                                (server, account, item["subject"], item["note"], time.time()))

    def hand_details(self, server, player):
        rows = self.db.execute("""SELECT h.room,h.number,h.data FROM hands h JOIN player_hands p
            ON h.server=p.server AND h.room=p.room AND h.number=p.number
            WHERE p.server=? AND p.player=? ORDER BY h.timestamp DESC LIMIT 10""", (server, player)).fetchall()
        return [{"room": row["room"], "hand": row["number"], **json.loads(row["data"])} for row in rows]

    def session_activity(self, session):
        rows = self.db.execute("""SELECT timestamp,kind,data FROM events WHERE session=?
            AND kind IN ('decision','fallback','table_decision','social_decision','reflection')
            ORDER BY id DESC LIMIT 60""", (session,)).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in reversed(rows)]

    def recent_hands(self, server, player, relevant_players, limit=20):
        rows = self.db.execute("""SELECT h.room,h.number,h.data FROM hands h JOIN player_hands p
            ON h.server=p.server AND h.room=p.room AND h.number=p.number
            WHERE p.server=? AND p.player=? ORDER BY h.timestamp DESC LIMIT ?""", (server, player, limit)).fetchall()
        summaries = []
        for row in rows:
            data = json.loads(row["data"])
            table = data["table"]
            hero = next((p for p in table.get("seats", []) if p and p["player_id"] == player), {})
            moves = [{k: a.get(k) for k in ("action", "amount", "street")}
                     for a in data["actions"] if a["player_id"] == player and a["action"] not in ("POST_SB", "POST_BB")]
            summaries.append({"room": row["room"], "hand": row["number"],
                              "complete_observation": data["complete_observation"],
                              "board": [c["notation"] for c in table.get("board_cards", [])],
                              "hero_cards": [c["notation"] for c in hero.get("hole_cards", [])],
                              "hero_actions": moves,
                              "results": [{k: r.get(k) for k in ("player_id", "name", "net_profit", "hand_desc")}
                                          for r in table.get("hand_results", []) if r["player_id"] in relevant_players]})
        return summaries

    def token_summary(self):
        row = self.db.execute("""SELECT
            COUNT(*) AS calls,
            SUM(COALESCE(json_extract(data, '$.prompt_tokens'), 0)) AS prompt_tokens,
            SUM(COALESCE(json_extract(data, '$.completion_tokens'), 0)) AS completion_tokens,
            SUM(COALESCE(json_extract(data, '$.reasoning_tokens'), 0)) AS reasoning_tokens
            FROM events WHERE kind='model_call'""").fetchone()
        calls = row["calls"] or 0
        prompt = row["prompt_tokens"] or 0
        completion = row["completion_tokens"] or 0
        reasoning = row["reasoning_tokens"] or 0
        return {
            "calls": calls,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "reasoning_tokens": reasoning,
            "total_tokens": prompt + completion
        }
