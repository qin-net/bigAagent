# InsightAgent 单实例 Agent 运行时框架设计

> 状态：实现前冻结稿  
> 日期：2026-08-18  
> 关联文档：`InsightAgent-设计文档.md`  
> 范围：单 Agent 本地调度、并行资源调用、L0–L4 上下文压缩、专用 State、Agent Loop、Function Calling 与重试

---

## 1. 项目概述

构建一套支持以下能力的单实例 Agent 运行时框架：

- Agent 内部工具并行调用
- L0–L4 逐级上下文治理
- Agent 独立运行生命周期
- Agent 专用 State，与消息上下文解耦
- Function Tool、Skill、知识库和其他 Agent 的统一资源抽象
- 指数退避容错重试
- 可暂停、恢复、检查点和审计

实施路线：

1. 冻结架构规范与合同
2. 完成分层伪代码
3. 使用 fake LLM / fake resource 实现框架内核
4. 接入真实模型、AKShare、方法论库与 5 个业务 Agent

上下文压缩层级严格定义为：

```text
L0：Tool Result Budget
L1：Snip
L2：Micro-Compact
L3：Context Collapse
L4：Auto-Compact
```

这里的 L0–L4 是**同一消息上下文的逐级压缩流水线**，不是五类记忆库。AgentState、原始消息归档、方法论知识库均独立于该压缩流水线。

---

## 2. 核心边界

### 2.1 单实例自治

每个 `AgentInstance` 拥有自己的：

- `AgentLocalScheduler`
- `AgentLoop`
- `ContextCompactor`
- `AgentState`
- `ResourceRegistry`
- `CallOrchestrator`
- `RetryPolicy`
- 消息上下文、归档与检查点

运行时不存在“统一控制所有 Agent 的全局 Agent 调度器”。

当一个 Agent 需要调用另一个 Agent 时，目标 Agent 被封装成当前 Agent 的普通资源 `AgentSkill`。调用方本地调度器只知道自己调用了一个 Skill，不直接控制目标 Agent 的内部 Loop。

### 2.2 应用 Workflow 不等于全局 Agent 调度器

InsightAgent 首次股票研究仍可由应用层 workflow 并行调用四个分析 Agent：

```text
Application Workflow
→ concurrent:
     FundamentalAgent.run()
     TechnicalAgent.run()
     SentimentAgent.run()
     MacroAgent.run()
→ 综合决策环节
```

该 workflow 只负责业务流程，不进入任一 Agent 内部，也不共享 AgentState。

追踪期则由 `TrackingAgent` 将四个分析 Agent 注册为 `AgentSkill`，由其本地调度器自主选择调用。

### 2.3 State 与 Context 解耦

- `ContextBuffer`：模型消息、工具结果和摘要；受 L0–L4 压缩
- `AgentState`：生命周期、业务状态、检查点和私有追踪记忆；不被上下文压缩修改
- `ContextArchive`：消息和工具结果归档；压缩后仍可恢复，但持久化实现不保存 `reasoning_content`
- `KnowledgeBase`：外部知识资源；通过 ResourceRegistry 调用

---

## 3. 单 Agent 内部组件总览

```text
┌─────────────────────────────────────────────────┐
│ AgentInstance（独立隔离）                         │
│                                                 │
│  ├─ AgentLocalScheduler  本地调度与生命周期中控   │
│  ├─ AgentLoop            标准模型—资源执行循环    │
│  ├─ ContextCompactor     L0–L4 上下文压缩引擎     │
│  ├─ AgentState           Agent 专属运行状态        │
│  ├─ ResourceRegistry     当前 Agent 私有资源表     │
│  │    ├─ FunctionTool                           │
│  │    ├─ Skill                                  │
│  │    ├─ KnowledgeBase                          │
│  │    └─ AgentSkill                             │
│  ├─ CallOrchestrator     串行/并行资源调用编排    │
│  ├─ RetryPolicy          指数退避重试             │
│  ├─ ContextBuffer        当前会话上下文            │
│  └─ ContextArchive       原始内容归档与恢复        │
└─────────────────────────────────────────────────┘
```

---

## 4. 组件职责

### 4.1 AgentLocalScheduler

只管理当前 Agent：

- 初始化、启动、暂停、恢复和终止本 Agent
- 驱动 AgentLoop
- 每轮调用 LLM 前执行上下文 token 检测与 L0–L4 压缩
- 将 ResourceRegistry 中全部可见资源 schema 注入 LLM 请求
- 解析 LLM 的最终输出或资源调用指令
- 将资源调用转发给 CallOrchestrator
- 控制单轮并发调用上限与 Agent 总资源预算
- 协调 RetryPolicy
- 更新 loop round、状态、checkpoint 和业务 State
- 在达到最大循环轮次时执行安全兜底

