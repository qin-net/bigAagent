from __future__ import annotations

from typing import List, Optional

from .business_contracts import Decision, EvidenceRef, FundamentalSnapshot, Report
from .contracts import utc_now

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
