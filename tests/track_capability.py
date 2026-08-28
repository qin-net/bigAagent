from __future__ import annotations

import json
from typing import Any, Dict, List

FALLBACK_THINKING = "specialist output was received; tracker did not write thinking"
FALLBACK_EVAL = "tracker omitted evaluation; marked incomplete"

REPORT_KEYS = {"role", "score", "stance", "financial_health", "cycle_tag"}


def _add(
    checks: List[Dict[str, Any]],
    name: str,
    passed: bool,
    detail: str,
    *,
    weight: int = 1,
) -> None:
    checks.append(
        {
            "name": name,
            "passed": passed,
            "detail": detail,
            "weight": weight,
        }
    )


def _text(value: Any) -> str:
    return str(value or "").strip()


def evaluate_tracker_capability(
    *,
    scenario: str,
    result: Any,
    call_args: List[Dict[str, Any]],
) -> Dict[str, Any]:
    deliverable = result.deliverable or {}
    thinking = _text(deliverable.get("thinking"))
    synthesis = _text(deliverable.get("synthesis"))
    summary = _text((deliverable.get("user_output") or {}).get("summary"))
    status = str(deliverable.get("status") or "")
    evaluations = list(deliverable.get("expert_evaluations") or [])
    triggers = list(result.prescreen.get("triggers") or [])
    skill_calls = list(result.skill_calls or [])
    successful = [item for item in skill_calls if item.get("status") == "success"]
    checks: List[Dict[str, Any]] = []

    _add(
        checks,
        "status_contract",
        status in {"unchanged", "review", "invalidate"},
        "status={}".format(status),
    )
    _add(
        checks,
        "thinking_authored",
        len(thinking) >= 60 and FALLBACK_THINKING not in thinking,
        "len={} fallback={}".format(len(thinking), FALLBACK_THINKING in thinking),
        weight=2,
    )
    _add(
        checks,
        "synthesis_authored",
        len(synthesis) >= 60 and synthesis != thinking,
        "len={} distinct_from_thinking={}".format(
            len(synthesis), synthesis != thinking
        ),
        weight=2,
    )
    _add(
        checks,
        "user_summary",
        len(summary) >= 8,
        summary[:80] or "(empty)",
    )

    tracker_invented_report = any(
        key in deliverable for key in REPORT_KEYS
    )
    _add(
        checks,
        "tracker_not_a_fifth_analyst_report",
        not tracker_invented_report,
        "report_keys_on_deliverable={}".format(
            sorted(REPORT_KEYS & set(deliverable))
        ),
        weight=2,
    )

    schema_ok = True
    schema_detail = "no call_*"
    for payload in call_args:
        if not str(payload.get("name") or "").startswith("call_"):
            continue
        raw = (payload.get("arguments") or {}).get("output_schema")
        try:
            schema = json.loads(raw) if isinstance(raw, str) else None
        except json.JSONDecodeError:
            schema = None
        schema_ok = isinstance(schema, dict) and bool(schema.get("properties"))
        schema_detail = "ok" if schema_ok else "missing/invalid output_schema"
        if not schema_ok:
            break
    if any(str(p.get("name") or "").startswith("call_") for p in call_args):
        _add(checks, "call_output_schema", schema_ok, schema_detail, weight=2)

    eval_by_agent = {
        str(item.get("agent")): item
        for item in evaluations
        if isinstance(item, dict)
    }
    for item in successful:
        agent = str(item.get("agent"))
        ev = eval_by_agent.get(agent) or {}
        notes = _text(ev.get("notes"))
        verdict = str(ev.get("verdict") or "")
        passed = (
            bool(ev)
            and FALLBACK_EVAL not in notes
            and len(notes) >= 20
            and verdict in {"accept", "discount", "reject", "insufficient"}
        )
        _add(
            checks,
            "eval_{}".format(agent),
            passed,
            "verdict={} notes_len={} fallback={}".format(
                verdict or "(missing)",
                len(notes),
                FALLBACK_EVAL in notes,
            ),
            weight=2,
        )

    inquiry_like = any("has_inquiry" in str(item) for item in triggers)
    if scenario == "inquiry":
        _add(
            checks,
            "saw_inquiry_trigger",
            inquiry_like,
            "triggers={}".format(triggers),
            weight=2,
        )
        called_sentiment = any(
            item.get("agent") == "sentiment" for item in skill_calls
        )
        _add(
            checks,
            "dispatched_sentiment",
            called_sentiment,
            "agents={}".format([item.get("agent") for item in skill_calls]),
            weight=2,
        )
        blob = "{} {}".format(thinking, synthesis).lower()
        grounded = any(
            token in blob
            for token in ("问询", "inquiry", "has_inquiry", "披露", "风险")
        )
        _add(
            checks,
            "analysis_grounded_in_inquiry",
            grounded,
            "thinking+synthesis mention inquiry/risk={}".format(grounded),
            weight=2,
        )
        if status == "invalidate":
            hard = any(
                str(ev.get("verdict")) in {"reject", "accept"}
                and str(ev.get("agent")) == "sentiment"
                for ev in evaluations
            )
            _add(
                checks,
                "invalidate_has_eval_support",
                hard,
                "invalidate without a sentiment evaluation",
                weight=2,
            )

    if scenario == "unchanged":
        _add(
            checks,
            "no_false_invalidate",
            status != "invalidate",
            "status={} triggers={}".format(status, triggers),
            weight=2,
        )
        blob = "{} {}".format(thinking, synthesis)
        grounded = any(
            token in blob
            for token in ("无", "没有", "维持", "增量", "不变", "baseline", "对照")
        )
        _add(
            checks,
            "explains_no_delta",
            grounded,
            "thinking/synthesis explain stability={}".format(grounded),
            weight=2,
        )

    earned = sum(item["weight"] for item in checks if item["passed"])
    possible = sum(item["weight"] for item in checks) or 1
    score = round(100.0 * earned / possible, 1)
    excerpts = {
        "status": status,
        "holding_advice": (deliverable.get("user_output") or {}).get(
            "holding_advice"
        ),
        "thinking": thinking[:1200],
        "synthesis": synthesis[:1200],
        "summary": summary[:400],
        "expert_evaluations": evaluations,
        "skill_outputs": [
            {
                "agent": item.get("agent"),
                "status": item.get("status"),
                "question": item.get("question"),
                "output": item.get("output"),
            }
            for item in skill_calls
        ],
        "triggers": triggers,
    }
    return {
        "scenario": scenario,
        "score": score,
        "earned": earned,
        "possible": possible,
        "pass_count": sum(1 for item in checks if item["passed"]),
        "check_count": len(checks),
        "checks": checks,
        "excerpts": excerpts,
    }
