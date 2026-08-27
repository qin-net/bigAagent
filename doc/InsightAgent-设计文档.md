# InsightAgent 设计文档

> 状态：已讨论冻结，待实现  
> 日期：2026-08-18  
> 范围：Agent 侧架构、合同、跟踪与质量机制；知识库由金融侧按本文接口交付
> 运行时细化：参见 `Agent运行时框架设计.md`  
> 下一实现切片：参见 `P0首次研究切片设计.md`  
> 用户意图与偏好（待审核）：参见 `用户意图与偏好记忆设计.md`

本文替代「四个对称 Agent + 大师语录 RAG + 单次研报」的初版构想，作为后续实现与分工的依据。

---

## 1. 产品定位

InsightAgent 是面向 A 股的轻量投研协作系统。用户输入股票代码，得到可溯源的研究报告；用户将某票标为关注/买入后，系统按选定频率做增量跟踪，而不是每天重写一篇研报。

核心标签：**轻量编排 · 证据绑定 · 过程可回放 · 跟踪对照 thesis**

对外口径：研究辅助，不是投资建议。跟踪默认输出「维持 / 建议重评 / 证伪已触发」，避免做成高频荐股。

---

## 2. 已冻结决策

| 议题 | 结论 |
|------|------|
| 专家互辩 | 不做。专家只向总控交作业 |
| A2A / LangGraph | 第一期不做。单实例 Agent 本地调度器 + 应用层 workflow |
| 状态 | 应用层维护业务 Run；5 个 Agent 各有按股票/thesis/session 隔离、版本化的私有 State，无共享可写 State |
| 四专家 | 保留四维视角，但重量不对称 |
| 跟踪调度 | 单独设置追踪 Agent，按增量事件选择性调度四个分析 Agent，不默认全量重跑 |
| 知识库 | 规则清单为主，条文为辅；禁止装饰性引用 |
| 方法论库管理 | 追踪 Agent 负责整理、检索、版本和更新提案；正式生效需审核，并提供真人可读页面 |
| 涨跌当标签 | 禁止用随后盈亏**自动**改策略/偏好/权重。复盘可看结果层（后来价格、是否打中 falsifier），但不回写旧 Report，也不得仅凭单笔盈亏晋升方法论 |
| 过程日志 | 必须落盘结构化 `ProcessRecord`（facts / clues / judgment_basis / trace_steps / reflection）；不落盘 `reasoning_content` |
| 自进化 | 只积 Case，过闸门后晋升检查项；无自治进化 Agent |

---

## 3. 总体架构

外层是固定 workflow（拓扑稳定），Agent 内部可根据证据缺口自主决定工具调用、检索和推理轮次。专家之间不通信。

```text
用户：研究（股票代码） | 跟踪（频率 + 关注/买入）
        │
        ▼
┌───────────────────────────────────────────────┐
│  Orchestrator（薄 workflow，Python）            │
│  Ingest → Specialists → Validate → Decide      │
│  → Persist Snapshot → Render                   │
│  跟踪：IngestDelta → TrackingAgent → Specialists │
│       → (可选) Decide → Persist                  │
└───────────────────────────────────────────────┘
        │ 分发 snapshot_slice + 合同
        ▼
┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
│ 基本面   │ │ 技术面   │ │ 情绪     │ │ 宏观     │
│ 重       │ │ 重       │ │ 中       │ │ 轻       │
└────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘
     │ tool/KB    │            │            │
     └────────────┴─────┬──────┴────────────┘
                        ▼
              数据层 / 规则引擎 / 检索
                        ▼
              综合决策环节（价值分 ≠ 时机分）
                        ▼
              RunSnapshot + AgentState 落盘
```

技术选型（沿用初版、实现从简）：

- Python 3.9+
- 数据：AKShare，封装适配层（预留 Tushare）
- LLM：DeepSeek / 智谱；抽取与描写用快模型，最终判断可用更强模型
- 界面：Streamlit
- 规则与检索：本地；金融侧交付规则 + 条文，Agent 侧负责命中与引用

---

## 4. Agent 体系与系统环节

系统包含 **4 个分析 Agent + 1 个追踪 Agent**。综合决策、编排、校验和持久化仍是系统环节，不包装成独立角色。

