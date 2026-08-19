# 分 Agent 实现任务（技术面 + 情绪）

> 状态：可转交实现  
> 日期：2026-08-19  
> 运行时：**复用现有 `AgentInstance`，禁止另起框架**  
> 模板：`src/insightagent/fundamental_agent.py`

系统已冻结 **4 个分析角色 + 1 个追踪角色**。本次只把其中两个分析角色交给你实现：

| 四角色 | 重量 | 本次谁做 |
|--------|------|----------|
| 基本面 `fundamental` | 重 | 已有，不要改 |
| **技术面 `technical`** | 重 | **任务 A，你写** |
| **情绪 `sentiment`** | 中 | **任务 B，你写** |
| 宏观 `macro` | 轻 | 不做 |
| 追踪 `tracking` | 中 | 不做（调度方以后写） |

两个分 Agent 都是分析角色，不是追踪、不是综合决策。框架、落盘表、audit API 都已有；你负责产出能被这些表吃进去的 `Report` / `state_patch`。不要自己建日志库。

---

## 0. 框架怎么用

和基本面同一条路，不要包装：

```python
agent = AgentInstance(name="technical", llm_adapter=..., config=technical_runtime_config())
register_technical_tools(agent, context)
final = await agent.run(user_query, session_id=..., business_context={...})
report = parse_technical_report(final.output)  # AgentFinalResponse.output.report
```

情绪把 `name` 换成 `"sentiment"`。`name` 必须是上表角色名，后面 `reports.agent_name`、session、审计都靠它区分。

禁止新建 Loop、Agent 基类、LangGraph/A2A/MCP、AgentSkill、自己的 LLM adapter。

不要动：`runtime.py`、`resources.py`、`llm.py`、`state.py`、`market.py`、`akshare_map.py`、`tools.py`、`workflows/`、`decision.py`、`fundamental_agent.py`、`persistence.py`。

没有「主 Agent」对聊。调用方只有 Python workflow（首次）和以后的追踪 Agent（再 `agent.run` 一次）。分 Agent 之间不通信。

---

## 任务 A → 角色「技术面」`technical`

### 职能

回答：现在是什么趋势、有没有结构、关键位在哪、量价是否异常。给本维 `stance`（偏多/中性/偏空/弃权）和 `score`（趋势清晰度 1–5）。

停止：指标够用并下判断，或 K 线不足则弃权。

禁止：估值/财务、新闻情绪、自己算 MA/MACD/RSI、预测买卖价。`timing_score` 不是你的字段。

### 工具清单（只读 context，不打 AKShare）

编排以后会把算好的切片放进 context；单测用 fixture。工具名与现有合同一致：

| 工具 | 合同 | 干什么 |
|------|------|--------|
| `get_indicator_snapshot` | `IndicatorSnapshot` | 主工具：ma5/10/20/60、macd、rsi14、volume_ratio、bars_used |
| `get_price_snapshot` | `PriceSnapshot` | 现价 |
| `get_kline_snapshot` | `KlineSnapshot` | 近期 OHLC，只供关键位 |
| `get_artifact` | 同基本面 | 校验 ref 后展开原文 |
| `search_methodology` | 本地 KB | scope 含 `technical`；没有条目返回 `[]` |

不要注册基本面、公告、新闻、宏观工具。Prompt：先调 `get_indicator_snapshot`；`bars_used < 20` 或 `ma20` 空 → 弃权/degraded。

### 代码规则（`technicals.py`，进模型前打标）

`ma_bull_align` / `ma_bear_align` / `macd_pos` / `rsi_overbought` / `rsi_oversold` / `volume_spike` / `insufficient_bars`。现价相对 ma20 在代码里算。关键位只引用 K 线出现过的高/低或均线值。

### 输出

`Report.role = "technical"`，扩展字段 `trend` / `setup` / `key_levels`（等合同补齐）。citation：`kind=field` 或 `kind=rule`。

### 文件

`technical_agent.py`、`technicals.py`、`tests/test_technical_agent.py`。  
符号：`TECHNICAL_SYSTEM_PROMPT`、`technical_runtime_config`、`register_technical_tools`、`parse_technical_report`。

