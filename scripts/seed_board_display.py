"""One-shot demo seed for InsightBoard paper, research, and track display."""
from __future__ import annotations

import asyncio
from uuid import uuid4

from insightagent.business_contracts import Decision, EvidenceRef, Report, RunRecord
from insightagent.contracts import TaskStatus, utc_now
from insightagent.env import resolve_path
from insightagent.persistence import SQLiteDatabase, SQLiteStateStore
from insightagent.research_store import ResearchStore
from insightagent.user_contracts import NONE, UserIntent, UserUtterance
from insightagent.user_store import UserStore
from insightboard.store import BoardStore

BOARD_DB = resolve_path("", default_relative="data/board.db")
AGENT_DB = resolve_path("", default_relative="data/insightagent.db")

BUYS = (
    ("000858", 800, "白酒核心资产，核对经营现金流后再加仓"),
    ("000333", 600, "家电龙头，不要只看估值便宜"),
    ("601318", 1000, "保险负债成本须对照利率周期"),
    ("300308", 200, "光模块景气，追踪不能只看一天涨跌"),
)

STOCKS = {
    "000858": {
        "name": "五粮液",
        "research_prompt_echo": "记住：不要只看便宜，须核对经营现金流",
        "track_prompt_echo": "追踪时对照基线价格，检查量能是否跟上",
        "fundamental": "渠道价稳，须核对经营现金流是否跟上利润。",
        "technical": "价格贴年线，趋势未破但量能偏弱。",
        "sentiment": "机构持仓平稳，未见集中减持公告。",
        "macro": "消费复苏斜率慢，利率环境中性。",
        "decision": "持有观察，现金流证伪再减。",
        "rating": "hold",
        "value": 4,
        "timing": 3,
        "track_status": "unchanged",
        "track_summary": "批价与基线接近，量能未放大，维持持有观察。",
        "thinking": "对照首次研究：价格偏离不足阈值，渠道反馈无突变。",
        "synthesis": "四维无增量证伪，不改持有。",
        "eval_notes": "技术面量能不足，但不构成证伪。",
        "memory": "盯经营现金流与批价倒挂",
        "track_memory": "下一检查点仍看批价和成交量",
        "fun_pref": "不要只看便宜须核对经营现金流",
        "track_slot": "对照基线价格检查量能是否跟上",
    },
    "000333": {
        "name": "美的集团",
        "research_prompt_echo": "记住：估值须对照自由现金流，不要只看 PE",
        "track_prompt_echo": "追踪宏观利率对出口的影响",
        "fundamental": "海外收入稳，估值须对照自由现金流。",
        "technical": "箱体上沿，未放量突破。",
        "sentiment": "回购仍在，情绪中性。",
        "macro": "出口链对汇率敏感，利率环境中性。",
        "decision": "持有，等放量再讨论加仓。",
        "rating": "hold",
        "value": 4,
        "timing": 3,
        "track_status": "review",
        "track_summary": "汇率波动加大，建议复核出口敞口，不立刻改评级。",
        "thinking": "宏观汇率相对基线变差，基本面快照未坏。",
        "synthesis": "维持持有，但把出口与对冲列入下次核对。",
        "eval_notes": "宏观评测打折，财务质量仍可用。",
        "memory": "估值须对照自由现金流",
        "track_memory": "盯出口与汇率对冲",
        "fun_pref": "估值须对照自由现金流不要只看PE",
        "track_slot": "对照利率与汇率检查出口敞口",
    },
    "601318": {
        "name": "中国平安",
        "research_prompt_echo": "记住：保险负债成本必须对照利率",
        "track_prompt_echo": "检查利率是否已证伪负债成本假设",
        "fundamental": "NBV 回升，负债成本必须对照利率。",
        "technical": "长期中枢震荡，未破关键支撑。",
        "sentiment": "地产关联舆情仍在，但非新增爆点。",
        "macro": "利率下行压力仍在，周期标签不足以下结论。",
        "decision": "持有，利率再下台阶则复核。",
        "rating": "hold",
        "value": 3,
        "timing": 3,
        "track_status": "review",
        "track_summary": "利率相对基线走低，负债成本假设需要复核。",
        "thinking": "宏观利率触发预筛，基本面尚未交出新快照证伪。",
        "synthesis": "不改持有，但把利率敏感性列为高优先级核对。",
        "eval_notes": "宏观评测接受，财务快照仍够用。",
        "memory": "负债成本必须对照利率",
        "track_memory": "利率下台阶则提高复核优先级",
        "fun_pref": "保险负债成本必须对照利率",
        "track_slot": "检查利率是否证伪负债成本假设",
    },
    "300308": {
        "name": "中际旭创",
        "research_prompt_echo": "记住：景气股不能只看一天涨跌",
        "track_prompt_echo": "追踪订单与产能，不要被日内波动带跑",
        "fundamental": "800G 需求在，须核对资本开支与经营现金流。",
        "technical": "高位震荡，趋势仍在但波动大。",
        "sentiment": "拥挤度高，Crowding 风险偏高。",
        "macro": "算力资本开支周期未结束，与个股相关度高。",
        "decision": "持有但仓位克制，拥挤与现金流双核对。",
        "rating": "hold",
        "value": 3,
        "timing": 2,
        "track_status": "unchanged",
        "track_summary": "价格回撤未破首次研究关键位，订单叙事未变。",
        "thinking": "情绪拥挤仍在，但没有新的订单证伪。",
        "synthesis": "维持克制持有，继续盯资本开支与现金流。",
        "eval_notes": "情绪面可靠性中等，已打折。",
        "memory": "景气须核对资本开支与经营现金流",
        "track_memory": "不要被日内涨跌改结论",
        "fun_pref": "景气股不能只看一天涨跌须核对现金流",
        "track_slot": "对照订单与产能不要被日内波动带跑",
    },
}


