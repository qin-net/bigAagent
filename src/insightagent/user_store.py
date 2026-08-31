from __future__ import annotations

import asyncio
import json
from typing import List, Optional
from uuid import uuid4

from .contracts import utc_now
from .persistence import SQLiteDatabase
from .user_contracts import DIMS, NONE, UserIntent, UserPreference, UserUtterance
from .user_intent import MAX_PREF_CHARS, passes_memory_gate, slot_of

REMEMBER_EFFECTS = {"remember", "remember_rerun"}
MAX_PREF = 8


class UserStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    async def save_utterance(self, row: UserUtterance) -> None:
        await self.database.initialize()
        await asyncio.to_thread(self._save_utterance_sync, row)

    async def save_intent(self, row: UserIntent) -> None:
        await self.database.initialize()
        await asyncio.to_thread(self._save_intent_sync, row)

    async def save_preference(self, row: UserPreference) -> None:
        await self.database.initialize()
        await asyncio.to_thread(self._save_preference_sync, row)

    async def persist_remember(
        self,
        *,
        intent: UserIntent,
        utterance: UserUtterance,
    ) -> None:
        if intent.effect not in REMEMBER_EFFECTS:
            return
        await self.database.initialize()
        await asyncio.to_thread(
            self._persist_remember_sync, intent, utterance
        )

    async def intent_for_run_moment(
        self,
        *,
        run_id: str,
        moment: str,
        effect: str = "this_run",
    ) -> Optional[UserIntent]:
        await self.database.initialize()
        return await asyncio.to_thread(
            self._intent_for_run_moment_sync, run_id, moment, effect
        )

    def _intent_for_run_moment_sync(
        self, run_id: str, moment: str, effect: str
    ) -> Optional[UserIntent]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT i.intent_id, i.utterance_id, i.effect, i.tags,
                       i.fundamental, i.technical, i.sentiment, i.macro,
                       i.decision, i.tracking, i.not_evidence,
                       i.schema_version, i.created_at
                FROM user_utterances AS u
                JOIN user_intents AS i ON i.intent_id = u.intent_id
                WHERE u.run_id = ? AND u.moment = ? AND u.effect = ?
                ORDER BY u.created_at DESC
                LIMIT 1
                """,
                (run_id, moment, effect),
            ).fetchone()
            return UserIntent.model_validate(dict(row)) if row else None
        finally:
            connection.close()

    async def active_preferences(
        self,
        *,
        user_id: str,
        scope: str,
        stock_code: str,
    ) -> List[UserPreference]:
        await self.database.initialize()
        return await asyncio.to_thread(
            self._active_preferences_sync, user_id, scope, stock_code
        )

    async def profile(self, *, user_id: str, stock_code: str = NONE) -> dict:
        await self.database.initialize()
        return await asyncio.to_thread(self._profile_sync, user_id, stock_code)

    async def retire_preference(self, *, user_id: str, preference_id: str) -> bool:
        await self.database.initialize()
        return await asyncio.to_thread(
            self._retire_preference_sync, user_id, preference_id
        )

    async def save_generated_profile(
        self, *, user_id: str, model: str, payload: dict
    ) -> dict:
        await self.database.initialize()
        return await asyncio.to_thread(
            self._save_generated_profile_sync, user_id, model, payload
        )

    async def latest_generated_profile(
        self, *, user_id: str
    ) -> Optional[dict]:
        await self.database.initialize()
        return await asyncio.to_thread(
            self._latest_generated_profile_sync, user_id
        )

    def _save_utterance_sync(self, row: UserUtterance) -> None:
        connection = self.database.connect()
        try:
            connection.execute(
                """
                INSERT INTO user_utterances(
                    utterance_id, user_id, moment, effect, tags, intent_id,
                    stock_code, thesis_id, run_id, schema_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.utterance_id,
                    row.user_id,
                    row.moment,
                    row.effect,
                    row.tags,
                    row.intent_id,
                    row.stock_code,
                    row.thesis_id,
                    row.run_id,
                    row.schema_version,
                    row.created_at,
                ),
            )
        finally:
            connection.close()

    def _save_intent_sync(self, row: UserIntent) -> None:
        connection = self.database.connect()
        try:
            connection.execute(
                """
                INSERT INTO user_intents(
                    intent_id, utterance_id, effect, tags,
                    fundamental, technical, sentiment, macro,
                    decision, tracking, not_evidence,
                    schema_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.intent_id,
                    row.utterance_id,
                    row.effect,
                    row.tags,
                    row.fundamental,
                    row.technical,
                    row.sentiment,
                    row.macro,
                    row.decision,
                    row.tracking,
                    row.not_evidence,
                    row.schema_version,
                    row.created_at,
                ),
            )
        finally:
            connection.close()

    def _save_preference_sync(self, row: UserPreference) -> None:
        connection = self.database.connect()
        try:
            self._insert_preference(connection, row)
        finally:
            connection.close()

    def _persist_remember_sync(
        self, intent: UserIntent, utterance: UserUtterance
    ) -> None:
        connection = self.database.connect()
        now = utc_now().isoformat()
        try:
            for dim in DIMS:
                slot = slot_of(intent, dim)
                if slot == NONE or not passes_memory_gate(slot):
                    continue
                trigger = slot[:16]
                title = slot[:16]
                statement = slot[:MAX_PREF_CHARS]
                active_rows = connection.execute(
                    """
                    SELECT p.preference_id, v.payload_json
                    FROM user_preferences AS p
                    JOIN user_preference_versions AS v
                      ON v.preference_id = p.preference_id
                     AND v.version = p.current_version
                    WHERE p.user_id = ? AND p.scope = ? AND p.status = 'active'
                    """,
                    (utterance.user_id, dim),
                ).fetchall()
                for active in active_rows:
                    payload = json.loads(active["payload_json"])
                    if payload.get("statement") == statement:
                        connection.execute(
                            """
                            UPDATE user_preferences
                            SET status = 'retired', updated_at = ?
                            WHERE preference_id = ?
                            """,
                            (now, active["preference_id"]),
                        )
                row = UserPreference(
                    preference_id=str(uuid4()),
                    user_id=utterance.user_id,
                    status="active",
                    current_version="1",
                    kind="constraint",
                    scope=dim,
                    stock_code=utterance.stock_code,
                    trigger=trigger,
                    title=title,
                    statement=statement,
                    source="user_feedback",
                    source_utterance_id=utterance.utterance_id,
                    source_run_id=utterance.run_id,
                    created_at=now,
                    updated_at=now,
                )
                self._insert_preference(connection, row)
        finally:
            connection.close()

    def _insert_preference(
        self, connection, row: UserPreference
    ) -> None:
        connection.execute(
            """
            INSERT INTO user_preferences(
                preference_id, user_id, status, current_version, kind,
                scope, stock_code, trigger, title, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.preference_id,
                row.user_id,
                row.status,
                row.current_version,
                row.kind,
                row.scope,
                row.stock_code,
                row.trigger,
                row.title,
                row.created_at,
                row.updated_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO user_preference_versions(
                preference_id, version, payload_json, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                row.preference_id,
                row.current_version,
                json.dumps(row.model_dump(), ensure_ascii=False),
                row.created_at,
            ),
        )

    def _active_preferences_sync(
        self, user_id: str, scope: str, stock_code: str
    ) -> List[UserPreference]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT p.preference_id, v.payload_json
                FROM user_preferences AS p
                JOIN user_preference_versions AS v
                  ON v.preference_id = p.preference_id
                 AND v.version = p.current_version
                WHERE p.user_id = ?
                  AND p.status = 'active'
                  AND p.scope = ?
                  AND (p.stock_code = ? OR p.stock_code = ?)
                ORDER BY p.updated_at DESC
                LIMIT ?
                """,
                (user_id, scope, stock_code, NONE, MAX_PREF),
            ).fetchall()
            return [
                UserPreference.model_validate(json.loads(row["payload_json"]))
                for row in rows
            ]
        finally:
            connection.close()

    def _retire_preference_sync(self, user_id: str, preference_id: str) -> bool:
        connection = self.database.connect()
        now = utc_now().isoformat()
        try:
            cursor = connection.execute(
                """
                UPDATE user_preferences
                SET status = 'retired', updated_at = ?
                WHERE preference_id = ? AND user_id = ? AND status = 'active'
                """,
                (now, preference_id, user_id),
            )
            return cursor.rowcount > 0
        finally:
            connection.close()

    def _save_generated_profile_sync(
        self, user_id: str, model: str, payload: dict
    ) -> dict:
        connection = self.database.connect()
        profile_id = str(uuid4())
        created_at = utc_now().isoformat()
        stored = {
            **payload,
            "profile_id": profile_id,
            "model": model,
            "generated_at": created_at,
        }
        try:
            connection.execute(
                """
                INSERT INTO user_profile_snapshots(
                    profile_id, user_id, model, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    user_id,
                    model,
                    json.dumps(stored, ensure_ascii=False),
                    created_at,
                ),
            )
            return stored
        finally:
            connection.close()

    def _latest_generated_profile_sync(
        self, user_id: str
    ) -> Optional[dict]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT payload_json
                FROM user_profile_snapshots
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None
        finally:
            connection.close()

    def _profile_sync(self, user_id: str, stock_code: str) -> dict:
        connection = self.database.connect()
        try:
            effects = {
                row["effect"]: row["n"]
                for row in connection.execute(
                    "SELECT effect, COUNT(*) AS n FROM user_utterances WHERE user_id=? GROUP BY effect",
                    (user_id,),
                )
            }
            moments = {
                row["moment"]: row["n"]
                for row in connection.execute(
                    "SELECT moment, COUNT(*) AS n FROM user_utterances WHERE user_id=? GROUP BY moment",
                    (user_id,),
                )
            }
            tags: dict[str, int] = {}
            for row in connection.execute(
                "SELECT tags FROM user_utterances WHERE user_id=?",
                (user_id,),
            ):
                try:
                    items = json.loads(row["tags"])
                except json.JSONDecodeError:
                    items = []
                if not isinstance(items, list):
                    items = [items]
                for tag in items:
                    if not tag or tag == NONE:
                        continue
                    tags[str(tag)] = tags.get(str(tag), 0) + 1
            dim_counts = {dim: 0 for dim in DIMS}
            for row in connection.execute(
                """
                SELECT i.fundamental, i.technical, i.sentiment, i.macro, i.decision, i.tracking
                FROM user_intents AS i
                JOIN user_utterances AS u ON u.intent_id = i.intent_id
                WHERE u.user_id = ?
                """,
                (user_id,),
            ):
                for dim in DIMS:
                    if row[dim] and row[dim] != NONE:
                        dim_counts[dim] += 1
            stocks = [
                {"stock_code": row["stock_code"], "count": row["n"]}
                for row in connection.execute(
                    """
                    SELECT stock_code, COUNT(*) AS n FROM user_utterances
                    WHERE user_id=? GROUP BY stock_code
                    ORDER BY n DESC, stock_code ASC LIMIT 8
                    """,
                    (user_id,),
                )
            ]
            pref_rows = connection.execute(
                """
                SELECT p.preference_id, v.payload_json
                FROM user_preferences AS p
                JOIN user_preference_versions AS v
                  ON v.preference_id = p.preference_id
                 AND v.version = p.current_version
                WHERE p.user_id = ? AND p.status = 'active'
                ORDER BY p.updated_at DESC
                """,
                (user_id,),
            ).fetchall()
            preferences = []
            scope_counts: dict[str, int] = {}
            for row in pref_rows:
                item = UserPreference.model_validate(json.loads(row["payload_json"]))
                if stock_code != NONE and item.stock_code not in {stock_code, NONE}:
                    continue
                preferences.append(
                    {
                        "preference_id": item.preference_id,
                        "scope": item.scope,
                        "stock_code": item.stock_code,
                        "kind": item.kind,
                        "statement": item.statement,
                        "updated_at": item.updated_at,
                    }
                )
                scope_counts[item.scope] = scope_counts.get(item.scope, 0) + 1
            utterance_count = sum(effects.values())
            return {
                "user_id": user_id,
                "utterance_count": utterance_count,
                "effects": effects,
                "moments": moments,
                "tags": tags,
                "dims": dim_counts,
                "stocks": stocks,
                "preferences": preferences,
                "highlights": _profile_highlights(
                    effects, tags, dim_counts, scope_counts, utterance_count
                ),
            }
        finally:
            connection.close()


def _profile_highlights(
    effects: dict,
    tags: dict,
    dim_counts: dict,
    scope_counts: dict,
    utterance_count: int,
) -> list[str]:
    names = {
        "fundamental": "基本面",
        "technical": "技术面",
        "sentiment": "情绪",
        "macro": "宏观",
        "decision": "决策",
        "tracking": "追踪",
    }
    lines: list[str] = []
    remember = effects.get("remember", 0) + effects.get("remember_rerun", 0)
    if utterance_count and remember / utterance_count >= 0.3:
        lines.append("常 #remember")
    if scope_counts:
        top_scope = max(scope_counts, key=lambda key: (scope_counts[key], key))
        lines.append("常约束" + names.get(top_scope, top_scope))
    tagged = {key: value for key, value in tags.items() if key in names}
    if tagged:
        top_tag = max(tagged, key=lambda key: (tagged[key], key))
        lines.append("常点名" + names[top_tag])
    elif any(dim_counts.values()):
        top_dim = max(dim_counts, key=lambda key: (dim_counts[key], key))
        if dim_counts[top_dim]:
            lines.append("常写" + names[top_dim] + "槽")
    return lines[:4]
