import json
from pathlib import Path

import pytest

from insightagent.tracking_agent import track_thesis

from tests.llm_recording import RecordingLLM
from tests.test_live_track import MODEL, _adapter
from tests.test_tracking_agent import _inquiry_current, _seed_baseline
from tests.track_capability import evaluate_tracker_capability

pytestmark = pytest.mark.live

DUMP = Path("/tmp/insightagent-track-capability.json")
MIN_SCORE = 70.0


async def _run(scenario: str, tmp_path, *, mutate=None):
    database, snaps, thesis_id = await _seed_baseline(tmp_path)
    current = mutate(snaps) if mutate else snaps
    rec = RecordingLLM(_adapter())
    result = await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=rec,
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        current=current,
        model=MODEL,
        thinking_enabled=True,
    )
    report = evaluate_tracker_capability(
        scenario=scenario,
        result=result,
        call_args=rec.call_args,
    )
    report["tools"] = rec.calls
    report["tracker_tools"] = rec.names_when("get_tracking_context")
    return report


def _dump(reports: list) -> None:
    existing = []
    if DUMP.exists():
        try:
            existing = json.loads(DUMP.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except json.JSONDecodeError:
            existing = []
    by_name = {item.get("scenario"): item for item in existing if isinstance(item, dict)}
    for report in reports:
        by_name[report["scenario"]] = report
    payload = list(by_name.values())
    DUMP.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("CAPABILITY_DUMP", DUMP)
    for report in reports:
        print(
            "SCENARIO",
            report["scenario"],
            "SCORE",
            report["score"],
            "{}/{}".format(report["pass_count"], report["check_count"]),
        )
        for item in report["checks"]:
            mark = "PASS" if item["passed"] else "FAIL"
            print(" ", mark, item["name"], "-", item["detail"])
        excerpts = report["excerpts"]
        print(" THINKING:", excerpts["thinking"][:500])
        print(" SYNTHESIS:", excerpts["synthesis"][:500])
        print(
            " EVALS:",
            json.dumps(excerpts["expert_evaluations"], ensure_ascii=False)[:800],
        )


@pytest.mark.asyncio
async def test_live_tracker_capability_unchanged_and_inquiry(tmp_path):
    unchanged = await _run("unchanged", tmp_path / "unchanged")
    _dump([unchanged])
    inquiry = await _run(
        "inquiry", tmp_path / "inquiry", mutate=_inquiry_current
    )
    _dump([unchanged, inquiry])
    reports = [unchanged, inquiry]
    for report in reports:
        failed = [item["name"] for item in report["checks"] if not item["passed"]]
        assert report["score"] >= MIN_SCORE, (
            "{} scored {} < {}; failed={}".format(
                report["scenario"], report["score"], MIN_SCORE, failed
            )
        )