| Agent | 重量 | 职责 | 自主停止条件 |
|------|------|------|--------------|
| 基本面 | 重 | 估值、盈利质量、财务健康、粗算安全边际 | 关键主张已有证据，或明确标记信息不足 |
| 技术面 | 重 | 趋势、关键位、量价；指标由代码预计算 | 趋势与风险判断所需指标已经覆盖 |
| 情绪 | 中 | 以公告/事件为主，禁止标题情感充当核心 | 重大事件已核查；无有效事件时允许弃权 |
| 宏观 | 轻 | 环境标签与风险提示，不主导买卖 | 与标的相关性已确认或判定为低相关 |
| 追踪 | 中 | 对照 thesis 与增量，自主决定是否以及调度哪些分析 Agent 复评 | 已能判断维持、重评或证伪，并说明依据 |

不在 Prompt 中规定固定轮次或步数。Agent 可自行继续检索、补取数据或结束；系统只保留超时、异常熔断、权限白名单等运行安全边界。成本预算作为提示和可观测指标，不作为正常情况下强制截断推理的固定步数。

系统环节：

- **编排环节**：拉数、分发输入、收集结果和落盘
- **校验环节**：检查 JSON 合同、引用和证据一致性
- **综合决策环节**：汇总四个分析 Agent 的判断，写分歧、弃权、置信度和证伪条件；可使用一次 LLM，但它不是第六个角色
- **状态管理环节**：装配共享追踪上下文；每个 Agent 的 LocalScheduler 读取私有 State、校验并提交 `state_patch`
- **应用层 workflow**只负责首次研究并行和综合决策；追踪期由追踪 Agent 将四个分析 Agent 作为 `AgentSkill` 调用

宏观缺失或失败时，Run 可降级继续。基本面失败则整次研究失败或标为不可用。

默认决策权重（代码配置，第一期不自动调）：基本面 ≥ 技术面 ≥ 情绪 ≥ 宏观。价值和时机必须分开展示，禁止平均成一个灵魂分数。

---

## 5. 核心工作流

### 5.1 研究模式（用户输入股票代码）

1. **Ingest**：按代码拉取 Data Snapshot（财务、行情、新闻/公告、宏观）。数据只拉一次，专家读切片；不足再各自补查。
2. **Specialists**：四路分析（MVP 可串行；互不依赖时可并行）。每人：抽取事实 → 跑规则清单 → 判断。清单外问题必须 `insufficient_data`。
3. **Validate**：检查字段、`stance`/`score` 合法、citations 非空或已声明弃权。失败重试 1 次或 `degraded=true`。
4. **Decide**：读取四份报告。一致则置信度可高；冲突必须写出分歧并下调置信度。先写证伪条件，再给结论。
5. **Persist + Render**：写入 `RunSnapshot`，生成报告（指标、四维分析、引用、决策、免责声明）。

分析 Agent 不直接通信。进入跟踪期后，每个 Agent 被唤醒时会收到自己的私有 state 与统一的只读 `TrackingContext`；追踪 Agent 可读取四个分析 Agent 的状态摘要，但不能修改它们。

### 5.2 首次固定合同与跟踪动态任务

- **首次研究 workflow**：四个分析 Agent 的输入、职责、数据切片和输出 schema 固定，保证结果可比较、可校验、可复现。
- **跟踪阶段**：追踪 Agent 根据增量事件自主生成 `AgentTaskPrompt`，决定调用哪个分析 Agent、要核查什么问题以及需要哪些证据。
- 追踪 Agent 只能提供动态 Task Prompt，不能修改分 Agent 的固定 System Prompt、工具权限或输出 schema。
- 分 Agent 必须返回严格 `AgentTaskResponse`；每个必答问题要么给出带引用的答案，要么明确写入 `missing_information`。
- schema 或证据校验失败时执行纠正重试；耗尽后标记该 AgentSkill 失败，追踪 Agent 不得代写其专业结论。

### 5.3 两段式推理（质量底线）

每个重专家禁止「边算边写」一次出评级：

1. **抽取**：只输出事实清单与出处，禁止评价；清单落盘为 `ProcessRecord.facts[]`  
2. **判断**：只基于该清单 + 规则命中结果；清单没有的内容不得出现；落盘为 `judgment_basis` + `trace_steps`  
3. **线索单列**：看过但未升格的信息进 `clues[]`，不得写进结论  

决策前可做主张核查：报告中的定量句子必须能对上字段或规则 ID，对不上则删除或降置信度。完整过程合同见 §6.3。

---

## 6. 合同（一等公民）

实现前先冻结 schema。下列为逻辑字段，可用 Pydantic 落地。

### 6.1 Report（专家输出）

共用骨架：

