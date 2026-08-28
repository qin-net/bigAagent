# 追踪 Agent 通信协议

> 状态：**待审**（对照当前代码写，不是愿望清单）  
> 日期：2026-08-28  
> 位置：本仓库 `doc/`  
> 只写谁对谁说什么、一轮怎么走。检索算法、卡片合同见 `知识库与追踪-架构.md`。

---

## 1. 总则

五个角色，**不是六个**。追踪是值班长：调度四专家，并对他们的卷做评测、思考和汇总。四维事实仍出自专家。

| 通道 | 谁 | 说什么 |
|---|---|---|
| 编排 → 追踪 | Python `track_thesis` | 本轮 Task JSON（蒸馏或跟踪） |
| 追踪 ↔ 工具 | 追踪自己的 ResourceRegistry | 只读上下文 / 预筛 / 知识库 / 叫醒专家 |
| 追踪 → 专家 | `call_*` 工具 | 一个问题 + 为何现有信息不足 + 本次 schema |
| 专家 ↔ 工具 | 专家自己的 Registry | 本维 snapshot + `search_methodology` |
| 专家 → 追踪 | 工具返回 | 符合本次 `output_schema` 的卷，或失败；追踪不得代写该维事实 |
| 追踪 → 系统 | `submit_final` | `TrackingDeliverable`（含 thinking / 评测 / synthesis） |
| 系统 → 库 | ResearchStore / audit | 追加 track run、时间线、`agent_dispatched` |

禁止：

- 专家之间通信  
- 追踪改专家 System / 工具 / schema  
- 追踪 `approve` 卡片  
- 首次研究叫醒追踪  
- 跟踪轮次读 L0 markdown  

同一角色、同一 System。差别只在 **本轮 Task** 和 **本轮打开的工具集**。

---

## 2. 两套 Task，两套工具

```text
编排装配上下文
        │
        ▼
┌────────────── tracking 一轮 agent.run ──────────────┐
│  System：固定（管理者 + 调度纪律）                     │
│  Task JSON：task=distill | track                    │
│  loop：LLM → 本轮工具 → 结果回上下文 → 再 LLM …        │
│  结束：submit_final                                  │
└─────────────────────────────────────────────────────┘
```

### 2.1 蒸馏 Task（读书、写卡片草稿）

这天追踪是图书管理员，**不能**给四个专家打电话。

| 工具 | 人话 | 你给它什么 | 它还你什么 |
|---|---|---|---|
| `read_source_markdown` | 打开指定的一章 md | 文件路径 | 这一章的正文（太长会截断） |
| `list_allowed_flags` | 问：这维允许写哪些机器字段 | 无 | 如 `cashflow_lag` 这类冻结名 |
| `search_existing_entries` | 查库里有没有类似卡片，避免重复 | 一句话检索 | 已有卡片短摘要 |
| `submit_candidate` | 交一张草稿卡 | id、触发词、对应哪些 flag、短正文 | 入库为 candidate；**不会自动批准** |

### 2.2 跟踪 Task（对照上次 thesis）

这天追踪是值班调度。**不能**读教材 md。工具清单见下一节。

---

## 3. Loop 约定

追踪自己的 runtime loop（`max_loop_round=8`）：

1. 模型看 System + Task + 已有工具结果。  
2. 一次模型回复是 **一轮**。这一轮可以叫多个非专家工具，但 **`call_*` 合计最多 1 次**。  
3. 工具结果写回上下文后，下一轮才开始。  
4. 下一轮可以再叫 **另一个** 专家（先评测上一轮交卷）。  
5. 同一轮里两个 `call_*`：第二个 `status=rejected`，`output=null`。  
6. `submit_final` 不得与其他工具混在同一轮。

这是调度协议，不是「整次跟踪只能叫一个专家」。

---

## 4. 跟踪那天，值班长手头有什么

追踪自己不算财务、不画 K 线、不判减持。维内事实向专家要。  
它要做的是：看档案、看对比单、决定问谁、评测交卷（不只核真假）、思考、汇总分析、交班。

建议顺序：先 `get_tracking_context` 和 `get_prescreen` → 不够再 `call_*`（一轮一个）→ 评测该卷 → 最后 `submit_final` 写出 thinking / expert_evaluations / synthesis。中间可以查卡片或交草稿，不是每次必做。

### 4.1 看档案 `get_tracking_context`

**干什么：** 打开这只票上次研究留下来的摘要。只读，改不了。

**你给它：** 不用给（空参数即可）。

**它还你：** 代码是哪只票、上次结论一句话、证伪条件、四维上次怎么说的、最近增量摘要。  
这不是专业判断。四个专家看不到这份档案，他们只看自己维的 snapshot。

### 4.2 看对比单 `get_prescreen`

