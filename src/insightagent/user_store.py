from __future__ import annotations

import asyncio
import json
from typing import List
from uuid import uuid4

from .contracts import utc_now
from .persistence import SQLiteDatabase
from .user_contracts import DIMS, NONE, UserIntent, UserPreference, UserUtterance
from .user_intent import slot_of

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
                if slot == NONE:
                    continue
                trigger = slot[:16]
                title = slot[:16]
                statement = slot[:80]
                connection.execute(
                    """
                    UPDATE user_preferences
                    SET status = 'retired', updated_at = ?
                    WHERE user_id = ? AND scope = ? AND trigger = ?
                      AND status = 'active'
                    """,
                    (now, utterance.user_id, dim, trigger),
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