| 字段 | 说明 |
|------|------|
| `role` | fundamental / technical / sentiment / macro |
| `score` | 1–5，本维质量或强度 |
| `stance` | buy / hold / sell / abstain |
| `summary` | 短结论 |
| `citations[]` | `{type, id, note}`，type 为 field / rule / kb |
| `risks[]` | 本维风险 |
| `degraded` | 数据不足时 true |
| `abstain` | 本维拒绝下结论 |

`Report` 是给人看的本维结论，不是过程日志。过程字段（facts / clues / judgment_basis / trace_steps）放在外层 `ProcessRecord` / `AnalysisDeliverable`，不塞进 Report，避免和业务合同缠在一起。

扩展（按 Agent，缺则不要编）：

- 基本面：`valuation`, `financial_health`, `earnings_quality`
- 技术面：`trend`, `setup`, `key_levels`
- 情绪：`event_flags[]`, `crowd_risk`（无事件则承认不足）
- 宏观：`cycle_tag`, `market_bias`, `relevance_to_stock`

### 6.2 Decision（决策输出）

| 字段 | 说明 |
|------|------|
| `rating` | buy / hold / sell / abstain |
| `value_score` | 值不值得持有 |
| `timing_score` | 现在动手是否合适 |
| `confidence` | 0–1，冲突和缺失时由规则打折 |
| `rationale` | 核心理由，须能指向 citations |
| `disagreements[]` | 四维冲突点 |
| `falsifiers[]` | 什么新事实出现则应改评级 |
| `risks[]` | 2–3 条 |
| `advice_one_liner` | 给用户的一句话，须与 rating 一致 |
| `master_refs[]` | 命中的规则/条文 ID，不是装饰语录 |

### 6.3 ProcessRecord（过程性日志，必须落盘）

「过程可回放」指三个月后能打开当时记录，回答四件事：凭什么判、走过哪些步骤、证据是哪条、哪些只是线索没升格。  
这不是把模型隐藏思维存下来。DeepSeek `reasoning_content` 只在当轮内存回传 Tool Call，写入 Archive 时剥离（见 `SQLite持久化设计.md`）。

名词对照：

| 要存的东西 | 结构化落盘 | 禁止当作替身 |
|------------|------------|----------------|
| 决策依据 | `Decision` 的 rationale / citations / falsifiers / risks / disagreements / dimensions_*；各维 `judgment_basis`；当时 snapshot 的 `artifact_ref` 与 `rule_hits` | 事后股价涨跌 |
| 思维链路 | 有序 `trace_steps[]`（工具→抽取→规则→升格/拒绝线索→判断）+ `facts[]` + 已存的 `context_messages` 工具协议 + `reflection` | `reasoning_content`、模型内部独白 |
| 证据 | `Report.citations[]`（`EvidenceRef`）+ 对应 `facts[]` + snapshot/公告 artifact 原文 | 没绑 id 的形容词、定量句子 |
| 线索 | `clues[]`：看过但未升格为证据的新闻/弱信号；必须标明 `promoted_to_evidence` | 把线索直接写成结论 |

合同（逻辑字段，进 artifact，不进 `reports` 正文）：

```python
class ExtractedFact:
    id: str
    kind: "field" | "rule" | "event" | "computed"
    id_ref: str                 # 字段名 / rule_id / event_id
    value: Any                  # 必须来自工具或代码规则，禁止手写
    source: str                 # 工具名，如 get_indicator_snapshot
    as_of: datetime
    artifact_ref: str | None

class Clue:
    id: str
    kind: "news" | "rumor" | "weak_event" | "other"
    text: str                   # 标题原文或短摘要
    source: str                 # 如 search_news
    observed_at: datetime
    promoted_to_evidence: bool
    why_not_promoted: str | None
    promoted_citation_id: str | None

class JudgmentBasis:
    stance: "buy" | "hold" | "sell" | "abstain"
    score: int                  # 1–5，须与 Report 一致
    used_fact_ids: list[str]
    used_clue_ids: list[str]
    rejected_clue_ids: list[str]
    missing_information: list[str]
    rationale_points: list[str] # 短句，每句应对 used_fact_ids

class TraceStep:
    seq: int
    action: "tool_call" | "extract" | "rule" | "promote_clue" | "reject_clue" | "judge" | "abstain"
    name: str | None            # 工具名或 rule_id
    fact_ids: list[str]
    clue_ids: list[str]
    note: str | None

class ProcessRecord:
    schema_version: str = "1"
    agent_name: "fundamental" | "technical" | "sentiment" | "macro" | "tracking"
    run_id: str
    session_id: str
    as_of: datetime
    facts: list[ExtractedFact]
    clues: list[Clue]
    judgment_basis: JudgmentBasis
    trace_steps: list[TraceStep]
    reflection: {
        "what_worked": list[str],
        "what_was_missing": list[str],
        "process_errors": list[str],
    }
```