**干什么：** 电脑把「上次快照」和「现在快照」对一下，列出变了什么。模型不算这个，也改不了这份单。

**你给它：** 不用给。

**它还你：** 新增/消失的 flag、股价有没有动超过 5%、建议先问哪一维、以及「刚问过同样的事，先别再问」（冷却）。  
建议可以不听，但单子本身是事实。

### 4.3 查纪律 `search_methodology`

**干什么：** 在已批准的短卡片里找和当前事实相关的几条（最多 3 条）。

**你给它：** 一句话 `query` + 哪一维 `scope`（fundamental / technical / sentiment / macro）。

**它还你：** 卡片 id、版本、短文、命中原因。  
真正能不能命中，由当前快照上的 flags 决定；你写的 query 只在能用的卡片里排序。空话搜不出不该出现的卡。

### 4.4 交纪律草稿 `submit_candidate`

**干什么：** 跟踪时发现「过程上该有一条纪律」，交一张草稿。和蒸馏是同一支笔。

**你给它：** 卡片 id、触发词、对应哪些冻结 flag、短正文。

**它还你：** 写成 candidate。人批准之前，分析专家检索不到。追踪自己批准不了。

### 4.5 给专家打电话 `call_*`

四个号，规则一样，只是打给谁不同：

| 工具 | 打给谁 |
|---|---|
| `call_fundamental` | 基本面 |
| `call_technical` | 技术面 |
| `call_sentiment` | 情绪/事件 |
| `call_macro` | 宏观 |

**你必须说清两句：**

- `question`：要他核对的那一件事（一句题，不是「全面再分析」）  
- `reason`：为什么档案和对比单还不够，才需要叫醒他  

**他还你（电话外壳）：**

- `success`：他按你这次下发的 **输出 schema** 交卷  
- `failed`：跑崩或交卷对不上 schema。`output` 为空。你不准替他编  
- `rejected`：这一轮已经打过一个专家电话，第二个挂断  

同一轮只能打通一个。看完他的卷，下一轮可以打给另一个人。专家怎么跑，见 §5。

---

## 5. 被 call 之后，专家怎么走

这是调度协议的核心。专家不是「再跑一遍首次研究」。

### 5.1 一次叫醒长什么样

```text
追踪 call_sentiment(question, reason, output_schema)
        │
        ▼
专家开始自己的 loop（独立 session）
  System：仍是「你是情绪专家」——身份、禁区、本维工具不变
  Task：追踪刚写下的题 + 这次要的输出 schema
        │
        ▼
  专家自己调工具（追踪看不到过程，只收最终卷）
    get_event_snapshot / get_holder_changes / search_methodology …
        │
        ▼
  专家 submit_final.output 必须通过 Task 里那份 schema
        │
        ▼
追踪收到 { status, output } ，写进自己下一轮上下文
```

专家 **看不见** TrackingContext，也 **不能** `call_*` 别的专家。  
数据仍从本维 snapshot 来；用哪套当前行情由编排注入，专家无感。

### 5.2 什么固定，什么由追踪定

| | 固定（角色合同） | 每次 call 由追踪定（任务合同） |
|---|---|---|
| System | 你是哪一维、不许越权、不许改别人结论 | 不改 |
| 工具表 | 仍是该维那几把（snapshot / 搜卡片 / 读本 run 的 artifact） | 不增不删 |
| 题 | | `question` + `reason` |
| **交卷长什么样** | | **`output_schema`** |

旧设计文档写过「不能改输出 schema」。那是把专家锁成首次研究的整份 `Report`。跟踪不该**系统强制**交完整 Report。

追踪当场要什么形状就写什么：只要一句话、要 thesis 影响、要 5 分和 stance，都可以。仓库**不**冻最小卷，也不禁 Report 字段。约束只在：必须是合法 object schema，交卷必须通过该 schema；首次研究仍用固定 Report。

所以：**首次研究继续用固定 Report；跟踪叫醒用追踪当场给的 schema，内容完全由追踪定。** 两套任务，不是两套专家人格。

### 5.3 追踪 → 专家的 Task（要定的输入）

`call_*` 请求扩成：

```json
{
  "question": "问询函是否改变风险感知",
  "reason": "对比单新增 has_inquiry，档案里没有这次问询的结论",
  "required_context_refs": ["flag_added:sentiment:has_inquiry"],
  "output_schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["answer", "thesis_impact", "evidence_refs", "abstain"],
    "properties": {
      "answer": { "type": "string" },
      "thesis_impact": {
        "type": "string",
        "enum": ["none", "weaken", "invalidate", "uncertain"]
      },
      "evidence_refs": {
        "type": "array",
        "items": { "type": "string" }
      },
      "falsifier_hit": { "type": "boolean" },
      "abstain": { "type": "boolean" },
      "missing": { "type": "array", "items": { "type": "string" } }
    }
  }
}
```

