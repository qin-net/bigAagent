from __future__ import annotations

from pathlib import Path

import pytest

from insightagent.business_contracts import EvidenceRef, Report
from insightagent.methodology import (
    MethodologyCatalog,
    drop_unretrieved_kb,
    parse_entry_markdown,
    resolve_markdown_path,
    search_methodology,
)
from insightagent.persistence import SQLiteDatabase


def test_empty_query_returns_nothing():
    assert search_methodology("", scope="fundamental") == []
    assert search_methodology("  ", scope="technical") == []
    assert search_methodology("a", scope="fundamental") == []


def test_scope_and_flags_gate_rsi():
    hits = search_methodology("rsi 超买", scope="technical", flags=["rsi_overbought"])
    assert any(item["id"] == "kb_rsi_overbought" for item in hits)
    blocked = search_methodology(
        "rsi 超买", scope="technical", flags=["ma_bull_align"]
    )
    assert blocked == []
    assert search_methodology("rsi", scope="fundamental", flags=None) == []


def test_sentiment_scope_without_flags_still_matches_trigger():
    hits = search_methodology("减持", scope="sentiment")
    assert any(item["id"] == "kb_event_reduction" for item in hits)
    assert search_methodology("减持", scope="fundamental") == []


def test_drop_unretrieved_kb_keeps_rules():
    report = Report(
        role="fundamental",
        score=3,
        stance="hold",
        summary="现金流滞后。",
        citations=[
            EvidenceRef(
                ref_id="r1",
                kind="rule",
                id="cashflow_lag",
                source="snapshot",
            ),
            EvidenceRef(
                ref_id="k1",
                kind="kb",
                id="kb_rsi_overbought",
                source="search_methodology",
            ),
        ],
        risks=["季节性"],
    )
    cleaned = drop_unretrieved_kb(report, {"kb_cashflow_lag"})
    kinds = {(item.kind, item.id) for item in cleaned.citations}
    assert ("rule", "cashflow_lag") in kinds
    assert ("kb", "kb_rsi_overbought") not in kinds


@pytest.mark.asyncio
async def test_catalog_seed_import_approve(tmp_path: Path):
    database = SQLiteDatabase(str(tmp_path / "kb.db"))
    await database.initialize()
    catalog = MethodologyCatalog(database)
    catalog.ensure_seeded()
    assert catalog.get("kb_cashflow_lag")["status"] == "approved"
    markdown = tmp_path / "card.md"
    markdown.write_text(
        "\n".join(
            [
                "---",
                "id: kb_non_recurring",
                "title: 扣非要对字段",
                "scope: [fundamental]",
                "trigger: 非经常 扣非",
                "action: 字段缺失则 missing",
                "evidence_required: [valuation_cheap]",
                "---",
                "非经常性损益不能单独写成盈利质量改善。",
            ]
        ),
        encoding="utf-8",
    )
    stored = catalog.import_markdown(markdown)
    assert stored["status"] == "candidate"
    assert catalog.search(
        "扣非", scope="fundamental", flags=["valuation_cheap"]
    ) == []
    catalog.approve("kb_non_recurring")
    hits = catalog.search(
        "扣非", scope="fundamental", flags=["valuation_cheap"]
    )
    assert hits[0]["id"] == "kb_non_recurring"


def test_parse_and_whitelist(tmp_path: Path):
    payload = parse_entry_markdown(
        "---\nid: kb_x\nscope: [fundamental]\n---\nhello"
    )
    assert payload["id"] == "kb_x"
    allowed = tmp_path / "kb"
    allowed.mkdir()
    target = allowed / "a.md"
    target.write_text("ok", encoding="utf-8")
    assert resolve_markdown_path(str(target), [allowed]) == target.resolve()
    outsider = tmp_path / "nope.md"
    outsider.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        resolve_markdown_path(str(outsider), [allowed])