绑定规则：

- 非弃权 `Report.citations` 的每一条，必须能在同一次 `facts[]` 里找到对应 `id_ref`
- 新闻默认进 `clues`；升格为证据时 `promoted_to_evidence=true`，且 citations 里有 `kind=event` 或 `field`
- 无有效事件时情绪维仍要留下 `clues`（若看过新闻）和 `facts`（如 `no_material_event`），再弃权
- K 线不足时技术面仍要列出 `bars_used` / `missing_fields` 再弃权
- `trace_steps` 按实际发生顺序写，是可回放思维链路；禁止把 `reasoning_content` 拷进去
- `LoopTracer` 只在当次进程内存，不算持久过程日志

分析 Agent 的 `AgentFinalResponse.output` 必须同时有 `report` 和上述过程块（或由编排从 output 组装成 `ProcessRecord` 再落盘）。`reflection` 走 `AgentFinalResponse.reflection`。

综合决策另存一份拍板依据：写满 `Decision` 已有字段，审计 `decision_written` 带上各维 `report` id 和 `ProcessRecord` artifact_ref。禁止用后来涨跌回写。

跟踪只追加时间线，不改首次 `ProcessRecord` / `reports` / `decisions`。每次被叫醒再写一份新的过程记录，并在 `agent_dispatched` 记下 trigger_reason、问了什么、input_refs。

落盘位置见 `SQLite持久化设计.md` §6.8。分 Agent 实现清单见 `分Agent实现任务.md`。

### 6.4 RunSnapshot（研究与跟踪共用时间线）

每次研究或跟踪追加一条，**只追加不改历史**。

```json
{
  "ts": "2026-08-18T15:00:00+08:00",
  "mode": "research | track_hour | track_day | track_week",
  "stock_code": "000858",
  "thesis_id": "run_20260818_001",
  "inputs_used": [],
  "citations": [],
  "value_score": null,
  "timing_score": null,
  "stance": "hold",
  "confidence": 0.62,
  "falsifiers": [],
  "triggers_hit": [],
  "dispatches": [],
  "advice": "unchanged | review | invalidate",
  "delta_from_last": "",
  "abstain": false,
  "notes": ""
}
```

`notes` 可写自由想法；**评测、跟踪闸门、Skill 晋升只读结构化字段**。

跟踪日志的 `thesis_id` 指向第一次完整分析。优化用的是当时 `inputs_used` / `citations` / `falsifiers`，不是事后涨跌。

### 6.5 TrackingContext（共享只读上下文）

每只股票/每个 thesis 维护一个由应用层装配的共享追踪上下文。5 个 Agent 都能在跟踪期读取它，但不能直接写入。它属于业务上下文，不等同于运行时 L0–L4 压缩层：

```json
{
  "stock_code": "000858",
  "thesis_id": "run_20260818_001",
  "as_of": "2026-08-18T17:00:00+08:00",
  "baseline_decision_ref": "decision_001",
  "latest_decision_ref": "decision_004",
  "current_thesis": "结构化摘要",
  "falsifiers": [],
  "latest_market_delta": {},
  "new_events": [],
  "recent_timeline_refs": [],
  "approved_methodology_refs": [],
  "agent_state_summaries": {
    "fundamental": {},
    "technical": {},
    "sentiment": {},
    "macro": {}
  }
}
```

`TrackingContext` 只保存可审计摘要和引用，不保存模型隐藏推理过程。不同股票或 thesis 的上下文严格隔离。

### 6.6 AgentState（每个 Agent 的私有追踪记忆）

基本面、技术面、情绪、宏观、追踪 5 个 Agent 均维护自己的版本化 state：

```json
{
  "agent": "fundamental | technical | sentiment | macro | tracking",
  "stock_code": "000858",
  "thesis_id": "run_20260818_001",
  "version": 3,
  "updated_at": "2026-08-18T17:00:00+08:00",
  "memory_summary": "该 Agent 对当前标的的压缩记忆",
  "active_hypotheses": [],
  "key_evidence_refs": [],
  "open_questions": [],
  "falsifiers_watched": [],
  "prior_output_refs": [],
  "lessons": [],
  "pending_tasks": []
}
```