def cite(code: str, kind: str = "field") -> EvidenceRef:
    return EvidenceRef(
        ref_id="ev-{}-{}".format(code, kind),
        kind="field",
        id="demo-{}".format(code),
        source="board-demo",
        note="展示样本",
    )


def report(role: str, code: str, spec: dict) -> Report:
    extras = {}
    if role == "fundamental":
        extras = {
            "valuation": "中枢附近",
            "financial_health": "可核对",
            "earnings_quality": "需对照现金流",
        }
        summary = spec["fundamental"]
        stance = "hold"
        score = spec["value"]
    elif role == "technical":
        extras = {
            "trend": "震荡",
            "setup": "无明确突破",
            "key_levels": "年线附近",
        }
        summary = spec["technical"]
        stance = "hold"
        score = spec["timing"]
    elif role == "sentiment":
        extras = {
            "event_flags": ["demo"],
            "crowd_risk": "high" if code == "300308" else "medium",
        }
        summary = spec["sentiment"]
        stance = "hold"
        score = 3
    else:
        extras = {
            "cycle_tag": "insufficient",
            "market_bias": "neutral",
            "relevance_to_stock": "high" if code == "300308" else "low",
        }
        summary = spec["macro"]
        stance = "hold"
        score = 3
    return Report(
        role=role,
        score=score,
        stance=stance,
        summary=summary,
        citations=[cite(code, role)],
        risks=["样本风险一", "样本风险二"],
        falsifiers=["关键假设被公开数据证伪"],
        **extras,
    )


def decision(code: str, spec: dict) -> Decision:
    return Decision(
        rating=spec["rating"],
        value_score=spec["value"],
        timing_score=spec["timing"],
        confidence=0.62,
        rationale=spec["decision"],
        disagreements=["技术面偏弱于基本面"],
        falsifiers=["经营现金流连续两季恶化"],
        risks=["估值拥挤", "宏观利率超预期"],
        advice_one_liner=spec["decision"],
        citations=[cite(code, "decision")],
        dimensions_used=["fundamental", "technical", "sentiment", "macro"],
        dimensions_missing=[],
    )


async def remember(
    users: UserStore,
    *,
    run_id: str,
    code: str,
    moment: str,
    effect: str,
    tags: str,
    slots: dict,
) -> None:
    now = utc_now().isoformat()
    utterance_id = str(uuid4())
    intent_id = str(uuid4())
    intent = UserIntent(
        intent_id=intent_id,
        utterance_id=utterance_id,
        effect=effect,
        tags=tags,
        fundamental=slots.get("fundamental", NONE),
        technical=slots.get("technical", NONE),
        sentiment=slots.get("sentiment", NONE),
        macro=slots.get("macro", NONE),
        decision=slots.get("decision", NONE),
        tracking=slots.get("tracking", NONE),
        not_evidence=NONE,
        created_at=now,
    )
    utterance = UserUtterance(
        utterance_id=utterance_id,
        user_id="local",
        moment=moment,
        effect=effect,
        tags=tags,
        intent_id=intent_id,
        stock_code=code,
        thesis_id="{}-initial".format(code),
        run_id=run_id,
        created_at=now,
    )
    await users.save_utterance(utterance)
    await users.save_intent(intent)
    await users.persist_remember(intent=intent, utterance=utterance)


async def expert_memory(
    states: SQLiteStateStore,
    *,
    agent_name: str,
    code: str,
    summary: str,
) -> str:
    state = await states.load_or_create(
        agent_name=agent_name,
        thesis_id="{}-initial".format(code),
        stock_code=code,
    )
    state.private_memory = {
        "memory_summary": summary[:200],
        "active_hypotheses": [summary[:80]],
        "open_questions": ["下一季数据是否证伪"],
        "falsifiers_watched": ["经营现金流恶化"],
        "lessons": ["展示样本，非投资建议"],
    }
    state.status = TaskStatus.SUCCESS
    await states.save(state, expected_version=state.version)
    return state.session_id