---

## 任务 B → 角色「情绪」`sentiment`

### 职能

回答：有没有足以改变风险认知的 **事件**（减持、增持、回购、问询、立案、诉讼、业绩预告）。不是标题情感分。

无有效事件 → 弃权或 degraded，`crowd_risk` 不得 `high`。

禁止：均线、估值、LPR、用「新闻很多」当过热、编造工具里没有的公告。非弃权不能只靠新闻。

### 工具清单

| 工具 | 合同 | 干什么 |
|------|------|--------|
| `get_event_snapshot` | `EventSnapshot` | 主工具 |
| `search_announcements` | `AnnouncementSearchResult` | 按关键词核公告 |
| `get_holder_changes` | `HolderChangeSnapshot` | 增减持（股数已对齐） |
| `search_news` | `NewsSearchResult` | 仅线索；不能当非弃权唯一证据 |
| `get_artifact` / `search_methodology` | 同基本面 | scope 含 `sentiment` 或 `event` |

不要注册指标/K 线/基本面 snapshot。Prompt：先 `get_event_snapshot`。

### 代码规则（`events.py`）

`has_reduction` / `has_buyback` / `has_inquiry` / `has_earnings_preview` / `no_material_event`。  
`crowd_risk` 只用 `low|medium|high`，且能指向 `event_flags`。

### 输出

`Report.role = "sentiment"`，扩展 `event_flags[]`、`crowd_risk`。citation 优先 `kind=event`（event_id）和 `kind=rule`。

### 文件

`sentiment_agent.py`、`events.py`、`tests/test_sentiment_agent.py`。  
符号：`SENTIMENT_SYSTEM_PROMPT`、`sentiment_runtime_config`、`register_sentiment_tools`、`parse_sentiment_report`。

---

## 过程性日志（这次要写死：存什么、不存什么、你填什么）

复查时要能回答四件事：**当时凭什么判、走过哪些步骤、证据是哪条、哪些只是线索没升格**。这些必须落盘。  
模型肚子里的隐藏思维（DeepSeek `reasoning_content`）**不落盘**——这是已冻结的持久化规则。可回放的「思维」用结构化过程代替，不是把 CoT 原文存下来。

### 名词对照

| 你要的东西 | 落盘形态 | 不落盘的替身 |
|------------|----------|----------------|
| 决策依据 | `Decision.rationale` + `citations` + 各维 Report 的 stance/score + `rule_hits` + 当时 snapshot 的 `artifact_ref` | 事后股价涨跌 |
| 思维 / 推理过程 | **抽取事实清单 `facts[]`** + **工具调用链** + **`reflection`** | `reasoning_content`、模型内部独白 |
| 证据 | `Report.citations[]`（EvidenceRef）+ snapshot/公告 artifact | 没绑 id 的形容词 |
| 线索 | `clues[]`：看过但未升格为证据的新闻/弱信号 | 把线索直接写成结论 |

### 分 Agent 必须在 `AgentFinalResponse` 里交出的过程块

`output.report` 仍是给人看的本维结论。另外 **必须** 带上下面三块（编排会存 artifact + audit，不进 Report 正文以免和业务合同缠在一起）：