Agent 不直接写数据库。每次运行在交付物中返回 `state_patch` 的业务字段（set/append/remove）。`base_version` 和 `loop_round` 是运行时记账，由 LocalScheduler 盖章，不让模型填写。调度器校验字段白名单和引用后提交，并保留旧版本。真正的版本冲突（Store 中的 version 与调度器内存不一致）不覆盖，重新加载最新 State 后恢复、重新运行或进入人工处理。

这里的 `memory_summary`、`lessons` 和 `reflection` 都是简洁、结构化、可给真人审阅的结论，不要求也不保存模型的原始思维链。

### 6.7 TrackingDeliverable（追踪 Agent 固定交付物）

追踪 Agent 每次被唤醒后，可以自主取数、检索方法论、决定调用哪些分析 Agent、反思本次工作并生成用户输出，但最终必须交付同一个结构：

```json
{
  "status": "unchanged | review | invalidate",
  "work_summary": "本次检查了什么、发现了什么",
  "evidence_refs": [],
  "triggers_hit": [],
  "agent_skill_calls": [
    {
      "agent": "fundamental | technical | sentiment | macro",
      "question": "需要核查的具体问题",
      "required_context_refs": [],
      "reason": "现有信息为何不足",
      "status": "success | failed"
    }
  ],
  "decision_required": false,
  "user_output": {
    "title": "本次跟踪更新",
    "summary": "给用户的简明结论",
    "holding_advice": "unchanged | review | invalidate",
    "key_changes": [],
    "next_watch_items": []
  },
  "reflection": {
    "what_worked": [],
    "what_was_missing": [],
    "process_errors": [],
    "methodology_proposals": []
  },
  "state_patch": {
    "base_version": 3,
    "set": {},
    "append": {}
  },
  "next_check_suggestion": {
    "urgency": "low | medium | high",
    "reason": ""
  }
}
```

固定的是交付结构和证据要求，不固定内部工作轮次。追踪 Agent 通过自己的 ResourceRegistry 和 CallOrchestrator 调用 `AgentSkill`，取得局部报告后可继续工作，直到产出最终 `TrackingDeliverable`。每次最终交付必须包含用户可读输出、工作反思和自身 `state_patch`；没有证据引用时只能弃权或标记信息不足。

---

## 7. 数据、规则与工具

### 7.1 数据层

AKShare 封装为统一适配层，超时、重试、缺失标记。专家拿到的是切片，不是各自重复拉全量。

预计算（代码算，不交给模型）：财务同比、估值分位、同行对比（有则必须给）、均线/MACD/RSI/量比、关键位。

A 股财务法医（规则打标，优先于宏观 Agent）：净利润 vs 经营现金流、商誉/应收/存货异常、非经常性损益占比、大股东质押或减持、审计意见。命中则进入风险清单。

情绪通道以事件为准：业绩预告、问询函、减持、诉讼、回购、立案。无事件则情绪维弃权或低置信，不用标题情感分充当核心卖点。

### 7.2 金融侧交付物

金融侧按四个分析角色分别收集文献与监管原文，清单见 `知识库资料-基本面Agent.md`、`知识库资料-技术面Agent.md`、`知识库资料-情绪事件Agent.md`、`知识库资料-宏观Agent.md`（总说明见 `知识库条目清单-经管对照查找.md`）。可检索知识库正文由 Agent 侧根据这些资料编写；Skill 仍由 Agent 侧编写。

Skill（何时检索、如何引用、输出纪律）由 Agent 侧编写，不由金融侧设计。

检索应数据触发：例如「利润增、经营现金流不增」才召回盈利质量相关条文，禁止每票固定关键词召回同一批语录。

Agent 侧：Skill 检索已批准条目 → citations 指向 `kb_id` / `rule_id` / 字段 → 模型只解释检索命中的知识与数据，不把知识库条目当成买卖信号。

---

## 8. 跟踪设计

用户将股票标为关注或买入，并选择频率：小时 / 天 / 周。三种频率 **任务重量不同**，不是同一套分析的 cron 密度差。

| 频率 | 系统做什么 | 用户看到什么 |
|------|------------|----------------|
| 小时 | 行情阈值 + 公告快扫；追踪 Agent 自主选择是否及调用多少分析 Agent，但提示其尽量少调用 | 无变化则静默；触发时给出增量建议 |
| 天 | 追踪 Agent 对照基准 snapshot：价、事件、falsifiers | 短更新：thesis 是否还在 |
| 周 | 追踪 Agent 可选择一次轻量复评（不满配四个分析 Agent） | 持有状态 + 相对上周差异 |

