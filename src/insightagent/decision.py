from __future__ import annotations

from typing import List, Optional

from .business_contracts import Decision, EvidenceRef, FundamentalSnapshot, Report
from .contracts import utc_now
from .events import NO_RECENT_EVENTS

DIMENSIONS_ALL = ["fundamental", "technical", "sentiment", "macro"]
DIMENSIONS_MISSING = ["technical", "sentiment", "macro"]
CONFIDENCE_BY_SCORE = {1: 0.45, 2: 0.55, 3: 0.7, 4: 0.85, 5: 0.95}


def p0_confidence(raw: float) -> float:
    return min(0.65, round(raw * 0.6, 4))


def build_p0_decision(
    report: Report,
    snapshot: FundamentalSnapshot,
) -> Decision:
    citations = list(report.citations)
    if report.abstain:
        rating = "abstain"
        value_score: Optional[int] = None
        confidence = p0_confidence(0.5)
        rationale = (
            "仅基于基本面。关键信息不足或分析师已弃权，不形成方向判断。"
            "技术面、情绪、宏观未评估，timing_score 保持未评估。"
        )
        advice = "信息不足，本次不形成方向判断。"
        falsifiers = _falsifiers(report, snapshot, abstain=True)
        risks = _risks(report, extra=["关键财务数据缺失，判断基础不完整"])
    else:
        rating = report.stance
        value_score = report.score
        confidence = p0_confidence(CONFIDENCE_BY_SCORE[report.score])
        rationale = (
            "仅基于基本面报告：stance={stance}，score={score}。"
            "其他三个维度未评估，因此置信度已按缺维规则打折。"
        ).format(stance=report.stance, score=report.score)
        if snapshot.company_name:
            advice = "{name}（{code}）基本面{stance}，时机未评估。".format(
                name=snapshot.company_name,
                code=snapshot.stock_code,
                stance=report.stance,
            )
        else:
            advice = "{code} 基本面{stance}，时机未评估。".format(
                code=snapshot.stock_code,
                stance=report.stance,
            )
        falsifiers = _falsifiers(report, snapshot, abstain=False)
        risks = _risks(report)

    if not citations and report.abstain:
        citations = [
            EvidenceRef(
                ref_id="missing-fields",
                kind="field",
                id="missing_fields",
                observed_at=snapshot.as_of or utc_now(),
                source="fundamental_snapshot",
                note="required fields unavailable",
            )
        ]

    return Decision(
        rating=rating,
        value_score=value_score,
        timing_score=None,
        confidence=confidence,
        rationale=rationale,
        disagreements=["仅基本面，其他维度未评估"],
        falsifiers=falsifiers,
        risks=risks,
        advice_one_liner=advice,
        citations=citations,
        dimensions_used=["fundamental"],
        dimensions_missing=list(DIMENSIONS_MISSING),
    )


def _falsifiers(
    report: Report,
    snapshot: FundamentalSnapshot,
    *,
    abstain: bool,
) -> List[str]:
    items: List[str] = []
    if "cashflow_lag" in snapshot.computed_flags:
        items.append(
            "若经营现金流持续无法支持净利润，则盈利质量假设被证伪"
        )
    if abstain:
        items.append("若关键财务数据补齐且质量可验证，则本次弃权不再成立")
    elif report.risks:
        items.append("若出现：" + report.risks[0] + "，则当前判断失效")
    else:
        items.append("若后续财报与当前 snapshot 方向相反，则本次判断失效")
    return items[:3]


def _risks(report: Report, extra: Optional[List[str]] = None) -> List[str]:
    risks: List[str] = []
    for item in list(report.risks) + list(extra or []):
        if item and item not in risks:
            risks.append(item)
    if "仅覆盖基本面，技术面/情绪/宏观未评估，时机不明" not in risks:
        risks.append("仅覆盖基本面，技术面/情绪/宏观未评估，时机不明")
    if len(risks) < 2:
        risks.append("财务快照可能滞后，实际披露或与当前数据不一致")
    return risks[:3]


