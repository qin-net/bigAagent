from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from .business_contracts import Decision, Report, RunRecord
from .contracts import utc_now
from .persistence import SQLiteDatabase, _rollback_if_needed


class ResearchStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    async def save_run(self, record: RunRecord) -> None:
        await self.database.initialize()
        await asyncio.to_thread(self._save_run_sync, record)

    def _save_run_sync(self, record: RunRecord) -> None:
        connection = self.database.connect()
        payload = record.model_dump(mode="json")
        try:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?",
                (record.run_id,),
            ).fetchone()
            if exists:
                connection.execute(
                    """
                    UPDATE runs
                    SET stock_code=?, thesis_id=?, mode=?, status=?,
                        schema_version=?, payload_json=?, updated_at=?
                    WHERE run_id=?
                    """,
                    (
                        record.stock_code,
                        record.thesis_id,
                        record.mode,
                        record.status,
                        record.schema_version,
                        json.dumps(payload, ensure_ascii=False),
                        record.updated_at.isoformat(),
                        record.run_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, stock_code, thesis_id, mode, status,
                        schema_version, payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.run_id,
                        record.stock_code,
                        record.thesis_id,
                        record.mode,
                        record.status,
                        record.schema_version,
                        json.dumps(payload, ensure_ascii=False),
                        record.created_at.isoformat(),
                        record.updated_at.isoformat(),
                    ),
                )
            connection.execute("COMMIT")
        except Exception:
            _rollback_if_needed(connection)
            raise
        finally:
            connection.close()

    async def save_report(
        self, run_id: str, agent_name: str, report: Report
    ) -> str:
        await self.database.initialize()
        report_id = "{}-{}".format(run_id, agent_name)
        await asyncio.to_thread(
            self._save_child_sync,
            "reports",
            report_id,
            run_id,
            agent_name,
            report.schema_version,
            report.model_dump(mode="json"),
        )
        return report_id

    async def save_decision(self, run_id: str, decision: Decision) -> str:
        await self.database.initialize()
        decision_id = "{}-decision".format(run_id)
        await asyncio.to_thread(
            self._save_decision_sync,
            decision_id,
            run_id,
            decision,
        )
        return decision_id

    def _save_child_sync(
        self,
        table: str,
        record_id: str,
        run_id: str,
        agent_name: str,
        schema_version: str,
        payload: Dict[str, Any],
    ) -> None:
        connection = self.database.connect()
        try:
            connection.execute(
                """
                INSERT OR REPLACE INTO reports(
                    report_id, run_id, agent_name, schema_version,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    run_id,
                    agent_name,
                    schema_version,
                    json.dumps(payload, ensure_ascii=False),
                    utc_now().isoformat(),
                ),
            )
        finally:
            connection.close()

    def _save_decision_sync(
        self, decision_id: str, run_id: str, decision: Decision
    ) -> None:
        connection = self.database.connect()
        try:
            connection.execute(
                """
                INSERT OR REPLACE INTO decisions(
                    decision_id, run_id, schema_version, payload_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    run_id,
                    decision.schema_version,
                    decision.model_dump_json(),
                    utc_now().isoformat(),
                ),
            )
        finally:
            connection.close()

    async def get_intent_for_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        await self.database.initialize()
        return await asyncio.to_thread(self._get_intent_for_run_sync, run_id)

    def _get_intent_for_run_sync(self, run_id: str) -> Optional[Dict[str, Any]]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT i.effect, i.fundamental, i.technical, i.sentiment,
                       i.macro, i.decision, i.tracking, i.not_evidence,
                       u.created_at
                FROM user_intents i
                JOIN user_utterances u ON u.intent_id = i.intent_id
                WHERE u.run_id = ?
                ORDER BY u.created_at DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    async def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        await self.database.initialize()
        return await asyncio.to_thread(self._get_run_sync, run_id)

    def _pack_run(self, connection, run) -> Dict[str, Any]:
        reports = connection.execute(
            "SELECT * FROM reports WHERE run_id = ?",
            (run["run_id"],),
        ).fetchall()
        decisions = connection.execute(
            "SELECT * FROM decisions WHERE run_id = ?",
            (run["run_id"],),
        ).fetchall()
        return {
            "run": json.loads(run["payload_json"]),
            "status": run["status"],
            "reports": [json.loads(row["payload_json"]) for row in reports],
            "decisions": [json.loads(row["payload_json"]) for row in decisions],
        }

    def _get_run_sync(self, run_id: str) -> Optional[Dict[str, Any]]:
        connection = self.database.connect()
        try:
            run = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not run:
                return None
            return self._pack_run(connection, run)
        finally:
            connection.close()

    async def latest_track_run(self, thesis_id: str) -> Optional[Dict[str, Any]]:
        await self.database.initialize()
        return await asyncio.to_thread(self._latest_track_run_sync, thesis_id)

    def _latest_track_run_sync(self, thesis_id: str) -> Optional[Dict[str, Any]]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM runs
                WHERE thesis_id = ? AND mode = 'track_day'
                  AND status IN ('success', 'degraded')
                ORDER BY created_at DESC LIMIT 1
                """,
                (thesis_id,),
            ).fetchone()
            if not row:
                return None
            return self._pack_run(connection, row)
        finally:
            connection.close()

    async def get_baseline_run(self, thesis_or_run_id: str) -> Optional[Dict[str, Any]]:
        await self.database.initialize()
        return await asyncio.to_thread(
            self._get_baseline_run_sync, thesis_or_run_id
        )

    def _get_baseline_run_sync(self, thesis_or_run_id: str) -> Optional[Dict[str, Any]]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (thesis_or_run_id,),
            ).fetchone()
            if row:
                return self._pack_run(connection, row)
            row = connection.execute(
                """
                SELECT * FROM runs
                WHERE thesis_id = ? AND mode = 'research'
                  AND status IN ('success', 'degraded')
                ORDER BY created_at DESC LIMIT 1
                """,
                (thesis_or_run_id,),
            ).fetchone()
            if not row:
                return None
            return self._pack_run(connection, row)
        finally:
            connection.close()

    async def list_timeline(self, thesis_id: str) -> List[Dict[str, Any]]:
        await self.database.initialize()
        return await asyncio.to_thread(self._list_timeline_sync, thesis_id)

    def _list_timeline_sync(self, thesis_id: str) -> List[Dict[str, Any]]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT timeline_id, payload_json, created_at
                FROM tracking_timeline
                WHERE thesis_id = ?
                ORDER BY created_at DESC
                """,
                (thesis_id,),
            ).fetchall()
        finally:
            connection.close()
        items = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["timeline_id"] = row["timeline_id"]
            payload["created_at"] = row["created_at"]
            items.append(payload)
        return items

    async def save_timeline(
        self,
        thesis_id: str,
        payload: Dict[str, Any],
        *,
        run_id: Optional[str] = None,
    ) -> str:
        await self.database.initialize()
        timeline_id = str(payload.get("timeline_id") or "{}-{}".format(
            thesis_id, utc_now().strftime("%Y%m%d%H%M%S%f")
        ))
        await asyncio.to_thread(
            self._save_timeline_sync, timeline_id, thesis_id, run_id, payload
        )
        return timeline_id

    def _save_timeline_sync(
        self,
        timeline_id: str,
        thesis_id: str,
        run_id: Optional[str],
        payload: Dict[str, Any],
    ) -> None:
        connection = self.database.connect()
        try:
            connection.execute(
                """
                INSERT INTO tracking_timeline(
                    timeline_id, thesis_id, run_id, schema_version,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    timeline_id,
                    thesis_id,
                    run_id,
                    str(payload.get("schema_version") or "1"),
                    json.dumps(payload, ensure_ascii=False),
                    utc_now().isoformat(),
                ),
            )
        finally:
            connection.close()