```python
# AgentFinalResponse.output
{
  "report": { ... Report ... },

  # 1. 抽取清单 = 可回放的推理过程（先事实后判断）
  "facts": [
    {
      "id": "fact-ma20",
      "kind": "field",          # field | rule | event | computed
      "id_ref": "ma20",
      "value": 73.18,
      "source": "get_indicator_snapshot",
      "as_of": "2026-08-19T07:00:00+08:00"
    }
  ],

  # 2. 线索：看过、记录了、但没有当成核心证据
  "clues": [
    {
      "id": "clue-news-1",
      "kind": "news",           # news | rumor | weak_event | other
      "text": "标题原文或短摘要",
      "source": "search_news",
      "promoted_to_evidence": false,
      "why_not_promoted": "仅新闻标题，无对应公告"
    }
  ],

  # 3. 本维判断依据（从 facts 指向 report.stance）
  "judgment_basis": {
    "stance": "hold",
    "score": 3,
    "used_fact_ids": ["fact-ma20", "rule-ma_bull_align"],
    "used_clue_ids": [],
    "rejected_clue_ids": ["clue-news-1"],
    "missing_information": [],
    "rationale_points": ["现价贴近 ma20，趋势未破"]
  },

  # 4. 有序步骤 = 可回放思维链路（不是 reasoning_content）
  "trace_steps": [
    {"seq": 1, "action": "tool_call", "name": "get_indicator_snapshot", "fact_ids": [], "clue_ids": [], "note": None},
    {"seq": 2, "action": "extract", "name": None, "fact_ids": ["fact-ma20"], "clue_ids": [], "note": None},
    {"seq": 3, "action": "rule", "name": "ma_bull_align", "fact_ids": ["fact-ma20"], "clue_ids": [], "note": "hit"},
    {"seq": 4, "action": "judge", "name": None, "fact_ids": ["fact-ma20"], "clue_ids": [], "note": "hold"}
  ]
}

# AgentFinalResponse.reflection（结构化，给真人看）
{
  "what_worked": ["先读指标再下趋势判断"],
  "what_was_missing": ["ma60 为空"],
  "process_errors": []          # 例如「差点用新闻当证据」
}
```

完整合同名是 `ProcessRecord`，见主设计 `InsightAgent-设计文档.md` §6.3。编排把这块打成 artifact，发 `audit_events.process_logged`。

规则：

- `facts` 里的数字必须来自工具/规则，禁止手写一个工具里没有的数
- 非弃权 `Report.citations` 的每一条，必须能在 `facts` 里找到对应 `id_ref`
- `clues` 升格为证据时：`promoted_to_evidence=true`，并且 citations 里有一条 `kind=event` 或 `field`
- 情绪维：新闻默认进 `clues`；只有对上公告/股东变动后才能进 `facts` 和 citations
- 技术面：K 线不足时 `facts` 仍要列出 `bars_used`、`missing_fields`，再弃权
- `trace_steps` 按实际发生顺序写；禁止把 `reasoning_content` 拷进 facts/clues/report/state/reflection/trace_steps

### 这些过程块存到哪（编排写库，你保证 JSON 齐）

| 内容 | 表 / 位置 | 说明 |
|------|-----------|------|
| 本维结论 | `reports` | `save_report(run_id, "technical"\|"sentiment", report)` |
| facts / clues / judgment_basis / trace_steps / reflection | **artifact** + `audit_events.process_logged` | payload 只存 `artifact_ref` 和摘要（fact 条数、clue 条数、used_fact_ids），全文在 artifact |
| 当时数据切片 | `artifacts` | 指标/事件 snapshot 原文；Report 只引用 ref |
| 工具调用链 | `context_messages` | 框架已存 assistant tool_calls + tool 结果；大结果 L0 外置 |
| 本维记忆 | `agent_states` + `agent_state_history` | `memory_summary`、`key_evidence_refs`、`falsifiers_watched`、已见 event_id |
| 综合决策依据 | `decisions` | 见下一节 |
| 发生了什么 | `audit_events` | 结构化事件，不含 CoT |

`LoopTracer` 只在当次进程内存里，**不算**持久过程日志。要回放以 SQLite + artifact 为准。

---

## 决策时还要多存一份「当时凭什么拍板」

综合决策是系统环节，不是 Agent。它读各维 Report + 各维 `judgment_basis`，写出 `Decision`。除已有 `save_decision` 外，决策过程日志必须能单独打开：

`Decision` 已有、必须写满的依据字段：

| 字段 | 过程含义 |
|------|----------|
| `rationale` | 当时核心理由（指向 citations，不是空话） |
| `citations[]` | 拍板用到的证据，从各维 Report 聚合，带 kind/id/source/observed_at |
| `value_score` / `timing_score` | 价值 vs 时机，分开；缺技术面则 timing 为 null |
| `confidence` | 缺维、冲突时的打折结果（规则算出的数也要能复算） |
| `disagreements[]` | 例如技术面偏多 vs 情绪减持 |
| `falsifiers[]` | 当时写下的证伪条件 |
| `risks[]` | 2–3 条 |
| `dimensions_used` / `dimensions_missing` | 用了谁、缺了谁 |
| `advice_one_liner` | 必须与 rating 一致 |