`max_loop_round` 是可配置的运行保护上限，不用于规定 Agent 应该如何推理。Agent 可以在任意轮自主完成。

### 4.2 ResourceRegistry

ResourceRegistry 是当前 Agent 私有的能力目录，统一管理：

1. `FunctionTool`：搜索、读取数据、指标计算等原子函数
2. `Skill`：封装多步流程的复合能力
3. `KnowledgeBase`：向量库、文档库、方法论库检索
4. `AgentSkill`：远程或同进程 Agent 的标准化包装

对 LLM 暴露统一资源 schema。LLM 不需要感知底层资源属于函数、流程、知识库还是另一个 Agent。

### 4.3 ContextCompactor

每一轮 LLM API 调用前：

1. 计算本次请求预计 token
2. 若未超预算，直接放行
3. 若超预算，依次执行 L0、L1、L2、L3、L4
4. 每完成一层立即重新计算 token
5. 满足预算后停止，不继续执行更重压缩
6. 五层全部执行后仍超限，抛出 `ContextOverflowError`

### 4.4 AgentState

AgentState 与 ContextBuffer 分离，保存生命周期和业务状态。上下文压缩不得修改 AgentState。

### 4.5 CallOrchestrator

统一执行四类资源调用：

- Function Tool
- Skill
- KnowledgeBase
- AgentSkill

负责：

- 参数 schema 校验
- 权限检查
- 串行/并行编排
- 并发信号量
- 幂等键和结果去重
- 调用级重试
- 结果标准化
- 异常归类与传播

### 4.6 RetryPolicy

应用于：

- LLM 请求
- Function Tool
- Skill
- KnowledgeBase
- AgentSkill

区分可重试和不可重试错误。重试耗尽后，默认结束当前 AgentLoop，并将 AgentState 标记为 `FAILED`。

---

## 5. AgentState

### 5.1 基础模型

```python
class AgentState:
    session_id: str
    parent_session_id: str | None
    agent_name: str
    stock_code: str | None
    thesis_id: str | None

    loop_round: int
    status: READY | RUNNING | PAUSED | SUCCESS | FAILED

    business_context: dict
    private_memory: dict
    checkpoint: Snapshot | None

    created_at: datetime
    updated_at: datetime
    version: int
    meta: dict
```

### 5.2 私有追踪记忆

5 个 Agent 均在 `private_memory` 中保存自己的追踪记忆：

```json
{
  "memory_summary": "",
  "active_hypotheses": [],
  "key_evidence_refs": [],
  "open_questions": [],
  "falsifiers_watched": [],
  "prior_output_refs": [],
  "lessons": [],
  "pending_tasks": []
}
```

隔离键：

```text
agent_state/{agent_name}/{stock_code}/{thesis_id}/{session_id}
```

### 5.3 State 更新

- AgentLoop 每轮更新 `loop_round`、`updated_at` 和运行状态
- 业务 Agent 最终通过 strict 工具 `submit_final` 输出 set/append/remove，不写 version / loop_round
- Scheduler 递增 `loop_round`、盖上当前 `base_version` 再提交；模型抄错计数器只忽略或纠正，不失败
- State 使用版本号进行乐观并发控制（Store CAS）
- 真正的版本冲突不覆盖，读取最新版本后恢复或重新执行
- ContextCompactor 永远不能删除、摘要或覆盖 State

### 5.4 Checkpoint

Checkpoint 至少包含：

- AgentState 快照
- ContextBuffer 指针
- ContextArchive 游标
- 当前资源调用状态
- 当前 loop round
- 待完成调用和幂等键

用途：

- 暂停后恢复
- AgentSkill 长任务恢复
- 失败调查
- 显式回滚到安全检查点

历史事件日志保持追加写；回滚生成新版本，不删除历史。

---

## 6. ResourceRegistry 统一资源模型

### 6.1 资源合同

```python
class ResourceType(Enum):
    FUNCTION = "function"
    SKILL = "skill"
    KNOWLEDGE_BASE = "knowledge_base"
    AGENT_SKILL = "agent_skill"

class ResourceSpec:
    name: str
    type: ResourceType
    description: str
    input_schema: dict
    output_schema: dict
    timeout_seconds: float
    retry_policy: str
    parallel_safe: bool
    side_effect: NONE | IDEMPOTENT | NON_IDEMPOTENT
    permission_tags: set[str]
    version: str
```

### 6.2 注册表能力

```python
class ResourceRegistry:
    def register(resource): ...
    def unregister(name): ...
    def get(name): ...
    def list_all(): ...
    def get_all_resource_definitions(): ...
```

每轮 LLM 请求注入当前 Agent 可见的全部资源定义。若资源很多，ResourceRegistry 自身的 schema 描述可以使用稳定短描述和版本缓存，但不能向 LLM 暴露未授权资源。

