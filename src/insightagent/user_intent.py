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
decision, tracking, not_evidence, effect, dims.
fundamental/technical/sentiment/macro/decision/tracking/not_evidence:
each is a short Chinese task sentence, or the string none.
effect is exactly one of: this_run, remember, rerun, remember_rerun, end.
dims is none, or a comma-separated list from:
fundamental, technical, sentiment, macro, decision.

Understand chat language, not only hashtags.
Wanting a dimension rewritten / redone / another pass → effect=rerun
and put that dimension in dims (analysis slot may stay none).
Wanting to keep a rule for next time → effect=remember and fill the slot.
Wanting to stop, quit, or drop this review → effect=end; other keys none.
A constraint without asking to rerun or remember → effect=this_run
and fill the matching slot.
Vague praise/blame or "just look" without a real instruction → effect=this_run
and all other keys none.

Put rumors into not_evidence. Split one sentence across slots if needed.
Do not copy the user message verbatim.
Do not add indicators the user did not mention.
Do not output null, missing keys, or empty strings.
""".strip()

EXTRACT_USER_PREFIX = """
Examples (do not copy these as output for other texts):
「别光看便宜」→ effect=this_run, dims=fundamental, fundamental: 估值不要只看价格低或PE低，须对照快照里已有的估值与质量字段。
「利润好看会不会假」→ effect=this_run, dims=fundamental, fundamental: 利润须对照经营现金流等已有质量字段，不能只看利润同比。
「写得不行，重写一下基本面」「基本面再出一份正经的」→ effect=rerun, dims=fundamental, analysis keys none.
「把技术面再看一遍」→ effect=rerun, dims=technical.
「记住别只看便宜」→ effect=remember, dims=fundamental, fundamental: 估值不要只看价格低或PE低，须对照快照里已有的估值与质量字段。
「这个任务结束吧，放弃了」→ effect=end, dims=none, all other keys none.
「线一直往下好吓人」→ effect=this_run, dims=technical, technical: 以下行趋势为主，不要把超卖单独当成反转。
「我同事说要涨」→ effect=this_run, not_evidence: 他人看多；分析槽 none.
「写得太保守了」「3分改成4分」「帮我看看」→ effect=this_run, all other keys none.

