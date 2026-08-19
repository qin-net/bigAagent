from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

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

    async def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        await self.database.initialize()
        return await asyncio.to_thread(self._get_run_sync, run_id)

    def _get_run_sync(self, run_id: str) -> Optional[Dict[str, Any]]:
        connection = self.database.connect()
        try:
            run = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not run:
                return None
            reports = connection.execute(
                "SELECT * FROM reports WHERE run_id = ?", (run_id,)
            ).fetchall()
            decisions = connection.execute(
                "SELECT * FROM decisions WHERE run_id = ?", (run_id,)
            ).fetchall()
            return {
                "run": json.loads(run["payload_json"]),
                "status": run["status"],
                "reports": [
                    json.loads(row["payload_json"]) for row in reports
                ],
                "decisions": [
                    json.loads(row["payload_json"]) for row in decisions
                ],
            }
        finally:
            connection.close()