流程：

```text
选定 → 存基准 RunSnapshot（第一次完整分析）
  → 到点拉增量
  → 装配 TrackingContext + 读取追踪 Agent 私有 state
  → 规则预筛 + 追踪 Agent：unchanged / review / invalidate
  → 追踪 Agent 通过 AgentSkill 选择性唤起 0～4 个分析 Agent
  → 被调 Agent 读取各自 state，收集局部复评与 state_patch
  → 必要时进入综合决策环节
  → 校验 TrackingDeliverable，提交各 AgentState 新版本
  → 追加一条带 ts 的 JSON，并输出给用户
```

### 8.1 追踪 Agent

追踪 Agent 是持续跟踪阶段的调度角色。它不提供基本面、技术面、情绪或宏观专业结论，也不直接替代最终决策。它读取：

- 初始 thesis、最近一次有效 Decision 与 `falsifiers`
- 本周期的价格、成交量、公告/新闻等增量
- `TrackingContext`、自身 `AgentState`
- 从上次跟踪至今的专家报告、四个分析 Agent 的状态摘要与触发历史

它可以在内部自主规划、调用、复查与反思，但对外必须输出第 6.7 节定义的 `TrackingDeliverable`。调度中的每个问题、最终用户输出、反思和自身 `state_patch` 都是必需交付项。每次调度还必须按第 6.3 节追加一份过程记录（为何叫醒、问了什么、用了哪些 facts/clues）。

调度采用「事件路由」，避免无差别重跑：

- 新财报、业绩预告、审计或现金流异常 → 基本面专家
- 跌破关键位、异常放量、波动率突变 → 技术面专家
- 减持、回购、问询函、立案、重大舆情 → 情绪专家；涉及财务实质时同时调基本面
- 利率、监管或行业级重大政策 → 宏观专家；仅在与该股票有明确相关性时触发
- 多个关键事件同时出现、原 thesis 被证伪，或局部报告冲突 → 综合决策环节

追踪 Agent 只能调用其私有 ResourceRegistry 中已注册的白名单资源。四个分析 Agent 以 `AgentSkill` 形式注册，由追踪 Agent 的本地 CallOrchestrator 执行；追踪 Agent 不能修改目标 Agent 的 Prompt、State 或内部资源。每次调用必须记录 `trigger_reason`、目标 Agent、问题、输入引用、结果和时间戳。使用冷却时间和去重键防止同一事件持续存在时无意义地重复调用，但不固定其内部规划轮次。

规则预筛负责确定性条件与成本控制；追踪 Agent 负责处理语义事件，并自主决定该问哪些分析 Agent 以及调用数量。系统不写死小时级只能调用某一个 Agent，但在追踪 Agent 的 Prompt 中明确：

- 优先复用最新有效报告和增量数据
- 能不调用专家就不调用
- 能调用一个专家解决就不调用多个
- 只有事件跨维度、thesis 可能失效或局部结论冲突时，才扩大调度范围
- 每个调度都要说明「为什么现有信息不足」和「新增调用预计解决什么问题」

每个配置的跟踪时间点都由追踪 Agent 接收规则预筛结果、压缩后的增量摘要、`TrackingContext` 和自身记忆。它可以选择不调用任何分析 Agent并直接输出 `unchanged`。高风险事件进入局部复评。小时级同样允许追踪 Agent 自主决定调用范围；系统提示其优先少调用，并用冷却时间、去重和超时熔断控制无效消耗，而不是固定推理步数。

持有建议三档，默认不要每天重新给出买/卖：

- `unchanged`：证伪未触发，维持原 thesis  
- `review`：新事件或价格条件触发，建议重评  
- `invalidate`：基准里写明的 falsifiers 已出现  

`sell` / 减仓话术仅在 `invalidate` 或用户明确索取操作建议时出现，且必须引用基准 JSON 中的 `falsifiers`。

禁止日常无差别重跑四个分析 Agent；允许追踪 Agent 因明确事件选择性调度。禁止用「建议持有后下跌」作为改策略的标签。

---

## 9. 投研方法与决策纪律库

系统维护一套同时服务 Agent 与真人的「投研方法与决策纪律库」。它不只是运行时 Skill 集合，也是一份可检索、可审计、可阅读的方法论手册。