### 6.3 标准调用指令

```json
{
  "calls": [
    {
      "call_id": "call_001",
      "resource": "get_financial_report",
      "arguments": {"stock_code": "000858"},
      "execution": "parallel",
      "depends_on": []
    }
  ]
}
```

### 6.4 标准调用结果

```json
{
  "call_id": "call_001",
  "resource": "get_financial_report",
  "status": "success | failed",
  "data": {},
  "data_ref": "artifact://...",
  "error": null,
  "started_at": "...",
  "finished_at": "...",
  "attempts": 1
}
```

超大结果的 `data` 可以为空，仅通过 `data_ref` 指向 L0 外部存储。

---

## 7. L0–L4 五级上下文压缩

### 7.1 Token 预算

每轮调用前计算：

```text
request_budget =
    model_context_window
    - reserved_output_tokens
    - safety_margin

request_tokens =
    system_prompt
    + resource_definitions
    + projected_context
```

当 `request_tokens > request_budget × token_threshold` 时开始压缩。

默认 `token_threshold` 可设置为 0.85，但属于模型配置，不写死在框架逻辑中。

### 7.2 L0｜Tool Result Budget

目标：优先处理最容易失控的超大工具结果。

策略：

- 对每个工具结果设置 token budget
- 超预算结果完整写入 ArtifactStore / 磁盘 / 缓存
- ContextBuffer 只保留：
  - 唯一 `data_ref`
  - 来源
  - 时间戳
  - 内容哈希
  - 可供模型判断是否需要展开的短摘要
- 后续可通过资源调用按片段读取原始内容

L0 不丢失原始内容，只改变上下文中的表示。

### 7.3 L1｜Snip

目标：裁剪最久远、最低优先级的历史消息。

优先保留：

- 当前用户任务
- 近期对话
- 关键决策
- 未解决问题
- 重要工具调用
- 当前引用中的证据

优先裁剪：

- 久远闲聊
- 已关闭且无后续引用的中间过程
- 重复资源定义回显
- 可从归档恢复的低价值消息

Snip 只影响本次请求投影，不删除 ContextArchive。

### 7.4 L2｜Micro-Compact

目标：轻量精简局部内容，不做完整会话摘要。

策略：

- 基于时间衰减和消息重要度选择局部片段
- 去掉工具结果中的冗余描述、重复字段和展示格式
- 保留核心事实、结论、异常、证据引用和未决问题
- 优先处理已关闭的工具交互
- 不改写数字、日期、评级和证伪条件

L2 产出局部 compact block，原消息仍在归档。

### 7.5 L3｜Context Collapse

目标：在发送 LLM 请求时动态投影简化历史上下文。

特点：

- 延迟式压缩
- 不永久改写 ContextBuffer 原始事件
- 根据当前任务只投影相关历史
- 将多个历史回合折叠成结构化上下文块
- 同一原始上下文可以针对不同任务生成不同投影

示例投影结构：

```json
{
  "task_relevant_history": [],
  "key_decisions": [],
  "active_questions": [],
  "evidence_refs": [],
  "superseded_items": []
}
```

### 7.6 L4｜Auto-Compact

目标：五层中的最终兜底。

策略：

- 使用 LLM 对久远多轮会话做全量摘要
- 保留任务目标、重要决策、关键事实、证据引用、失败记录和未决问题
- 原始历史完整归档
- 新上下文使用“历史摘要 + 近期原始消息”
- 摘要带版本、来源范围和校验信息
- 后续可以按归档引用恢复原始内容

L4 摘要模型输出必须通过 schema 校验：

```python
class AutoCompactSummary:
    covered_message_ids: list[str]
    goals: list[str]
    key_facts: list[FactWithRef]
    decisions: list[DecisionWithRef]
    unresolved_questions: list[str]
    failures: list[str]
    restore_refs: list[str]
    summary_text: str
```

### 7.7 逐级执行

```python
async def compact_before_llm(context):
    projected = context.project_raw()

    if fits(projected):
        return projected

    for layer in [L0, L1, L2, L3, L4]:
        projected = await layer.apply(
            original_context=context,
            current_projection=projected,
        )
        record_compaction(layer, projected)

        if fits(projected):
            return projected

    raise ContextOverflowError()
```

### 7.8 压缩安全

- 每层产物保留来源消息 ID
- 数值事实必须携带 ArtifactRef 或 EvidenceRef
- 原始消息和工具结果不可因压缩被物理删除
- 压缩摘要不能作为自身证据
- L4 后仍超限时失败，不继续无边界截断
- State 不参与压缩

---

## 8. AgentLoop

### 8.1 生命周期

```text
READY
→ RUNNING
→ SUCCESS
   PAUSED
   FAILED
```

恢复：

