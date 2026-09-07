# 从对话到单 Agent

> 系列 1/3。从 InsightAgent 抽出来的运行时知识，不谈股票细节。  
> 下一篇：`Agent框架-02-状态记忆与资源.md`

很多人把「能调模型的脚本」叫做 Agent。能聊天只说明你接了补全接口。Agent 是：**在约束里自己决定调什么、停在哪、把结果写成可校验的交卷**。

---

## 1. 聊天机器人缺了什么

一次聊天补全是：

```text
messages → 模型 → 一段文字 → 结束
```

业务脚本再包一层，通常是：程序事先排好步骤，每步问一次模型，人负责把答案塞进数据库。模型没有「本轮还没做完」的状态，也没有「我再调一个工具」的合法出口。

单 Agent 把这件事收成一个 **原生 function-calling Loop**（不是 ReAct）：

```text
带着 Task 进入 RUNNING
  while 未交卷且未熔断:
      压缩上下文
      问模型（OpenAI/DeepSeek 风格 tools schema）
      finish_reason=tool_calls:
          普通工具 → CallOrchestrator 可并行执行，结果写回 tool message
          仅 submit_final → 校验 schema，盖章，SUCCESS
          submit_final 与其它工具混在同一轮 → 交卷作废，当非法输出
      finish_reason=stop → 仅作兜底解析；正路是工具交卷
```

模型在 Loop 里选调哪些已登记工具，不是整条流水线的导演。导演是调度器：谁可以调、调几次、失败怎么退、什么叫合法结束。

ReAct 是把 Thought / Action / Observation **写进同一段自然语言**，再靠字符串解析出动作。这里没有这三行协议：动作走 API 的 `tool_calls`，思考若出现也在 `reasoning_content`，归档时剥掉，不当业务事实。结束不是模型写 `Final Answer:`，而是调用 `submit_final`。同一轮可以并行多个普通工具，这是编排器能力，也不是 ReAct 逐步「想一句、动一下」。

---

## 2. 一次合法结束长什么样

InsightAgent 里，结束不是「模型说完了」，而是调用名为 `submit_final` 的工具，载荷必须通过 **Pydantic schema**。

通过之后，调度器做三件模型不准自己做的事：

1. 把业务字段写进 `state_patch`（set / append / remove），而不是让模型直接 UPDATE 数据库。
2. 盖调度器字段：`base_version`、`loop_round`、生命周期。这些数对不上就当冲突，不覆盖。
3. 标 `SUCCESS`，本 session 结束。下次分析开 **新 session**，不接着同一段聊天往下滚。

非法 JSON、缺必填、轮次用尽：失败或按策略重试。没有「先落一版脏数据再慢慢修」。

效果：交卷是合同，不是散文。测试可以用 Fake LLM 把整条 Loop 跑通，不必等真实模型。

---

## 3. 工具在模型眼里应该长得一样

运行时把能力登记进 **本 Agent 私有的 ResourceRegistry**。对模型来说，入口都是 function calling：

| 类型 | 实际是什么 | 模型需要知道的 |
|------|------------|----------------|
| FunctionTool | 读快照、算指标 | 名字、参数、返回短 JSON |
| KnowledgeBase | 检索已批准短卡片 | 同上 |
| Skill | 多步封装 | 同上 |
| AgentSkill | 另一个 Agent 的一次 `run()` | 仍然是一次工具调用 |

调用方的调度器 **不进入** 被调 Agent 的内部 Loop，也不写对方的 State。这是后面多 Agent 篇的伏笔：协作首先是「把别人注册成资源」，不是开一场群聊。

工具失败走 **指数退避**（可尊重 `Retry-After`）。429 / 超时可重试；401、422 这类合同错误直接失败。熔断是 `max_loop_round` 和并行上限，不规定「专家必须想满 15 步」。

---

## 4. 上下文会胀，但不能靠「再摘要一次」糊弄

每一轮问模型前先估 token。超预算则 **同一条消息上下文** 上逐级压，压成功立刻停：

```text
L0 工具结果预算 → L1 裁旧消息 → L2 微压缩长工具结果
→ L3 折叠更早轮次 → L4 摘要
```

这里的 L0–L4 **不是** 知识库分层，也不是五类记忆。压缩只动 `ContextBuffer`（当次发给模型的消息和工具结果）。

另有 `ContextArchive`：压缩前原文可回放，用于审计。实现上 **不保存** `reasoning_content`（思维链）。过程要可追，但不是把模型内心独白当业务事实。

---

## 5. 这一层记住三句话

1. Agent = 原生 function-calling Loop + 可校验 `submit_final` + 调度器盖章；不是 ReAct 文本协议，也不是更长的聊天。
2. 模型不直接写库；业务只经 `state_patch`。
3. 压缩、归档、重试、熔断属于运行时，不属于某个业务专家的人格。

业务专家（基本面、技术面……）都是挂在这套内核上的角色。下一篇讲：为什么 **State 必须和消息拆开**，以及记忆、知识库为什么不能塞进同一段 RAG。
