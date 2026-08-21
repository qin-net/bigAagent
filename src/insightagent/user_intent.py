from __future__ import annotations

import json
import re
from typing import List, Sequence, Tuple
from uuid import uuid4

from .contracts import LLMMessage, LLMRequest, utc_now
from .llm import LLMAdapter
from .user_contracts import (
    DIMS,
    NONE,
    TAG_ALIASES,
    LlmIntentSlots,
    Moment,
    TagParse,
    UserIntent,
    UserUtterance,
)

TAG_RE = re.compile(r"(#[^\s#]+)")

EXTRACT_SYSTEM = """
You extract user intent for A-share research specialists.
Output JSON only. Keys: fundamental, technical, sentiment, macro,
decision, tracking, not_evidence.
Each value is a short Chinese task sentence, or the string none.
none means this slot has no user instruction.
Do not answer the stock question. Do not copy the user message verbatim.
Do not output null, missing keys, or empty strings.
If the text is unrelated, all slots are none.
""".strip()

DEFAULT_INSTRUCTION = {
    "fundamental": (
        "Analyze this stock from the precomputed snapshot. "
        "Call get_fundamental_snapshot, then return the final JSON."
    ),
    "technical": (
        "Analyze this stock from the precomputed snapshot. "
        "Call get_indicator_snapshot first, then return the final JSON."
    ),
    "sentiment": (
        "Analyze this stock from the precomputed snapshot. "
        "Call get_event_snapshot first, then return the final JSON."
    ),
}

MAX_SLOT_CHARS = 200
MAX_PREF = 8
MAX_PREF_CHARS = 80


def parse_tags(raw: str) -> TagParse:
    text = raw.strip()
    if text == "" or text == NONE:
        return TagParse(
            tags=json.dumps([NONE], ensure_ascii=False),
            effect="this_run",
            body=NONE,
            tagged_dims=json.dumps([NONE], ensure_ascii=False),
        )

    found: List[str] = []
    dims: List[str] = []
    remember = False
    rerun = False
    rest = text
    for match in TAG_RE.finditer(text):
        token = match.group(1)
        mapped = TAG_ALIASES.get(token) or TAG_ALIASES.get(token.lower())
        if mapped is None:
            continue
        found.append(mapped)
        rest = rest.replace(token, " ", 1)
        if mapped == "remember":
            remember = True
        elif mapped == "rerun":
            rerun = True
        elif mapped in DIMS:
            dims.append(mapped)

    if remember and rerun:
        effect = "remember_rerun"
    elif remember:
        effect = "remember"
    elif rerun:
        effect = "rerun"
    else:
        effect = "this_run"

    body = rest.strip() or NONE
    return TagParse(
        tags=json.dumps(found or [NONE], ensure_ascii=False),
        effect=effect,
        body=body,
        tagged_dims=json.dumps(dims or [NONE], ensure_ascii=False),
    )


def empty_slots() -> LlmIntentSlots:
    payload = {name: NONE for name in DIMS}
    payload["not_evidence"] = NONE
    return LlmIntentSlots.model_validate(payload)


async def extract_slots(
    llm: LLMAdapter,
    body: str,
    model: str,
) -> Tuple[LlmIntentSlots, str]:
    """Return slots and audit_event: parsed | schema_invalid."""
    if body == NONE:
        return empty_slots(), "intent_parsed"
    request = LLMRequest(
        model=model,
        messages=[
            LLMMessage(role="system", content=EXTRACT_SYSTEM),
            LLMMessage(role="user", content=body),
        ],
        thinking_enabled=False,
        response_format="json",
        max_tokens=1024,
    )
    try:
        response = await llm.complete(request)
        data = json.loads(response.content or "{}")
        return LlmIntentSlots.model_validate(data), "intent_parsed"
    except Exception:
        return empty_slots(), "intent_schema_invalid"


def build_intent(
    *,
    utterance_id: str,
    parsed: TagParse,
    slots: LlmIntentSlots,
) -> UserIntent:
    return UserIntent(
        intent_id=str(uuid4()),
        utterance_id=utterance_id,
        effect=parsed.effect,
        tags=parsed.tags,
        fundamental=slots.fundamental,
        technical=slots.technical,
        sentiment=slots.sentiment,
        macro=slots.macro,
        decision=slots.decision,
        tracking=slots.tracking,
        not_evidence=slots.not_evidence,
        created_at=utc_now().isoformat(),
    )


def slot_of(intent: UserIntent, dim: str) -> str:
    return str(getattr(intent, dim))


def split_slot(slot: str) -> Tuple[List[str], List[str]]:
    if slot == NONE:
        return [], []
    text = slot[:MAX_SLOT_CHARS]
    if text.endswith("？") or text.endswith("?"):
        return [text], []
    return [], ["用户口径：" + text]


def build_expert_user_query(
    *,
    run_id: str,
    stock_code: str,
    as_of: str,
    dim: str,
    intent: UserIntent,
    preference_statements: Sequence[str],
) -> str:
    questions, constraints = split_slot(slot_of(intent, dim))
    for statement in list(preference_statements)[:MAX_PREF]:
        constraints.append(statement[:MAX_PREF_CHARS])
    return json.dumps(
        {
            "run_id": run_id,
            "stock_code": stock_code,
            "as_of": as_of,
            "output_schema_version": "1",
            "instruction": DEFAULT_INSTRUCTION[dim],
            "objective": "完成本维分析",
            "reason": "首次研究",
            "required_questions": questions,
            "constraints": constraints,
        },
        ensure_ascii=False,
    )


def build_utterance(
    *,
    utterance_id: str,
    intent: UserIntent,
    parsed: TagParse,
    stock_code: str,
    thesis_id: str,
    run_id: str,
    created_at: str,
    user_id: str = "local",
    moment: Moment = "pre_run",
) -> UserUtterance:
    return UserUtterance(
        utterance_id=utterance_id,
        user_id=user_id,
        moment=moment,
        effect=parsed.effect,
        tags=parsed.tags,
        intent_id=intent.intent_id,
        stock_code=stock_code,
        thesis_id=thesis_id,
        run_id=run_id,
        created_at=created_at,
    )