追踪 Agent 是该知识库的管理者，负责：

- **蒸馏**：读取经管交付、已转成 Markdown 的一章/一份公告，写成候选条目（与跟踪调度同一 Agent、不同 Task；一次不得喂全书）
- 从跟踪日志和复盘 Case 中发现重复出现的过程问题
- 整理候选方法、检查清单、反例和适用边界
- 检索已有条目，去重并检查潜在冲突
- 创建更新提案、维护关联 Case 和版本变更说明
- 在跟踪与调度时检索已批准条目，为分析 Agent 提供相关方法
- 为真人生成清晰的 Markdown/页面视图，而不是只保存机器 Prompt

追踪 Agent 可以直接写入 `candidate` 区，但不能自行把候选变成 `approved`，也不能自动修改买卖权重或其他 Agent 的 Prompt。正式生效仍需规则校验和人工审核。蒸馏 Task 与跟踪 Task 共用这条闸门。

### 9.1 条目结构

```json
{
  "id": "method_fundamental_001",
  "title": "利润增长需要现金流验证",
  "type": "principle | rule | checklist | anti_pattern | lesson",
  "scope": ["fundamental", "tracking"],
  "trigger": "净利润增长但经营现金流下降",
  "action": "检查应收账款、存货和非经常性损益",
  "rationale": "利润增长可能缺乏现金支持",
  "evidence_required": ["net_profit_growth", "operating_cashflow_growth"],
  "exceptions": [],
  "source_refs": [],
  "case_refs": [],
  "status": "candidate | approved | retired",
  "version": 1,
  "created_at": "2026-08-18T16:50:00+08:00",
  "updated_at": "2026-08-18T16:50:00+08:00",
  "change_note": "初次从复盘案例归纳"
}
```

方法论条目是给 Skill **检索**用的知识正文，不直接作为买卖信号，也不替代 Skill。经管同学按四个分析角色分头找完整资料，清单见 `知识库条目清单-经管对照查找.md`。

### 9.2 Case 与晋升闸门

跟踪与复盘产出 Case，不直接改 Prompt 或买卖权重。Case 至少包含：当时 thesis、falsifiers、后来发生的事实（含价格是否打中证伪条件）、过程错误（漏了**当时已有**的证据、不该判却判了）、是否仍成立（基于新事实）。后来涨跌是结果层对照，不是旧报告的 citations。

候选条目晋升为 `approved` 的条件：

- 同类 `process_error` 达到次数阈值
- 晋升理由必须能指向当时可获得却未用的证据或未写清的 falsifier；**不得仅凭单笔事后盈亏**
- 有明确触发条件、所需证据、适用边界和反例
- 与已有条目不重复、不冲突
- 点时评测未出现明显退化
- 人工审核通过

所有修改生成新版本，不覆盖旧版本。历史报告必须能够还原当时使用的条目版本。

### 9.3 真人阅读界面

界面提供独立的「方法论库」页面，至少支持：

- 按基本面、技术面、情绪、宏观、追踪筛选
- 查看方法说明、适用条件、例外、来源和关联 Case
- 查看版本历史和变更原因
- 查看候选条目并执行批准、驳回、编辑
- 从具体报告跳转到其引用的方法论条目

机器检索使用结构化字段和向量/关键词索引；真人默认看到整理后的标题、摘要、证据要求、适用边界和案例，不直接暴露内部 Prompt。

### 9.4 个人投资决策手册

除可检索的方法论库外，系统还提供一份面向普通用户的「个人投资决策手册」。它回答的是可执行问题，而不是展示 Agent 的内部知识：

1. **什么样的股票进入候选池**：能力圈、盈利质量、财务健康、估值区间、治理与重大风险检查
2. **什么时候可以买**：价值条件、时机条件、信息充分度、分批计划和买前证伪条件
3. **买入后怎么跟踪**：需要观察的指标、公告与事件、小时/天/周跟踪分别看什么
4. **什么时候继续持有**：原 thesis 未失效、关键指标仍成立、短期价格波动与基本面变化的区分
5. **什么时候减仓或退出**：thesis 被证伪、基本面恶化、估值极端、仓位或流动性风险；卖出理由不得只写「股价下跌」
6. **什么时候不做决定**：证据不足、超出能力圈、事件尚未确认或模型结论不稳定时应弃权
7. **风险与复盘**：仓位、分散、最大可承受损失、决策日志和事后过程复盘

