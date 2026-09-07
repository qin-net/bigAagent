# 状态、记忆与资源

> 系列 2/3。接 `Agent框架-01-从对话到单Agent.md`。  
> 下一篇：`Agent框架-03-多Agent协作与闸门.md`

单 Agent Loop 能跑之后，真正难的是：**哪些东西可以丢、哪些必须活过下一轮、哪些根本不该进 Prompt**。InsightAgent 用拆对象来解决，而不是再加一个「记忆 Agent」。

---

## 1. 四样东西，不要叫同一个「记忆」

| 对象 | 是什么 | 压缩会不会改它 |
|------|--------|----------------|
| `ContextBuffer` | 当次发给模型的 messages、工具结果 | 会，走 L0–L4 |
| `AgentState` | 生命周期、checkpoint、`private_memory` | 不会 |
| `ContextArchive` | 压缩前可回放原文 | 不进 Prompt |
| 方法论库 | 外部资源，经 Registry 检索 | 独立于压缩 |

常见翻车：把 State 序列化进 messages，上下文一满就摘要，专家上周记下的证伪条件一起被摘要掉。或者反过来：把整段聊天当 State，版本冲突时无法只回滚业务字段。

InsightAgent 的硬规则：**State 与 Context 必须拆开。** 压缩引擎没有权限改 `private_memory`。

---

## 2. 私有记忆怎么活过下一轮

每次成功分析或追踪都开新 session，不复用聊天记录。活下去的是白名单快照：

```text
上次 SUCCESS 的 AgentState.private_memory
        │  仅同一 agent_name + thesis_id
        ▼
截断后的白名单字段
        ├─ 拷进新 session 的 private_memory（本轮仍可用 state_patch 改）
        └─ 写入本轮 Task JSON 的 prior_memory
```

白名单包括：`memory_summary`、`lessons`、`active_hypotheses`、`falsifiers_watched`、`open_questions`、`pending_tasks`、以及证据/产出引用。基本面写下的字段，技术面下次 **读不到**。没有「全员共享黑板」。

模型改这些字段只能走 `state_patch`。`base_version` 对不上：不覆盖，重新加载再跑。这是乐观并发，不是群聊里最后发言的人说了算。

效果：长 Loop 可以被压短；跨天、跨次研究仍能带上「我上次在盯什么会被证伪」。

---

## 3. 用户口径是第三套记忆，而且更严

用户在分析前或反馈里写的一句话，**不是** 专家的 `private_memory`，也 **不是** 知识库条目。

编排路径：

```text
用户输入 → 剥 tag（记住 / 某维 / 重跑）
        → 模型抽六维 UserIntent（抽不出就 none）
        → this_run：只注入本轮对应专家的 Task
          remember：过闸门后写成 UserPreference
          rerun：按被点名的维再跑
```

硬约束（实现已按此做）：

1. **不落原话。** 落盘只留元数据和抽好的槽；prompt 里的原文只在 worker 内存里活到抽取结束。
2. **用户内容只进 Task。** System 提示和证据绑定看不到用户句子，避免把「我觉得要看现金流」当成报表字段。
3. **记住要过闸门。** 必须是可执行短规则（对照、核对），不是情绪或事后涨跌总结。盈亏可以拿来对照当初的 `falsifiers`，但不得自动回写旧报告、自动改偏好、自动晋升方法论。

效果：系统能「听人」，但人不能污染证据链。这和「把用户消息丢进向量库当下次 RAG」是反着的。

---

## 4. 知识库是资源，不是第二套聊天记录

知识分三层，运行时默认只碰一层：

```text
L0  文献转写的 markdown     不进分析 Prompt，不把 PDF 发给模型
L1  短卡片 + 版本           仅 status=approved 可被 search_methodology 命中
L2  手册（以后）            不进专家 System
```

检索不是「用户那句话的 embedding 近邻」，而是带着 **本轮快照里的口径 / flags / 字段门** 去命中短文，返回 0～3 条。报告里的知识库引用必须 ∈ 本轮工具返回；没检索到的 kb 引用会被丢掉（`drop_unretrieved_kb`）。禁止装饰性贴大师语录。

写库权限更窄：分析专家 **只读** 已批准卡片。追踪在蒸馏 Task 里最多 `submit_candidate`。**批准是人**，不是任何 Agent。已批准条目不能被候选覆盖。改文只追加 version，旧报告钉死当时版本。

研究日志（`insightagent.db`）和方法论库（`kb.db`）拆文件：卡片可以进 git，Run / 会话 / 用户原话残留不进 git。

---

## 5. 资源表是隔离边界

每个 `AgentInstance` 自己的 Registry。追踪把四专家注册成 `AgentSkill` 时，只是「我这轮打不打开这把工具」。蒸馏轮次可以不注册四专家，跟踪轮次可以一个都不叫，直接 `unchanged`。

同一套 DeepSeek Chat Completions 适配器、同一套重试。差别在 **本轮 Task 和打开的工具集**，不在「再做一个人格」。

---

## 6. 这一层记住三句话

1. 能压缩的是消息；必须跨 session 的是白名单 State；给人看的归档不进 Prompt。
2. 专家记忆 / 用户偏好 / 方法论卡片分权分库，混成一段 RAG 就会把口径当成事实。
3. 工具登记表决定本轮能力；没登记的，模型再想也调不到。

下一篇把「多个这样的实例」放在一起：为什么不互辩、谁在编排、追踪如何当调用方。