```text
PAUSED → load checkpoint → RUNNING
FAILED → explicit retry/recover → new run or checkpoint restore
```

### 8.2 标准循环

```text
初始化
→ 加载 AgentState
→ 加载 ContextBuffer / ContextArchive 游标
→ 读取 ResourceRegistry
→ status = RUNNING

while RUNNING and loop_round < max_loop_round:
    1. token 检测，依次执行 L0–L4
    2. 组装 system prompt + compacted context + 全部资源 schema
    3. 带 RetryPolicy 请求 LLM
    4. 解析：
       A. Final Answer
          → 校验输出
          → 应用 state_patch
          → SUCCESS
       B. Resource Calls
          → CallOrchestrator 串行/并行执行
          → 调用失败按 RetryPolicy 处理
          → 标准结果追加 ContextBuffer
    5. loop_round += 1
    6. 更新 State / checkpoint

达到 max_loop_round：
→ FAILED
→ 保存 checkpoint 和错误原因
```

### 8.3 终止条件

正常终止：

- LLM 输出符合 schema 的最终答案
- Agent 明确 `abstain`，且说明信息不足

异常终止：

- 达到最大循环轮次
- 重试耗尽
- L0–L4 后仍上下文溢出
- 不可重试权限/参数错误
- deadline/cancellation
- State 提交冲突且无法恢复

---

## 9. CallOrchestrator

### 9.1 并行规则

LLM 可以在同一轮返回多个调用。

可以并行：

- `execution == parallel`
- `depends_on` 为空
- ResourceSpec.parallel_safe 为 true
- 并发信号量有容量

必须串行：

- 有显式依赖
- 非幂等副作用资源
- 目标资源声明 `parallel_safe=false`

### 9.2 并发保护

- Agent 级 `max_parallel_calls`
- 资源类型级并发上限
- Provider 级限流器
- 父 Agent取消时取消未完成子调用
- 并行结果按 `call_id` 归槽，不以完成顺序改变语义

### 9.3 失败语义

默认严格模式：

- 任一必需调用重试耗尽，当前 AgentLoop 失败

资源可显式声明可选模式：

- 失败结果作为结构化 observation 返回
- Agent 可以基于缺失信息选择 `abstain`

不能静默吞掉失败。

---

## 10. 指数退避重试

### 10.1 公式

```python
delay = (
    base_delay
    * (backoff_factor ** retry_count)
    + random_jitter
)
```

若服务返回 `Retry-After`，等待时间不得短于该值。

### 10.2 错误分类

可重试：

- 网络瞬断
- 连接超时
- API 限流
- 暂时性 5xx
- 可恢复的 Provider unavailable

不可重试：

- 参数非法
- schema 不匹配且无法修复
- 权限/鉴权错误
- 资源不存在
- 违反 Agent 权限
- 明确业务拒绝

### 10.3 配置

```python
class RetryConfig:
    max_retries: int
    base_delay: float
    backoff_factor: float
    jitter_min: float
    jitter_max: float
    max_delay: float
    respect_retry_after: bool
```

不同链路使用不同配置：

```text
llm_retry
function_retry
skill_retry
knowledge_retry
agent_skill_retry
```

### 10.4 可观测

记录：

- resource / provider
- error category
- retry_count
- delay
- request id
- idempotency key
- 最终成功或耗尽

---

## 11. AgentSkill

### 11.1 定义

AgentSkill 将其他 Agent 包装为普通 Resource：

```python
class AgentSkill(Resource):
    target_agent: AgentInstance
    input_schema: dict
    output_schema: dict
    mode: "same_process | remote"
```

调用时：

1. 创建子 Agent 会话或恢复目标 Agent 对应股票/thesis 的会话
2. 设置 `parent_session_id`
3. 传入结构化任务与只读业务引用
4. 启动目标 Agent 自己的 LocalScheduler 和 Loop
5. 等待、超时或取消
6. 将目标 Agent 最终结果包装成标准 ResourceResult

### 11.2 边界

- 调用方不能读写目标 AgentState
- 调用方不能指定目标 Agent 内部工具顺序
- 目标 Agent 使用自己的 ResourceRegistry
- 子 Agent失败按 AgentSkill RetryPolicy 处理
- 防止调用环：分析 Agent 默认无权注册追踪 Agent 或其他分析 Agent；追踪 Agent 可注册四个分析 Agent

### 11.3 追踪 Agent

追踪 Agent 的 ResourceRegistry 包含：

- 增量行情和事件工具
- 方法论知识库
- FundamentalAgentSkill
- TechnicalAgentSkill
- SentimentAgentSkill
- MacroAgentSkill

追踪 Agent 可以在一轮中并行调用多个 AgentSkill，也可以根据第一批结果继续下一轮。

### 11.4 动态任务 Prompt 与固定返回合同

