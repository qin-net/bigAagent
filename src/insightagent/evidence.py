from __future__ import annotations

import math
import re
from typing import Iterable, List, Sequence, Set

from .business_contracts import FundamentalSnapshot, Report
from .data_contracts import MacroSnapshot

YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
SCORE_RE = re.compile(r"^[1-5]$")
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
STOCK_CODE_RE = re.compile(r"\b\d{6}\b")
ISO_DATETIME_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)
SLASH_DATE_RE = re.compile(r"\d{4}/\d{1,2}/\d{1,2}")
CN_DATE_RE = re.compile(r"\d{4}年\d{1,2}月(?:\d{1,2}日)?")
YEAR_MONTH_RE = re.compile(r"\d{4}-\d{2}(?!\d)")
RULE_THRESHOLDS = (15.0, 30.0, 60.0, 70.0, 80.0)
# Exact snapshot copy only. Do not treat 22 as 22.145.
NUMBER_ABS_TOL = 1e-6
NUMBER_REL_TOL = 1e-9


class EvidenceBindingError(ValueError):
    def __init__(self, numbers: Sequence[float]) -> None:
        self.numbers = list(numbers)
        super().__init__(
            "Report contains numbers not bound to snapshot evidence: {}".format(
                self.numbers
            )
        )


def report_text(report: Report) -> str:
    parts = [
        report.summary,
        report.valuation or "",
        report.financial_health or "",
        report.earnings_quality or "",
        " ".join(report.risks),
        " ".join(report.missing_information),
    ]
    return " ".join(part for part in parts if part)


def allowed_financial_numbers(snapshot: FundamentalSnapshot) -> Set[float]:
    allowed: Set[float] = set(RULE_THRESHOLDS)
    for value in snapshot.model_dump().values():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            allowed.add(float(value))
    for hit in snapshot.rule_hits:
        for token in NUMBER_RE.findall(hit.detail):
            allowed.add(float(token))
    return allowed


def strip_dates(text: str) -> str:
    cleaned = ISO_DATETIME_RE.sub(" ", text)
    cleaned = SLASH_DATE_RE.sub(" ", cleaned)
    cleaned = CN_DATE_RE.sub(" ", cleaned)
    return YEAR_MONTH_RE.sub(" ", cleaned)


def extract_candidate_numbers(text: str, stock_code: str) -> List[float]:
    cleaned = strip_dates(text)
    cleaned = STOCK_CODE_RE.sub(" ", cleaned)
    if stock_code:
        cleaned = cleaned.replace(stock_code, " ")
    numbers: List[float] = []
    for token in NUMBER_RE.findall(cleaned):
        if YEAR_RE.fullmatch(token) or SCORE_RE.fullmatch(token):
            continue
        numbers.append(float(token))
    return numbers


def number_is_allowed(value: float, allowed: Iterable[float]) -> bool:
    for candidate in allowed:
        if math.isclose(
            value,
            candidate,
            rel_tol=NUMBER_REL_TOL,
            abs_tol=NUMBER_ABS_TOL,
        ):
            return True
    return False


def bind_report_evidence(report: Report, snapshot: FundamentalSnapshot) -> None:
    allowed = allowed_financial_numbers(snapshot)
    unbound = [
        number
        for number in extract_candidate_numbers(
            report_text(report), snapshot.stock_code
        )
        if not number_is_allowed(number, allowed)
    ]
    if unbound:
        raise EvidenceBindingError(unbound)
    if not report.abstain and not report.citations:
        raise EvidenceBindingError([])
    require_cashflow_lag_citation(report, snapshot)


def macro_report_text(report: Report) -> str:
    parts = [
        report.summary,
        report.cycle_tag or "",
        report.market_bias or "",
        report.relevance_to_stock or "",
        " ".join(report.risks),
        " ".join(report.missing_information),
    ]
    return " ".join(part for part in parts if part)


def bind_macro_report_evidence(report: Report, macro: MacroSnapshot) -> None:
    allowed = {
        float(value)
        for value in [macro.lpr_1y, macro.lpr_5y, macro.shibor_overnight]
        if value is not None
    }
    unbound = [
        number
        for number in extract_candidate_numbers(
            macro_report_text(report), "000000"
        )
        if not number_is_allowed(number, allowed)
    ]
    if unbound:
        raise EvidenceBindingError(unbound)
    if not report.abstain and not report.citations:
        raise EvidenceBindingError([])


def require_cashflow_lag_citation(
    report: Report, snapshot: FundamentalSnapshot
) -> None:
    if report.abstain:
        return
    if "cashflow_lag" not in snapshot.computed_flags:
        return
    cited = any(
        citation.kind == "rule" and citation.id == "cashflow_lag"
        for citation in report.citations
    )
    if not cited:
        raise EvidenceBindingError([])