User text:
""".strip()

HANZI_RE = re.compile(r"[\u4e00-\u9fff]")
MEMORY_CHECK_MARKERS = (
    "对照",
    "对比",
    "不要",
    "不能",
    "须",
    "必须",
    "检查",
    "估值",
    "现金流",
    "均线",
    "回购",
    "增持",
    "利率",
    "便宜",
    "趋势",
    "超卖",
    "pe",
    "PE",
)
MEMORY_GARBAGE_MARKERS = (
    "不行",
    "不好",
    "改分",
    "太保守",
    "太乐观",
    "写得",
    "太差",
    "太好",
)

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
    "macro": (
        "Analyze macro relevance from the precomputed snapshot. "
        "Call get_macro_snapshot first, then return the final JSON."
    ),
}

MAX_SLOT_CHARS = 200
MAX_PREF = 8
MAX_PREF_CHARS = 80
ANALYSIS_DIMS = ("fundamental", "technical", "sentiment", "macro")
RERUN_EFFECTS = {"rerun", "remember_rerun"}
NL_RERUN_MARKERS = (
    "重写",
    "重跑",
    "再跑一遍",
    "重新分析",
    "再出一份",
    "重看一遍",
    "再看一遍",
    "重看一次",
    "再看一次",
)
NL_REMEMBER_MARKERS = (
    "记住",
    "记下来",
    "下次都要",
    "下次都按",
    "以后都要",
    "以后都按",
)
NL_END_MARKERS = (
    "结束",
    "放弃",
    "不用了",
    "算了别",
    "停下来",
)
NL_DIM_PHRASES = (
    ("基本面", "fundamental"),
    ("技术面", "technical"),
    ("情绪面", "sentiment"),
    ("宏观", "macro"),
    ("决策", "decision"),
    ("情绪", "sentiment"),
    ("技术", "technical"),
)


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
    ended = False
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
        elif mapped == "end":
            ended = True
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
    if ended and "end" not in found:
        found.append("end")
    return TagParse(
        tags=json.dumps(found or [NONE], ensure_ascii=False),
        effect=effect,
        body=body,
        tagged_dims=json.dumps(dims or [NONE], ensure_ascii=False),
    )


CONTROL_EFFECTS = {
    "this_run",
    "remember",
    "rerun",
    "remember_rerun",
    "end",
}


def _parse_dims_field(raw: str) -> List[str]:
    if not raw or raw == NONE:
        return []
    parts = [part.strip() for part in raw.replace("，", ",").split(",")]
    return [part for part in parts if part in DIMS]


def merge_parsed_with_slots(parsed: TagParse, slots: LlmIntentSlots) -> TagParse:
    tags = [item for item in json.loads(parsed.tags) if item != NONE]
    dims = [item for item in json.loads(parsed.tagged_dims) if item != NONE]
    hash_remember = "remember" in tags
    hash_rerun = "rerun" in tags
    hash_end = "end" in tags
    explicit = hash_remember or hash_rerun or hash_end

    llm_effect = slots.effect.strip() if slots.effect != NONE else "this_run"
    if llm_effect not in CONTROL_EFFECTS:
        llm_effect = "this_run"
    llm_dims = _parse_dims_field(slots.dims)
    inferred = _infer_control_from_nl(parsed.body)

    if not explicit:
        if llm_effect != "this_run":
            chosen = llm_effect
        elif inferred["rerun"] and inferred["remember"]:
            chosen = "remember_rerun"
        elif inferred["rerun"]:
            chosen = "rerun"
        elif inferred["remember"]:
            chosen = "remember"
        elif inferred["end"] or llm_effect == "end":
            chosen = "end"
        else:
            chosen = "this_run"
        if chosen == "remember_rerun":
            hash_remember = True
            hash_rerun = True
        elif chosen == "remember":
            hash_remember = True
        elif chosen == "rerun":
            hash_rerun = True
        elif chosen == "end":
            hash_end = True

    for dim in llm_dims + inferred["dims"]:
        if dim not in dims:
            dims.append(dim)
    if hash_remember and "remember" not in tags:
        tags.append("remember")
    if hash_rerun and "rerun" not in tags:
        tags.append("rerun")
    if hash_end and "end" not in tags:
        tags.append("end")
    for dim in dims:
        if dim not in tags:
            tags.append(dim)

    if hash_remember and hash_rerun:
        effect = "remember_rerun"
    elif hash_remember:
        effect = "remember"
    elif hash_rerun:
        effect = "rerun"
    else:
        effect = "this_run"

    body = parsed.body
    if hash_end and not hash_rerun and not hash_remember:
        body = NONE

    return TagParse(
        tags=json.dumps(tags or [NONE], ensure_ascii=False),
        effect=effect,
        body=body,
        tagged_dims=json.dumps(dims or [NONE], ensure_ascii=False),
    )


def _infer_control_from_nl(body: str) -> dict:
    if body == NONE:
        return {"remember": False, "rerun": False, "end": False, "dims": [], "tags": []}
    remember = any(marker in body for marker in NL_REMEMBER_MARKERS)
    rerun = any(marker in body for marker in NL_RERUN_MARKERS)
    if "再分析" in body and "别再分析" not in body and "不要再分析" not in body:
        rerun = True
    ended = any(marker in body for marker in NL_END_MARKERS)
    dims: List[str] = []
    for phrase, dim in NL_DIM_PHRASES:
        if phrase in body and dim not in dims:
            dims.append(dim)
    tags: List[str] = []
    if remember:
        tags.append("remember")
    if rerun:
        tags.append("rerun")
    if ended and not rerun:
        tags.append("end")
    tags.extend(dims)
    if ended and rerun:
        ended = False
    return {
        "remember": remember,
        "rerun": rerun,
        "end": ended and not rerun and not remember,
        "dims": dims,
        "tags": tags,
    }


def empty_slots() -> LlmIntentSlots:
    payload = {name: NONE for name in DIMS}
    payload["not_evidence"] = NONE
    payload["effect"] = "this_run"
    payload["dims"] = NONE
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
            LLMMessage(
                role="user",
                content=EXTRACT_USER_PREFIX + "\n" + body,
            ),
        ],
        thinking_enabled=False,
        response_format="json",
        max_tokens=1024,
    )
    try:
        response = await llm.complete(request)
        data = json.loads(response.content or "{}")
        if isinstance(data.get("dims"), list):
            data["dims"] = ",".join(str(item) for item in data["dims"]) or NONE
        data.setdefault("effect", "this_run")
        data.setdefault("dims", NONE)
        if not data.get("effect") or data["effect"] == NONE:
            data["effect"] = "this_run"
        if not data.get("dims"):
            data["dims"] = NONE
        return LlmIntentSlots.model_validate(data), "intent_parsed"
    except Exception:
        return empty_slots(), "intent_schema_invalid"


async def understand_prompt(
    llm: LLMAdapter, raw: str, model: str
) -> Tuple[TagParse, LlmIntentSlots, str]:
    parsed = parse_tags(raw)
    slots, event = await extract_slots(llm, parsed.body, model)
    return merge_parsed_with_slots(parsed, slots), slots, event


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


def compute_rerun_dims(intent: UserIntent) -> List[str]:
    tags = json.loads(intent.tags)
    named = {name for name in ANALYSIS_DIMS if name in tags}
    for dim in ANALYSIS_DIMS:
        if slot_of(intent, dim) != NONE:
            named.add(dim)
    return [dim for dim in ANALYSIS_DIMS if dim in named]


def is_decision_only_rerun(intent: UserIntent) -> bool:
    tags = json.loads(intent.tags)
    return not compute_rerun_dims(intent) and (
        "decision" in tags or intent.decision != NONE
    )


def should_schedule_rerun(intent: UserIntent) -> bool:
    if intent.effect not in RERUN_EFFECTS:
        return False
    return bool(compute_rerun_dims(intent)) or is_decision_only_rerun(intent)


def _hanzi_count(text: str) -> int:
    return len(HANZI_RE.findall(text))


def passes_memory_gate(slot: str) -> bool:
    """Memory gate: executable short rule only. Execution gate is slot != none."""
    if slot == NONE:
        return False
    text = slot.strip()
    if not text:
        return False
    lowered = text.lower()
    has_check = any(marker.lower() in lowered for marker in MEMORY_CHECK_MARKERS)
    if _hanzi_count(text) < 8 and not has_check:
        return False
    if not has_check and any(marker in text for marker in MEMORY_GARBAGE_MARKERS):
        return False
    return True


_ECHO_LABELS = (
    ("fundamental", "基本面"),
    ("technical", "技术面"),
    ("sentiment", "情绪面"),
    ("macro", "宏观"),
    ("decision", "决策"),
    ("tracking", "追踪"),
    ("not_evidence", "不当证据"),
)


def format_intent_echo(intent: UserIntent) -> str:
    lines = ["意图理解（非原话）："]
    for key, label in _ECHO_LABELS:
        lines.append("  {}: {}".format(label, slot_of(intent, key)))
    lines.append("  效力: {}".format(intent.effect))
    tags = json.loads(intent.tags)
    if "end" in tags:
        lines.append("  控制: 结束（不改已有报告）")
    return "\n".join(lines)


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
