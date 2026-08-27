from __future__ import annotations

from pathlib import Path

import pytest

from insightagent.business_contracts import EvidenceRef, Report
from insightagent.kb_contract import card_from_payload
from insightagent.methodology import (
    MethodologyCatalog,
    SEED_ENTRIES,
    drop_unretrieved_kb,
    parse_entry_markdown,
    record_search,
    resolve_markdown_path,
    search_methodology,
)
from insightagent.persistence import SQLiteDatabase


def test_empty_query_returns_nothing():
    assert search_methodology("", scope="fundamental") == []
    assert search_methodology("  ", scope="technical") == []
    assert search_methodology("a", scope="fundamental") == []


def test_flags_gate_query_only_ranks():
    noisy = search_methodology(
        "zzzz unrelated", scope="technical", flags=["rsi_overbought"]
    )
    named = search_methodology(
        "rsi 超买", scope="technical", flags=["rsi_overbought"]
    )
    assert [item["id"] for item in noisy] == [item["id"] for item in named]
    assert named[0]["id"] == "kb_rsi_overbought"
    assert named[0]["reasons"]
    assert any(item.startswith("flag:rsi_overbought") for item in named[0]["reasons"])
    assert any(item.startswith("version:") for item in named[0]["reasons"])
    ma_hits = search_methodology(
        "rsi 超买", scope="technical", flags=["ma_bull_align"]
    )
    assert [item["id"] for item in ma_hits] == ["kb_ma_align"]
    sneaked = search_methodology(
        "现金流 利润含金量",
        scope="fundamental",
        flags=["ma_bull_align"],
    )
    assert sneaked == []
    empty_flags = search_methodology("rsi 超买", scope="technical", flags=[])
    assert empty_flags == []


def test_query_reranks_applicable_cards():
    cheap = search_methodology(
        "扣非", scope="fundamental", flags=["valuation_cheap"]
    )
    ids = [item["id"] for item in cheap]
    assert "kb_valuation" in ids
    alias = search_methodology(
        "利润含金量",
        scope="fundamental",
        flags=["cashflow_lag", "valuation_cheap"],
    )
    assert alias[0]["id"] == "kb_cashflow_lag"
    assert any("alias:利润含金量" in item or item == "alias:利润含金量" for item in alias[0]["reasons"])


def test_seed_card_fixtures():
    card = card_from_payload(
        next(item for item in SEED_ENTRIES if item["id"] == "kb_cashflow_lag")
    )
    for case in card.tests.should_match:
        hits = search_methodology(
            case.query, scope=card.scope[0], flags=case.flags
        )
        assert card.id in [item["id"] for item in hits]
    for case in card.tests.should_not_match:
        hits = search_methodology(
            case.query, scope=card.scope[0], flags=case.flags
        )
        assert card.id not in [item["id"] for item in hits]


def test_record_search_returns_retrieval_id():
    retrieved = set()
    payload = record_search(
        retrieved,
        "利润含金量",
        scope="fundamental",
        flags=["cashflow_lag"],
    )
    assert payload["retrieval_id"].startswith("kb-search-")
    assert payload["entries"][0]["id"] == "kb_cashflow_lag"
    assert payload["entries"][0]["version"] == "1"
    assert "kb_cashflow_lag" in retrieved


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
    pre = catalog.search(
        "扣非", scope="fundamental", flags=["valuation_cheap"]
    )
    assert all(item["id"] != "kb_non_recurring" for item in pre)
    catalog.approve("kb_non_recurring")
    hits = catalog.search(
        "扣非", scope="fundamental", flags=["valuation_cheap"]
    )
    ids = [item["id"] for item in hits]
    assert "kb_non_recurring" in ids
    assert "kb_valuation" in ids
    other_query = catalog.search(
        "zzzz", scope="fundamental", flags=["valuation_cheap"]
    )
    other_ids = [item["id"] for item in other_query]
    assert "kb_valuation" in other_ids
    assert "kb_non_recurring" in other_ids


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
