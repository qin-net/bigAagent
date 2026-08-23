from datetime import datetime, timezone

from insightagent.data_contracts import (
    EventItem,
    EventSnapshot,
    HolderChangeItem,
    HolderChangeSnapshot,
)
from insightagent.events import NO_RECENT_EVENTS, apply_event_rules
from insightagent.technicals import apply_computed_technical_semantics


def test_stale_events_flag_no_recent_and_ignore_old_reductions():
    as_of = datetime(2026, 8, 23, tzinfo=timezone.utc)
    events = EventSnapshot(
        stock_code="000001",
        as_of=as_of,
        events=[
            EventItem(
                event_id="event:000001:old",
                event_type="reduction",
                title="五年前减持",
                published_at="2021-09-07",
            )
        ],
    )
    holders = HolderChangeSnapshot(
        stock_code="000001",
        as_of=as_of,
        items=[
            HolderChangeItem(
                holder_name="高管",
                change_type="increase",
                published_at="2021-09-07",
            )
        ],
    )
    rules = apply_event_rules(events, holders)
    assert NO_RECENT_EVENTS in rules["flags"]
    assert "has_reduction" not in rules["flags"]
    assert events.events[0].in_window is False
    assert events.events[0].age_days == 1811


def test_recent_reduction_still_material():
    as_of = datetime(2026, 8, 23, tzinfo=timezone.utc)
    events = EventSnapshot(
        stock_code="000858",
        as_of=as_of,
        events=[
            EventItem(
                event_id="event:000858:red",
                event_type="reduction",
                title="控股股东拟减持",
                published_at="2026-07-20",
            )
        ],
    )
    holders = HolderChangeSnapshot(stock_code="000858", as_of=as_of, items=[])
    rules = apply_event_rules(events, holders)
    assert "has_reduction" in rules["flags"]
    assert NO_RECENT_EVENTS not in rules["flags"]
    assert events.events[0].in_window is True


def test_technical_semantics_backfill_numeric_key_levels():
    from insightagent.business_contracts import EvidenceRef, Report

    report = Report(
        role="technical",
        score=3,
        stance="hold",
        summary="整理",
        citations=[
            EvidenceRef(ref_id="p", kind="field", id="price", source="test")
        ],
        risks=["a", "b"],
        key_levels="下方支撑",
    )
    filled = apply_computed_technical_semantics(
        report, {"key_levels": "近期高点 89.50；MA20 85.07"}
    )
    assert "89.50" in (filled.key_levels or "")