手册采用「原则 → 检查清单 → 示例 → 反例 → 记录模板」的格式。每条规则必须关联正式方法论条目 ID 和版本，不能引用 `candidate` 内容，也不得承诺收益。

追踪 Agent 可以根据已批准条目和 Case 提交手册修订草稿；人工审核后才发布新版本。用户必须能查看当前版本、更新时间、变更摘要和引用来源。手册内容是通用投研教育与决策纪律，不替代针对具体用户风险承受能力的个性化投资建议。

---

## 10. 准确性机制（不含知识库/计算器/权重调参时仍要做）

- 抽取与判断分离  
- 主张核查  
- 信息不足则弃权，置信度规则打折  
- 30–50 个点时评测样本（禁止用未来年报评过去时点）  
- 同一输入多次采样测稳定性；分歧则降置信或改为 hold  
- 可选双模型交叉：只对「同意/反对 + 哪句不成立」投票  
- 用标注过的好/坏案例做 few-shot（输出纪律，不是大师语录）  
- 错误分类后针对性改：周期高峰当永续、一次性收益、无事件强情绪、缺数据高分、前后矛盾  

评测看过程：该不该弃权、风险有没有点到、句证是否一致、价值与时机是否分开。自动化通过标准不看随后几天涨跌。人审复盘可以同时看结果层（见 §9.2）。

---

## 11. 非目标（第一期）

- A2A、LangGraph、专家互辩、对等网调度  
- 自动下单、对接券商  
- 用盈亏自动改 Prompt、权重或方法论条目  
- 宏观主导决策  
- 新闻标题情感作为核心卖点  
- 小时级全量四专家分析  
- 追踪 Agent 自主批准候选条目或修改正式方法论  

---

## 12. 分工与分期

**Agent 侧（本仓库）：** 单实例 Agent runtime、应用 workflow、合同、数据适配、预计算与规则执行、专家 Prompt、决策、快照落盘、跟踪闸门、UI。

**金融侧：** 规则表 + 条文、规则适用边界、评测样本的金融标注（可选）。

| 期 | 交付 |
|----|------|
| P0 | 业务合同冻结 + 基本面 Agent + 简化决策 + SQLite 落盘 + `analyze` CLI（详见 `P0首次研究切片设计.md`） |
| P1 | 跟踪（小时/天/周）+ TrackingContext/AgentState + 追踪 Agent 固定交付物 + 自主调度 + 时间线 UI |
| P2 | 情绪事件通道、轻量宏观、主张核查、小评测集 |
| P3 | Case 库、方法论库真人页面、个人投资决策手册、追踪 Agent 更新提案与人工晋升闸门 |

---

## 13. 模块草图

```text
insightagent/
  contracts/          # Report, Decision, RunSnapshot, TrackingContext, AgentState, TrackingDeliverable
  data/               # AKShare 适配、预计算、事件拉取
  rules/              # 金融交付的规则执行器
  kb/                 # 方法论、规则、条文、候选与版本
  agents/
    fundamental.py
    technical.py
    sentiment.py
    macro.py
    tracking.py         # 追踪 Agent：增量判断与调度计划
  workflow/
    decision.py         # 综合决策环节
  orchestrator.py     # 应用层首次研究 workflow，不是全局 Agent 调度器
  tracking.py         # 频率心跳、闸门、追加日志
  store/              # snapshot、AgentState 版本与追加日志
  eval/               # 点时样本与过程指标
  methodology.py      # 真人查询、审核和版本管理
  handbook.py         # 个人投资决策手册的生成、发布与版本
  app.py              # Streamlit
```

专家对外保持：`analyze(ctx) -> Report`。编排串起来即可。日后若需 A2A，只换运输层，不改合同。

---

## 14. 风险与约束

| 风险 | 应对 |
|------|------|
| AKShare 变动 | 适配层 + 备用源 |
| 模型空写管理层、宏观故事 | 证据绑定 + 弃权 |
| 跟踪变成荐股 | 三档 advice + 免责声明 |
| 日志无法回放 | 强制 inputs_used / citations / falsifiers |
| 自我进化过拟合 | 涨跌不进晋升理由；模型不能 apply |

---

## 15. 一句话

InsightAgent 用薄 workflow 调度四个不对称专家；专家对数据和规则负责，对彼此不交谈；决策把价值与时机拆开，并把证伪条件写进快照；跟踪只对照快照做增量，过程 JSON 可回放；策略升级靠 Case 过闸，不靠涨跌自学。