def build_multi_factor_decision(
    fundamental_report: Report,
    technical_report: Report,
    sentiment_report: Report,
    macro_report: Report,
    fundamental_snapshot: FundamentalSnapshot,
    user_constraint: str = "none",
) -> Decision:
    reports = {
        "fundamental": fundamental_report,
        "technical": technical_report,
        "sentiment": sentiment_report,
        "macro": macro_report,
    }
    used = [
        dim
        for dim in ["fundamental", "technical", "sentiment", "macro"]
        if not dimension_is_missing(dim, reports[dim])
    ]
    missing = [
        dim
        for dim in ["fundamental", "technical", "sentiment", "macro"]
        if dimension_is_missing(dim, reports[dim])
    ]

    fundamental = fundamental_report
    technical = technical_report
    sentiment = sentiment_report

    value_score = None if fundamental.abstain else fundamental.score
    timing_score = None if technical.abstain else technical.score

    voting_reports = [fundamental, technical, sentiment]
    stances = [r.stance for r in voting_reports if not r.abstain]
    if not stances:
        rating = "abstain"
    elif len(set(stances)) == 1:
        rating = stances[0]
    else:
        rating = _majority_stance(stances)

    if fundamental.abstain:
        confidence = p0_confidence(0.5)
    else:
        base = CONFIDENCE_BY_SCORE.get(fundamental.score, 0.5)
        confidence = min(0.85, base)
        for _ in missing:
            confidence *= 0.6
        if len(set(stances)) > 1:
            confidence *= 0.8
        confidence = min(0.85, round(confidence, 4))

    rationale_parts = []
    if not fundamental.abstain:
        rationale_parts.append(
            "基本面：{stance}，score={score}。".format(
                stance=fundamental.stance, score=fundamental.score
            )
        )
    else:
        rationale_parts.append("基本面弃权。")
    if not technical.abstain:
        rationale_parts.append(
            "技术面：{stance}，score={score}。".format(
                stance=technical.stance, score=technical.score
            )
        )
    else:
        rationale_parts.append("技术面弃权。")
    if not sentiment.abstain:
        rationale_parts.append(
            "情绪面：{stance}。".format(stance=sentiment.stance)
        )
    else:
        rationale_parts.append("情绪面弃权。")
    macro = macro_report
    if not dimension_is_missing("macro", macro):
        suffix = (
            "，本维不形成方向。"
            if macro.abstain
            else "。"
        )
        rationale_parts.append(
            "宏观：{cycle_tag}，相关性 {relevance}{suffix}".format(
                cycle_tag=macro.cycle_tag or "未标注",
                relevance=macro.relevance_to_stock or "unknown",
                suffix=suffix,
            )
        )
    else:
        rationale_parts.append("宏观未评估。")
    rationale = "".join(rationale_parts)
    if user_constraint != "none":
        rationale = "用户决策口径：" + user_constraint[:200] + "。" + rationale

    disagreements = []
    non_abstain = [
        (dim, r.stance)
        for dim, r in reports.items()
        if dim != "macro" and not r.abstain
    ]
    if len(non_abstain) >= 2:
        stances_set = {s for _, s in non_abstain}
        if len(stances_set) > 1:
            for dim, stance in non_abstain:
                for other_dim, other_stance in non_abstain:
                    if dim != other_dim and stance != other_stance:
                        disagreements.append(
                            "{dim} 为 {stance}，{other} 为 {other_stance}".format(
                                dim=dim,
                                stance=stance,
                                other=other_dim,
                                other_stance=other_stance,
                            )
                        )
            disagreements = list(dict.fromkeys(disagreements))[:3]

    falsifiers = _merge_falsifiers(reports.values())

    risks = _merge_risks(reports.values())

    if fundamental.abstain:
        advice = "信息不足，本次不形成方向判断。"
    elif value_score is not None and timing_score is not None:
        advice = (
            "价值面 {value}/5，时机面 {timing}/5，综合 {rating}。".format(
                value=value_score, timing=timing_score, rating=rating
            )
        )
    elif value_score is not None:
        advice = "价值面 {value}/5，时机未评估，综合 {rating}。".format(
            value=value_score, rating=rating
        )
    else:
        advice = "价值面未评估，综合 {rating}。".format(rating=rating)
    if disagreements:
        advice = advice.rstrip("。") + "；" + disagreements[0]

    if fundamental.abstain:
        citations = [
            EvidenceRef(
                ref_id="missing-fields",
                kind="field",
                id="missing_fields",
                observed_at=fundamental_snapshot.as_of or utc_now(),
                source="fundamental_snapshot",
                note="required fields unavailable",
            )
        ]
    else:
        citations = list(fundamental.citations)
    seen = {c.ref_id for c in citations}
    for r in [technical, sentiment, macro]:
        if r.abstain:
            continue
        for c in r.citations:
            if c.ref_id not in seen:
                citations.append(c)
                seen.add(c.ref_id)

    return Decision(
        rating=rating,
        value_score=value_score,
        timing_score=timing_score,
        confidence=confidence,
        rationale=rationale,
        disagreements=disagreements,
        falsifiers=falsifiers,
        risks=risks,
        advice_one_liner=advice,
        citations=citations,
        dimensions_used=used,
        dimensions_missing=missing,
    )