async def seed_stock(research: ResearchStore, states: SQLiteStateStore, users: UserStore, board: BoardStore, code: str) -> None:
    spec = STOCKS[code]
    thesis = "{}-initial".format(code)
    research_id = "demo-{}-research".format(code)
    track_id = "demo-{}-track".format(code)
    sessions = {}
    for role, summary in (
        ("fundamental", spec["memory"]),
        ("technical", spec["technical"][:80]),
        ("sentiment", spec["sentiment"][:80]),
        ("macro", spec["macro"][:80]),
        ("tracking", spec["track_memory"]),
    ):
        sessions[role] = await expert_memory(
            states, agent_name=role, code=code, summary=summary
        )
    run = RunRecord(
        run_id=research_id,
        stock_code=code,
        thesis_id=thesis,
        mode="research",
        status="success",
        session_ids={k: sessions[k] for k in ("fundamental", "technical", "sentiment", "macro")},
    )
    await research.save_run(run)
    for role in ("fundamental", "technical", "sentiment", "macro"):
        await research.save_report(research_id, role, report(role, code, spec))
    await research.save_decision(research_id, decision(code, spec))
    await remember(
        users,
        run_id=research_id,
        code=code,
        moment="pre_run",
        effect="remember",
        tags="#remember #fundamental",
        slots={"fundamental": spec["fun_pref"]},
    )
    job = board.create_research_job(code, kind="analyze")
    board.finish_research(job["job_id"], run_id=research_id)

    track_run = RunRecord(
        run_id=track_id,
        stock_code=code,
        thesis_id=thesis,
        mode="track_day",
        status="success",
        parent_run_id=research_id,
        session_ids={"tracking": sessions["tracking"]},
    )
    await research.save_run(track_run)
    await research.save_timeline(
        thesis,
        {
            "schema_version": "1",
            "mode": "track_day",
            "stock_code": code,
            "thesis_id": thesis,
            "advice": spec["track_status"],
            "triggers_hit": ["demo_display"],
            "deliverable": {
                "status": spec["track_status"],
                "work_summary": spec["track_summary"],
                "thinking": spec["thinking"],
                "synthesis": spec["synthesis"],
                "expert_evaluations": [
                    {
                        "agent": "technical",
                        "reliability": "medium",
                        "verdict": "discount",
                        "gaps": ["量能样本有限"],
                        "notes": spec["eval_notes"],
                    },
                    {
                        "agent": "fundamental",
                        "reliability": "high",
                        "verdict": "accept",
                        "gaps": [],
                        "notes": "财务快照仍可用",
                    },
                ],
                "evidence_refs": [],
                "triggers_hit": ["demo_display"],
                "agent_skill_calls": [
                    {
                        "agent": "technical",
                        "question": "对照基线，当前量价是否证伪趋势？",
                        "required_context_refs": [],
                        "reason": "展示调度",
                        "status": "success",
                    }
                ],
                "decision_required": spec["track_status"] != "unchanged",
                "user_output": {
                    "title": "本次跟踪更新",
                    "summary": spec["track_summary"],
                    "holding_advice": spec["track_status"],
                    "key_changes": [spec["thinking"][:40]],
                    "next_watch_items": [spec["track_memory"]],
                },
                "next_check_suggestion": {
                    "urgency": "medium" if spec["track_status"] == "review" else "low",
                    "reason": spec["track_memory"],
                },
            },
        },
        run_id=track_id,
    )
    await remember(
        users,
        run_id=track_id,
        code=code,
        moment="post_track",
        effect="this_run",
        tags="#tracking",
        slots={"tracking": spec["track_slot"]},
    )
    track_job = board.create_research_job(code, kind="track")
    board.finish_research(track_job["job_id"], run_id=track_id)


async def main() -> None:
    board = BoardStore(BOARD_DB)
    board.initialize()
    board.initialize_research()
    board.initialize_paper()
    board.save_pick_memory(
        stock_code="none",
        statement="选股纪律：不追一天涨停，加仓前须核对经营现金流",
    )
    for code, qty, reason in BUYS:
        board.add_watch(code)
        snap = board.paper_trade(code, side="buy", quantity=qty, reason=reason)
        print("bought", code, qty, "equity", snap["equity"], "cash", snap["cash"])
    database = SQLiteDatabase(AGENT_DB)
    await database.initialize()
    research = ResearchStore(database)
    states = SQLiteStateStore(database)
    users = UserStore(database)
    for code in STOCKS:
        await seed_stock(research, states, users, board, code)
        print("seeded research+track", code)
    final = board.paper_snapshot()
    print(
        "paper equity",
        final["equity"],
        "cash",
        final["cash"],
        "positions",
        len(final["positions"]),
    )


if __name__ == "__main__":
    asyncio.run(main())