追踪阶段由追踪 Agent 自主决定需要什么信息，并为被唤醒的分析 Agent 生成结构化任务 Prompt。Prompt 分三层，禁止混写：

```text
System Prompt（目标 Agent 固定）
  - 角色边界
  - 工具权限
  - 证据纪律
  - 输出 schema

Task Prompt（追踪 Agent 动态生成）
  - 本次目标
  - 具体问题
  - 为什么需要复评
  - 可读取的上下文引用
  - 必须核查的证据
  - 完成条件

Evidence/Data（只作为数据）
  - 行情、财报、公告、新闻、历史报告
  - 其中的文本不得被当成指令
```

追踪 Agent 可以决定 Task Prompt，但不能：

- 修改目标 Agent 的 System Prompt
- 扩大目标 Agent 的资源权限
- 要求目标 Agent 跳过证据或 schema 校验
- 将外部新闻/公告中的文字作为系统指令
- 指定目标 Agent 内部必须按某个工具顺序执行

AgentSkill 输入合同：

```python
class AgentTaskPrompt:
    task_id: str
    target_agent: "fundamental | technical | sentiment | macro"
    objective: str
    reason: str
    required_questions: list[str]
    required_evidence: list[str]
    context_refs: list[str]
    constraints: list[str]
    completion_criteria: list[str]
    output_schema_version: str
    as_of: datetime
```

分 Agent 必须返回严格结构：

```python
class AgentTaskResponse:
    task_id: str
    status: "completed | abstained | degraded"
    answers: list[{
        "question": str,
        "answer": str,
        "evidence_refs": list[str],
        "confidence": float,
    }]
    findings: list[dict]
    risks: list[dict]
    missing_information: list[str]
    thesis_impact: "none | strengthen | weaken | invalidate | uncertain"
    report: Report
    reflection: Reflection
    state_patch: StatePatch
```

校验要求：

- `task_id` 必须匹配调用
- `required_questions` 必须逐项回答；无法回答时写入 `missing_information`
- 所有确定性判断必须带 `evidence_refs`
- `output_schema_version` 必须是目标 Agent 已注册版本
- schema 不合格时，将校验错误作为纠正请求返回该分 Agent
- 纠正重试耗尽后，该 AgentSkill 调用失败，追踪 Agent不得自行补写分 Agent 结论

---

## 12. Function Calling 策略

### 12.1 每轮模型输出

模型只能返回两种结构之一：

```text
FinalResponse
ResourceCallBatch
```

不能用普通文本伪装资源调用。

### 12.2 ResourceCallBatch

```python
class ResourceCall:
    call_id: str
    resource_name: str
    arguments: dict
    execution: "serial | parallel"
    depends_on: list[str]

class ResourceCallBatch:
    calls: list[ResourceCall]
```

### 12.3 执行前校验

- 资源已注册
- 当前 Agent 有权限
- 参数符合 input schema
- 调用依赖无环
- 并行调用符合 parallel_safe
- 幂等键已生成
- 未超过本轮并发和资源预算

### 12.4 结果进入上下文

- 小结果直接追加
- 大结果经过 L0 外置，仅追加摘要和 data_ref
- 工具结果带时间戳、来源、版本和哈希
- Agent 下一轮可通过 data_ref 分片加载

---

## 13. InsightAgent 业务映射

### 13.1 五个实例

```text
FundamentalAgent
TechnicalAgent
SentimentAgent
MacroAgent
TrackingAgent
```

每个实例有独立：

- State
- ContextBuffer
- ContextArchive
- ResourceRegistry
- LocalScheduler
- RetryPolicy 配置

### 13.2 首次查询

应用 workflow 对交互物使用固定合同，并行调用四个分析 Agent，收集结果后进入综合决策环节。综合决策是系统环节，不是第六个 Agent。

```python
class InitialAnalysisRequest:
    run_id: str
    stock_code: str
    snapshot_ref: str
    thesis_id: str
    as_of: datetime
    output_schema_version: str
```

四个请求分别绑定固定的数据切片、固定职责和固定输出 schema。首次 workflow 不允许运行时临时改变专家职责。

### 13.3 跟踪

Scheduler/Cron 只负责按小时、天、周唤醒 TrackingAgent：

```text
TrackingAgent.run(delta_task)
→ 自主读取状态与上下文
→ 自主调用工具/知识库
→ 自主形成 AgentTaskPrompt
→ 自主调用 0～4 个 AgentSkill
→ 严格校验 AgentTaskResponse
→ 自主反思
→ 输出固定 TrackingDeliverable
→ 更新自身 AgentState
```

四个分析 Agent被唤醒后，也会读取自己的追踪上下文和私有 State，并在完成后更新自己的 State。

---

## 14. 固定业务交付物

### 14.1 分析 Agent