def dimension_is_missing(dim: str, report: Report) -> bool:
    """Low-relevance macro or stale-only sentiment is a completed N/A, not a hole."""
    if dim == "macro" and report.relevance_to_stock == "low":
        return False
    if dim == "sentiment" and NO_RECENT_EVENTS in report.missing_information:
        return False
    return bool(report.abstain)


def report_degrades_run(dim: str, report: Report) -> bool:
    if dim == "macro" and report.relevance_to_stock == "low":
        return False
    if dim == "sentiment" and NO_RECENT_EVENTS in report.missing_information:
        return False
    return bool(report.abstain or report.degraded)


def _majority_stance(stances: List[str]) -> str:
    counts: dict[str, int] = {}
    for s in stances:
        counts[s] = counts.get(s, 0) + 1
    return max(counts, key=counts.get)


_BOILERPLATE_FALSIFIERS = (
    "方向相反",
    "新增信息与当前判断",
    "本次判断失效",
)


def _usable_falsifier(text: str) -> bool:
    if not text or len(text.strip()) < 8:
        return False
    if any(token in text for token in _BOILERPLATE_FALSIFIERS):
        return False
    return True


def _merge_falsifiers(reports: List[Report]) -> List[str]:
    items: List[str] = []

    def add(text: Optional[str]) -> None:
        if text and _usable_falsifier(text) and text not in items:
            items.append(text)

    for report in reports:
        for item in report.falsifiers:
            add(item)
        if report.abstain:
            continue
        if report.role == "technical" and report.key_levels:
            add("若收盘有效跌破本次关键位：" + report.key_levels[:120])
        if report.role == "fundamental" and report.risks:
            add("若出现：" + report.risks[0][:80] + "，则当前基本面判断失效")
        if report.role == "sentiment" and report.event_flags:
            add("若60日窗口内新增控股股东减持或监管问询，则当前情绪判断失效")
        if (
            report.role == "macro"
            and report.relevance_to_stock == "high"
        ):
            add("若1年期LPR相对本次快照出现明显变动，则当前利率环境标签失效")
    if not items:
        items.append(
            "若关键价位、财报字段或60日窗口内重大事件与本次依据相反，则判断失效"
        )
    return items[:3]


def _merge_risks(reports: List[Report]) -> List[str]:
    merged: List[str] = []
    for report in reports:
        for item in report.risks:
            if item and item not in merged:
                merged.append(item)
    if len(merged) < 2:
        merged.append("部分维度未评估，判断基础不完整")
    if len(merged) < 3:
        merged.append("数据快照可能滞后于实时市场变化")
    return merged[:3]
