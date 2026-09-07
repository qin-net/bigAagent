# 多 Agent 协作与闸门

> 系列 3/3。接 `Agent框架-02-状态记忆与资源.md`。  
> 设计原文：`Agent运行时框架设计.md`、`InsightAgent-设计文档.md`、`知识库与追踪-架构.md`

多 Agent 最容易做成群聊：大家互相看发言，再选一个「主席」总结。InsightAgent 冻结的是反面：**专家不互辩、不共享可写 State；系统里没有第六个总控 Agent。**

---

## 1. 先数清有几个 Agent

只有五个业务实例：基本面、技术面、情绪、宏观、追踪。

综合决策、编排、校验、落盘、报告渲染是 **Python workflow**。不要再包成「决策 Agent」或「编排 Agent」。否则你会得到第六个会写字的角色，却没有比代码更强的合同。

重量可以不对称（宏观轻、基本面重），视角必须隔离。专家只读本轮快照切片和已批准卡片，不读用户原话，不读 L0 文献全文。

---

## 2. 两种叫醒方式，不要合成一种图

**首次研究 / 按维反馈重跑** —— 应用层并行：

```text
Application Workflow
  → concurrent:
       FundamentalAgent.run()
       TechnicalAgent.run()
       SentimentAgent.run()
       MacroAgent.run()
  → 校验交卷与证据绑定
  → 综合决策（模板与规则，不是第五票）
  → 落盘 RunSnapshot
```

Workflow **不进入** 任一 Agent 内部 Loop，也不共享 `AgentState`。四个 `run()` 只是四个自治实例同时干活。

**追踪** —— 一个追踪实例当调用方：

```text
TrackingAgent.run()
  本轮 Task = 对照 thesis（维持 / 复核 / 证伪）
  工具集里可选：call_fundamental / call_technical / …
  调度器自己决定：叫 0～4 个，或谁都不叫 → unchanged
```

被叫的专家对追踪来说是 `AgentSkill`：一次工具调用，一次交卷。追踪 **不能** 改专家 State、不能改专家 System、不能 `approve` 卡片。

蒸馏和跟踪是 **同一个** 追踪角色、同一套 State 类型。差别只是 Task 和工具：蒸馏读本机章节 md、只写 `candidate`；跟踪默认不读全书。禁止再做一个「知识库 Agent」。

第一期不做 A2A、不做 LangGraph 全局图。需要拓扑时写 Python；需要自主时留在实例内部的 Loop。

---

## 3. 证据绑定是多 Agent 的质量闸门

多专家最常见的假繁荣：四份漂亮报告，引用对不上快照。

合同侧要求：主张能指回本轮工具返回的字段或已检索卡片 id。知识库引用若未出现在本轮 `search_methodology` 结果里，出报告前丢掉。绑定失败应重跑，而不是「先展示再人工对一下」。

过程要落结构化记录（facts / clues / judgment_basis / …），**不落** 思维链原文。可回放的是判断依据，不是模型内心戏。

追踪默认输出维持 / 建议复核 / 原判断失效，而不是每天重写研报、不是小时 cron 荐股。增量对照 thesis，比再开一轮群聊便宜，也更难偷换成高频喊单。

---

## 4. 闸门一览（谁有权写什么）

| 动作 | 谁可以 | 谁不可以 |
|------|--------|----------|
| 写专家 `private_memory` | 该专家自己的 `state_patch` | 追踪、其他专家、用户原文 |
| 写 `UserPreference` | 编排在 remember 闸门之后 | 专家、涨跌脚本 |
| 写方法论 `candidate` | 追踪·蒸馏 | 四专家、用户 |
| `approve` 卡片 | 人（CLI / 审核） | 任何 Agent |
| 改已批准正文 | 新 version + 人审 | 候选覆盖 |
| 综合决策 | 系统环节 | 第五个投票专家 |
| 用户原话进库 / 进证据 | 无人 | 全员 |

版本冲突（`base_version`）不覆盖。旧报告钉死当时的卡片 version。事后盈亏可以对照 `falsifiers`，不得自动晋升方法论、不得回写旧 Report。

这些闸门看起来像产品规矩，其实是多 Agent 能拆开测试的前提：每个写路径只有一个作者，Fake LLM 才能断言「技术面看不见基本面的 lessons」。

---

## 5. 看板只是同一运行时的队列，不是另一套 Agent

页面把分析 / 追踪打成 job：排队、单 worker 领取、超时回收。执行的仍是上面的 `run()`。密钥、重试耗尽、同标的已有 running job，都是队列合同，不要做成前端自己「再调一次模型」。

专家工作台展示的是「某角色在若干标的上的最新私有记忆」，不是「追踪把四专家全员重跑了几次」。数字含义错了，人会以为协作是全量重跑。

---

## 6. 和常见框架差在哪（实现选择，不是口号）

- **不是** 多 Agent 辩论 + 主席总结。交卷向上，综合在代码。
- **不是** 全局 StateGraph 当唯一真相。图在应用层；自主在实例内。
- **不是** 共享记忆 + 用户消息 RAG。三套记忆分权，检索走快照口径和已批准卡片。
- **是** 单实例内核复用：同一套 Loop、压缩、Registry、重试；角色靠工具集和 Task 区分。

若只抄「五个 system prompt」，没有 State/Context 拆分、没有交卷 schema、没有写权限表，那只是五个聊天窗口。框架的技术含量在 **合同和闸门**，不在角色名字。

---

## 7. 三篇合在一起的一条线

```text
能停的 Loop + 可校验交卷
    → State 活过压缩，用户口径不进证据
    → 多个实例只通过 workflow 或 AgentSkill 协作
    → 人批准知识，模型只提议候选
```

要对照代码，从 `runtime.py`（Loop / patch / 压缩）读到 `initial_research.py`（并行四专家）和 `tracking_agent.py`（AgentSkill + candidate），比从角色人设读更快。