决策审计 `decision_written` 的 payload 最少：

```text
rating, confidence, value_score, timing_score
dimensions_used, dimensions_missing
report_refs: {fundamental, technical, sentiment}   # reports 表 id
process_artifact_refs: {technical, sentiment}     # 各维 facts/clues artifact
snapshot_refs
citation_ids
disagreements
falsifiers
as_of
```

这样三个月后能回答：当时技术面看了 ma20=…、情绪把哪条新闻降为线索、决策因为缺宏观把置信度打到多少。  
禁止用「后来跌了」回写这条日志。

P0 现在 missing 含 technical/sentiment/macro。你交作业后 used 纳入对应维；宏观仍 missing，rationale 必须写明。

---

## 追踪时的过程日志（追加，不改首次研究那条）

跟踪只 **追加** 时间线。依据永远是 **当时看得到的** inputs、facts、citations、falsifiers。

事件路由（调度方以后实现）：破位/放量 → 技术面；减持/回购/问询/立案 → 情绪。

每次跟踪最少落盘：

| 日志 | 必须有的过程信息 |
|------|------------------|
| 时间线条 `RunSnapshot` | ts，mode，thesis_id，advice=`unchanged\|review\|invalidate`，delta_from_last，`inputs_used`（当时价、事件 id、snapshot ref） |
| `agent_dispatched` | trigger_reason，target_agent，问了什么，input_refs，去重键 |
| 分 Agent 新产出 | 新的 Report + 新的 facts/clues/judgment_basis artifact（对照上次 key_levels / 已见 event_id） |
| `agent_completed` | stance、score、thesis_impact、used_fact_ids、新线索是否升格 |
| `TrackingDeliverable` | work_summary，evidence_refs，triggers_hit，agent_skill_calls（谁、问什么、为何现有信息不足、结果），reflection |
| 新 `decisions` 行 | 仅当 `decision_required=true` 时追加，不覆盖首次 decision |
| State 新 version | 更新本维记忆；history 保留旧版 |

`agent_skill_calls[]` 就是追踪侧的过程链：为什么叫醒、问了哪题、引用哪条证据、专家答了什么。不要只记「调用了技术面」四个字。

你为跟踪预留的：`state_patch` 写上次趋势、关键位、已见 `event_id`。首次 Prompt 仍按「完成本维分析」；以后 user_query 多带问题也还是这次框架的 `agent.run`。

---

## 明确不存

- DeepSeek `reasoning_content` / 隐藏思维链
- API Key、密钥、cookie
- 未外置的全量 K 线、公告全文（只存 artifact_ref）
- 事后盈亏、事后涨跌（不能当过程标签）
- `LoopTracer` 内存事件（除非编排另做导出，默认不入库）

---

## 共用约定

- 两段式：先抽取后判断，写进 System Prompt
- 最终必须是 `AgentFinalResponse`；弃权则 `status=abstained`
- 工具只读 context 里已算好的 snapshot
- FakeLLM 单测即可，不必 live DeepSeek
- 本维 summary 不要写另一维结论（技术面不谈 PE，情绪不谈均线）

## 验收摘要

**技术面：** 调 `get_indicator_snapshot` 得到 `role=technical` 的 Report；K 线不足弃权；均线规则单测通过。  
**情绪：** 减持/回购 fixture 有 flags 和 citations；无事件弃权；仅新闻不得非弃权买卖。  
**日志：** 不自己写 SQLite。`output` 必须带齐 `report` + `facts` + `clues` + `judgment_basis` + `trace_steps`，`reflection` 走顶层字段。编排会 `save_report` 并写 ProcessRecord artifact / `process_logged`。`AgentInstance.name` 与角色名一致。