`output_schema` 是这份 call 的交卷合同。可以每次不同。追踪想要什么，写在这，不要指望专家自己猜「是不是还要交一份完整 Report」。

DeepSeek strict 工具不能传开放 object，所以 `call_*` 参数里的 `output_schema` 是 **JSON 字符串**，字符串内容才是上面这份 object schema。专家 Task 里会再展开成对象。

专家 user_query 就是这份 Task（外加 `task=reval` / run_id / stock_code / as_of）。**不要**再套首次研究那份 `instruction + 固定 Report schema`。

### 5.4 专家交卷（要定的输出）

电话外壳（给追踪看的）是确定的：

```json
{
  "agent": "sentiment",
  "status": "success",
  "question": "...",
  "reason": "...",
  "error": "",
  "output": { }
}
```

`output` 的形状 **不确定成 Report**，只确定一件事：必须通过这次 `output_schema`。  
编排校验：过了才 `success`；过不了纠正重试，耗尽则 `failed`，`output=null`。追踪不得补写。

`evidence_refs` 仍要能对上专家**这次**工具结果（snapshot 字段、事件 id、本次搜到的 kb id）。schema 里要了引用，就要能钉死。

### 5.5 代码对齐

跟踪叫醒已按 §5.2–5.4 落地：

- 专家 System 只写身份和禁区，不再嵌 `Report`
- Task 带本次 `question` / `reason` / `output_schema`
- `submit_final.output` 按该 schema 校验；过不了则纠正重试，耗尽 `failed`
- 电话返回字段是 `output`，不是 `report`
- 首次研究四维仍走固定 `Report`，不受影响

---

## 6. 追踪收口

`submit_final.output` 为 `TrackingDeliverable`：

```json
{
  "status": "unchanged | review | invalidate",
  "work_summary": "本次检查了什么",
  "thinking": "对照档案、预筛和专家卷，自己怎么想的",
  "synthesis": "汇总后的跟踪分析，不是代写某维 Report",
  "expert_evaluations": [
    {
      "agent": "sentiment",
      "reliability": "high | medium | low | unusable",
      "verdict": "accept | discount | reject | insufficient",
      "gaps": [],
      "notes": "这卷答没答到题、有没有过声称、和 thesis 冲不冲突"
    }
  ],
  "evidence_refs": [],
  "triggers_hit": [],
  "agent_skill_calls": [
    {
      "agent": "sentiment",
      "question": "...",
      "required_context_refs": [],
      "reason": "...",
      "status": "success"
    }
  ],
  "decision_required": false,
  "user_output": {
    "title": "本次跟踪更新",
    "summary": "给用户的一句",
    "holding_advice": "unchanged | review | invalidate",
    "key_changes": [],
    "next_watch_items": []
  },
  "next_check_suggestion": { "urgency": "low", "reason": "" }
}
```

系统会再盖章：

- `agent_skill_calls` **以实际工具记录为准**，不以模型自述为准  
- `triggers_hit` 来自预筛  
- 叫醒成功却没写评测 → 补一条 `verdict=insufficient`（不代写分析正文）  
- 无预筛触发、无成功专家、又想 `invalidate` → 压成 `unchanged`  
- `decision_required` 仅当 `invalidate`  
- **不**自动重跑综合决策  

落盘：新 `RunRecord.mode=track_day`（`parent_run_id`=基线），`tracking_timeline` 只追加；首次 `reports` / `decisions` 不动。  
审计：每次真实/拒绝的叫醒写 `agent_dispatched`（target、question、reason、status）。

入口：`python -m insightagent track <thesis_id或基线 run_id>`。无小时心跳。

---

## 7. 明确没写进协议的

```text
专家互辩
追踪替失败专家编该维事实 / 评分 / stance
把 TrackingContext 整份塞进专家 Prompt
跟踪轮次读 PDF / 全书 md
自动 approve / 改买卖权重 / 改专家 System 或工具表
首次研究第五路并行
小时 cron
跟踪叫醒仍交完整首次 Report   ← 已废，跟踪用追踪下发的 schema
```

---

## 8. 请审的点

1. §5.2：跟踪叫醒由追踪下发 `output_schema`，首次研究仍用固定 Report。**已冻。**  
2. schema 要不要仓库级「跟踪最小卷」？**不要。** 每次完全自由，追踪要 score/stance 也可以。  
3. 专家 System 里还要不要印任何默认交卷形状，还是 System 只写身份+禁区，交卷形状全部来自 Task？  
4. 一轮一个专家、多轮可多个：是否仍按 §3？  
5. `invalidate` 要不要立刻进综合决策，还是只打标？  