```python
class AnalysisDeliverable:
    task_id: str
    status: "completed | abstained | degraded"
    report: Report
    answers: list[QuestionAnswer]
    evidence_refs: list[str]
    missing_information: list[str]
    thesis_impact: str
    facts: list[ExtractedFact]
    clues: list[Clue]
    judgment_basis: JudgmentBasis
    trace_steps: list[TraceStep]
    reflection: Reflection
    state_patch: StatePatch
```

### 14.2 追踪 Agent

```python
class TrackingDeliverable:
    status: "unchanged | review | invalidate"
    work_summary: str
    evidence_refs: list[str]
    triggers_hit: list[dict]
    agent_skill_calls: list[AgentSkillCallRecord]
    decision_required: bool
    user_output: UserTrackingOutput
    reflection: Reflection
    methodology_proposals: list[dict]
    state_patch: StatePatch
    next_check_suggestion: dict
```

固定的是最终交付结构，不限制内部循环和资源调用路径。过程字段定义见主设计文档 §6.3；编排将 facts/clues/judgment_basis/trace_steps/reflection 打成 `ProcessRecord` 落盘。

`reflection` 仅保存结构化结论：

- 哪些步骤有效
- 缺少什么信息
- 出现了什么过程错误
- 哪些方法论值得形成候选

不要求或保存模型隐藏思维链。

---

## 15. 顶层伪代码

### 15.1 状态

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import uuid

class TaskStatus(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"

@dataclass
class AgentState:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_session_id: str | None = None
    agent_name: str = ""
    stock_code: str | None = None
    thesis_id: str | None = None

    loop_round: int = 0
    status: TaskStatus = TaskStatus.READY

    business_context: dict[str, Any] = field(default_factory=dict)
    private_memory: dict[str, Any] = field(default_factory=dict)
    checkpoint: dict[str, Any] | None = None

    version: int = 0
    meta: dict[str, Any] = field(default_factory=dict)
```

### 15.2 资源注册表

```python
class ResourceRegistry:
    def __init__(self):
        self._resources: dict[str, Resource] = {}

    def register(self, resource: Resource) -> None:
        if resource.name in self._resources:
            raise DuplicateResourceError(resource.name)
        self._resources[resource.name] = resource

    def get(self, name: str) -> Resource:
        return self._resources[name]

    def get_all_resource_definitions(self) -> list[dict]:
        return [
            resource.to_llm_schema()
            for resource in self._resources.values()
        ]
```

### 15.3 L0–L4 压缩

```python
class ContextCompactor:
    def __init__(self, token_counter, artifact_store, archive, config):
        self.token_counter = token_counter
        self.artifact_store = artifact_store
        self.archive = archive
        self.config = config
        self.layers = [
            ToolResultBudgetLayer(),  # L0
            SnipLayer(),              # L1
            MicroCompactLayer(),      # L2
            ContextCollapseLayer(),   # L3
            AutoCompactLayer(),       # L4
        ]

    async def compact_before_llm(
        self,
        context_buffer,
        system_prompt,
        resource_definitions,
        model_config,
    ):
        projection = context_buffer.raw_projection()

        if self._fits(
            system_prompt,
            resource_definitions,
            projection,
            model_config,
        ):
            return projection

        for layer in self.layers:
            projection = await layer.apply(
                original=context_buffer,
                projection=projection,
                artifact_store=self.artifact_store,
                archive=self.archive,
            )

            if self._fits(
                system_prompt,
                resource_definitions,
                projection,
                model_config,
            ):
                return projection

        raise ContextOverflowError(
            "Context still exceeds budget after L0-L4"
        )
```

### 15.4 指数退避

```python
import asyncio
import random

class ExponentialBackoff:
    def __init__(self, config, classifier):
        self.config = config
        self.classifier = classifier

    async def execute(self, operation, *args, **kwargs):
        retry_count = 0

        while True:
            try:
                return await operation(*args, **kwargs)
            except Exception as error:
                error_type = self.classifier(error)

                if not error_type.retryable:
                    raise

                if retry_count >= self.config.max_retries:
                    raise RetryExhaustedError() from error

                jitter = random.uniform(
                    self.config.jitter_min,
                    self.config.jitter_max,
                )
                delay = (
                    self.config.base_delay
                    * (self.config.backoff_factor ** retry_count)
                    + jitter
                )
                delay = min(delay, self.config.max_delay)

                if error_type.retry_after is not None:
                    delay = max(delay, error_type.retry_after)

                record_retry(error, retry_count, delay)
                await asyncio.sleep(delay)
                retry_count += 1
```

### 15.5 调用编排器

```python
class CallOrchestrator:
    def __init__(self, registry, retry_policies, max_parallel_calls):
        self.registry = registry
        self.retry_policies = retry_policies
        self.semaphore = asyncio.Semaphore(max_parallel_calls)

    async def dispatch_calls(self, calls: list[ResourceCall]):
        validate_dependency_graph(calls)

        results = {}
        pending = {call.call_id: call for call in calls}

        while pending:
            ready = [
                call for call in pending.values()
                if all(dep in results for dep in call.depends_on)
            ]
            if not ready:
                raise CallDependencyCycleError()

            parallel_calls = [
                call for call in ready
                if call.execution == "parallel"
                and self.registry.get(call.resource_name).parallel_safe
            ]
            serial_calls = [call for call in ready if call not in parallel_calls]

            parallel_results = await asyncio.gather(
                *(self._execute(call) for call in parallel_calls)
            )
            for call, result in zip(parallel_calls, parallel_results):
                results[call.call_id] = result
                pending.pop(call.call_id)

            for call in serial_calls:
                results[call.call_id] = await self._execute(call)
                pending.pop(call.call_id)

        return results

    async def _execute(self, call):
        resource = self.registry.get(call.resource_name)
        arguments = validate(resource.input_schema, call.arguments)
        retry = self.retry_policies.for_resource(resource)

        async with self.semaphore:
            return await retry.execute(
                resource.invoke,
                arguments,
                idempotency_key=make_idempotency_key(call),
            )
```

### 15.6 本地调度器与 AgentLoop

```python
class AgentLocalScheduler:
    def __init__(
        self,
        state_store,
        resource_registry,
        compactor,
        retry_policies,
        orchestrator,
        llm_client,
        config,
    ):
        self.state_store = state_store
        self.resources = resource_registry
        self.compactor = compactor
        self.retry_policies = retry_policies
        self.orchestrator = orchestrator
        self.llm = llm_client
        self.config = config

    async def run_agent_loop(
        self,
        state: AgentState,
        user_input: str,
        context_buffer,
    ):
        state.status = TaskStatus.RUNNING
        context_buffer.append_user(user_input)
        await self.state_store.save(state)

        try:
            while (
                state.status == TaskStatus.RUNNING
                and state.loop_round < self.config.max_loop_round
            ):
                resource_definitions = (
                    self.resources.get_all_resource_definitions()
                )

                compacted_context = await self.compactor.compact_before_llm(
                    context_buffer=context_buffer,
                    system_prompt=build_system_prompt(state),
                    resource_definitions=resource_definitions,
                    model_config=self.config.model,
                )

                llm_response = await self.retry_policies.llm.execute(
                    self.llm.respond,
                    system_prompt=build_system_prompt(state),
                    context=compacted_context,
                    resources=resource_definitions,
                    output_schema=output_schema_for(state.agent_name),
                )

                action = parse_llm_action(llm_response)

                if action.is_final_answer:
                    final = validate_final_output(action.content)
                    state = apply_state_patch(state, final.state_patch)
                    state.status = TaskStatus.SUCCESS
                    state.checkpoint = make_checkpoint(
                        state,
                        context_buffer,
                    )
                    await self.state_store.save(state)
                    return final

                calls = validate_resource_calls(
                    action.calls,
                    registry=self.resources,
                )
                call_results = await self.orchestrator.dispatch_calls(calls)

                for result in call_results.values():
                    context_buffer.append_resource_result(result)

                state.loop_round += 1
                state.checkpoint = make_checkpoint(
                    state,
                    context_buffer,
                )
                await self.state_store.save(state)

            state.status = TaskStatus.FAILED
            state.business_context["failure_reason"] = (
                "max_loop_round_reached"
            )
            await self.state_store.save(state)
            raise MaxLoopRoundExceeded()

        except PauseRequested:
            state.status = TaskStatus.PAUSED
            state.checkpoint = make_checkpoint(state, context_buffer)
            await self.state_store.save(state)
            raise

        except Exception as error:
            state.status = TaskStatus.FAILED
            state.business_context["failure_reason"] = serialize_error(error)
            state.checkpoint = make_checkpoint(state, context_buffer)
            await self.state_store.save(state)
            raise
```

### 15.7 AgentInstance

```python
class AgentInstance:
    def __init__(self, name, config, dependencies):
        self.name = name
        self.state_store = dependencies.state_store.for_agent(name)
        self.context_archive = dependencies.archive.for_agent(name)
        self.context_buffer = ContextBuffer(self.context_archive)

        self.resource_registry = ResourceRegistry()
        self.compactor = ContextCompactor(
            token_counter=dependencies.token_counter,
            artifact_store=dependencies.artifact_store,
            archive=self.context_archive,
            config=config.context,
        )
        self.retry_policies = build_retry_policies(config.retry)
        self.orchestrator = CallOrchestrator(
            registry=self.resource_registry,
            retry_policies=self.retry_policies,
            max_parallel_calls=config.max_parallel_calls,
        )
        self.scheduler = AgentLocalScheduler(
            state_store=self.state_store,
            resource_registry=self.resource_registry,
            compactor=self.compactor,
            retry_policies=self.retry_policies,
            orchestrator=self.orchestrator,
            llm_client=dependencies.llm_client,
            config=config,
        )

    def register_tool(self, tool):
        self.resource_registry.register(tool)

    def register_skill(self, skill):
        self.resource_registry.register(skill)

    def register_knowledge_base(self, knowledge_base):
        self.resource_registry.register(knowledge_base)

    def register_agent_as_skill(self, other_agent, schema):
        self.resource_registry.register(
            AgentSkill(
                target_agent=other_agent,
                input_schema=schema.input,
                output_schema=schema.output,
            )
        )

    async def run(
        self,
        user_query,
        *,
        session_id=None,
        parent_session_id=None,
        business_context=None,
    ):
        state = await self.state_store.load_or_create(
            session_id=session_id,
            parent_session_id=parent_session_id,
            agent_name=self.name,
            business_context=business_context or {},
        )
        context = await self.context_buffer.load(state.session_id)
        return await self.scheduler.run_agent_loop(
            state=state,
            user_input=user_query,
            context_buffer=context,
        )
```

### 15.8 AgentSkill

```python
class AgentSkill(Resource):
    async def invoke(self, arguments, idempotency_key):
        task_prompt = AgentTaskPrompt.model_validate(arguments)

        child_result = await self.target_agent.run(
            user_query=render_task_prompt(task_prompt),
            session_id=arguments.get("target_session_id"),
            parent_session_id=arguments["parent_session_id"],
            business_context={
                "task_prompt": task_prompt.model_dump(),
                "context_refs": task_prompt.context_refs,
            },
        )

        response = AgentTaskResponse.model_validate(child_result)
        validate_task_response(
            response,
            expected_task_id=task_prompt.task_id,
            required_questions=task_prompt.required_questions,
            required_evidence=task_prompt.required_evidence,
        )
        return normalize_agent_skill_result(response)
```

### 15.9 首次研究应用 Workflow

```python
async def run_initial_research(stock_code):
    business_context = {"stock_code": stock_code}

    fundamental, technical, sentiment, macro = await asyncio.gather(
        fundamental_agent.run(
            "完成基本面分析",
            business_context=business_context,
        ),
        technical_agent.run(
            "完成技术面分析",
            business_context=business_context,
        ),
        sentiment_agent.run(
            "完成情绪与事件分析",
            business_context=business_context,
        ),
        macro_agent.run(
            "完成宏观相关性分析",
            business_context=business_context,
        ),
    )

    return await decision_phase(
        fundamental,
        technical,
        sentiment,
        macro,
    )
```

---

## 16. 建议目录

```text
insightagent/
  runtime/
    agent_instance.py
    local_scheduler.py
    agent_loop.py
    call_orchestrator.py
    retry.py
    lifecycle.py
  context/
    buffer.py
    archive.py
    compactor.py
    token_budget.py
    layers/
      l0_tool_result_budget.py
      l1_snip.py
      l2_micro_compact.py
      l3_context_collapse.py
      l4_auto_compact.py
  state/
    models.py
    store.py
    checkpoint.py
  resources/
    base.py
    registry.py
    function_tool.py
    skill.py
    knowledge_base.py
    agent_skill.py
  contracts/
    actions.py
    results.py
    tracking.py
    state_patch.py
  agents/
    fundamental.py
    technical.py
    sentiment.py
    macro.py
    tracking.py
  workflows/
    initial_research.py
    decision.py
    tracking_trigger.py
  observability/
    tracing.py
    metrics.py
    replay.py
```

---

## 17. 实现顺序

1. AgentState、ResourceSpec、ResourceResult、LLM Action 合同
2. 内存版 StateStore、ContextBuffer、ContextArchive
3. ResourceRegistry 与 FunctionTool
4. RetryPolicy
5. CallOrchestrator 串行/并行执行
6. L0 Tool Result Budget
7. L1 Snip
8. L2 Micro-Compact
9. L3 Context Collapse
10. L4 Auto-Compact
11. AgentLocalScheduler + AgentLoop
12. Skill、KnowledgeBase、AgentSkill
13. Checkpoint、暂停恢复和重放
14. 5 个业务 Agent
15. 首次研究 workflow 与追踪唤醒

测试顺序：

- fake resource 的参数、并行、依赖和失败测试
- fake LLM 的 Tool Call / Final Answer Loop 测试
- 每层上下文压缩的 token 与可恢复性测试
- 重试、耗尽、暂停、恢复和 max loop 测试
- AgentSkill 父子会话、取消和调用环测试
- 最后接真实模型与真实数据
