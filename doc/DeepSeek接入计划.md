# DeepSeek API 接入计划

> 状态：实现前计划  
> 官方文档：https://api-docs.deepseek.com/zh-cn/  
> 关联文档：`Agent运行时框架设计.md`

## 1. 接入结论

第一期使用 DeepSeek 官方 OpenAI 兼容接口：

```text
base_url: https://api.deepseek.com
endpoint:  POST /chat/completions
models:    deepseek-v4-flash / deepseek-v4-pro
```

框架不得直接依赖 OpenAI SDK 返回对象。新增内部 `LLMAdapter`，DeepSeek 只是首个实现：

```python
class LLMAdapter:
    async def complete(request: LLMRequest) -> LLMResponse: ...
    async def stream(request: LLMRequest) -> AsyncIterator[LLMEvent]: ...
```

## 2. 官方 API 对框架的关键影响

### 2.1 思考模式

- `thinking.type`：`enabled | disabled`，默认 enabled
- `reasoning_effort`：`low | high | max`
- OpenAI SDK 下 `thinking` 通过 `extra_body` 传入
- 思考模式不支持有效调节 `temperature` / `top_p`
- 思考模式发生 Tool Call 后，后续请求必须完整回传对应 assistant 消息的 `reasoning_content`，否则可能返回 400

框架处理：

- `reasoning_content` 只保存在当前进程的 ContextBuffer，用于 API 协议回放；持久化 ContextArchive 写入时剥离
- 不写入 AgentState、Reflection 或用户输出
- L0–L4 压缩不能破坏仍处于工具调用链中的 `reasoning_content`
- 进程在 Tool Call 中间退出时，从最后安全 checkpoint 重跑该轮，不从数据库恢复隐藏 reasoning
- 新进程恢复历史会话时，将已完成的旧 Tool Call/Result 链折叠为历史数据摘要，不把缺少 reasoning 的 assistant Tool Call 原样发回 API
- 工具调用回合关闭后，才允许按压缩规则归档

### 2.2 Tool Calls

- API 当前仅支持 `function` 类型工具
- 单次最多传入 128 个 function
- 模型可能一次返回一个或多个 `tool_calls`
- `arguments` 是 JSON 字符串，仍可能非法或包含 schema 外字段
- 每个 Tool Result 必须使用对应 `tool_call_id` 回传

框架处理：

- ResourceRegistry 的 Function / Skill / KnowledgeBase / AgentSkill 全部映射为 function schema
- CallOrchestrator 支持同轮多个 Tool Call 并行
- 执行前必须 JSON 解析、Pydantic 校验、权限校验
- 不相信模型生成的参数，即使启用 strict
- assistant 的 Tool Call 消息必须原样进入 ContextBuffer

### 2.3 Strict Tool Calls

官方 strict 模式当前属于 Beta：

```text
base_url: https://api.deepseek.com/beta
function.strict: true
```

限制：

- 所有 function 都必须设置 `strict=true`
- object 的全部 properties 必须 required
- `additionalProperties=false`
- 仅使用官方支持的 JSON Schema 子集

第一期策略：

- 默认使用 Beta base URL + `function.strict=true` + 本地 Pydantic 二次校验
- 所有工具 schema 经 `to_strict_json_schema` 改写成官方子集（properties 全 required、`additionalProperties=false`、`$defs` 改为 `$def`）
- 最终交卷走 `submit_final` 工具，用同一套 strict schema 约束；DeepSeek 不支持 `response_format.json_schema`
- Strict 仍不是正确性的唯一保障：参数和最终 payload 都要再校验

### 2.4 JSON Output

使用：

```json
{"response_format": {"type": "json_object"}}
```

注意：

- Prompt 中必须明确包含“JSON”并给出格式要求
- JSON Output 只保证合法 JSON，不保证符合业务 schema
- content 可能为空
- `finish_reason=length` 时 JSON 可能被截断

框架处理：

- 最终交付物优先通过 strict 工具 `submit_final` 提交（JSON Schema 在 function.parameters 上强制）
- `json_object` 仅作为非 strict 回退；DeepSeek 不提供 `json_schema` response_format
- 空 content 或 schema 失败视为可纠正输出错误，进入下一轮，不因模型抄错 version/loop_round 失败
- `length` 不直接解析为成功，先执行上下文/输出预算处理
- `loop_round` 与 `base_version` 由 LocalScheduler 递增/盖章

### 2.5 finish_reason

必须处理：

```text
stop
length
content_filter
tool_calls
insufficient_system_resource
```

映射：

- `stop`：解析 FinalResponse
- `tool_calls`：进入 CallOrchestrator
- `length`：输出截断；重新评估 max_tokens 和上下文压缩
- `content_filter`：不可伪装成功，返回受限错误
- `insufficient_system_resource`：暂时性错误，进入指数退避

### 2.6 上下文缓存

DeepSeek 默认启用上下文硬盘缓存，前缀完整匹配才能命中。

usage 字段：

```text
prompt_cache_hit_tokens
prompt_cache_miss_tokens
```

请求组织：

1. 固定 System Prompt
2. 稳定 Resource schema（按名称排序）
3. 相对稳定的历史前缀
4. 当前动态任务和增量数据

L0–L4 压缩应尽量只改后部动态上下文，减少无意义的前缀变化。记录缓存命中率，但不能为了缓存牺牲上下文正确性。

### 2.7 user_id

- 用于安全、KVCache 和调度隔离
- 不包含姓名、手机号等隐私
- 使用内部不可逆用户标识或租户标识

### 2.8 限流与错误码

错误映射：

```text
400 请求格式错误       不原样重试
401 认证失败           不重试
402 余额不足           不重试
422 参数错误           不原样重试
429 限流               指数退避
500 服务端错误         指数退避
503 服务繁忙           指数退避
```

`insufficient_system_resource` 即使 HTTP 为 200，也按可重试推理中断处理。

HTTP 客户端要兼容：

- 非流式等待期间的空行保活
- 流式 SSE `: keep-alive`
- 长请求 deadline 与主动取消

## 3. 内部合同

```python
class LLMRequest:
    model: str
    messages: list[LLMMessage]
    tools: list[LLMTool]
    tool_choice: str | dict | None
    thinking_enabled: bool
    reasoning_effort: str
    response_format: str
    max_tokens: int
    stream: bool
    user_id: str | None

class LLMMessage:
    role: "system | user | assistant | tool"
    content: str | None
    reasoning_content: str | None
    tool_calls: list[ToolCall] | None
    tool_call_id: str | None

class LLMResponse:
    id: str
    model: str
    content: str | None
    reasoning_content: str | None
    tool_calls: list[ToolCall]
    finish_reason: str
    usage: LLMUsage
    system_fingerprint: str | None
```

## 4. DeepSeek Adapter 伪代码

```python
class DeepSeekChatAdapter(LLMAdapter):
    def __init__(self, client, config):
        self.client = client
        self.config = config

    async def complete(self, request: LLMRequest) -> LLMResponse:
        payload = {
            "model": request.model,
            "messages": [
                self._serialize_message(message)
                for message in request.messages
            ],
            "tools": [
                self._serialize_tool(tool)
                for tool in request.tools
            ] or None,
            "max_tokens": request.max_tokens,
            "stream": False,
            "user_id": request.user_id,
        }

        if request.thinking_enabled:
            payload["extra_body"] = {
                "thinking": {"type": "enabled"},
                "reasoning_effort": request.reasoning_effort,
            }
            # thinking + tools 时不发送不兼容的采样参数
        else:
            payload["extra_body"] = {
                "thinking": {"type": "disabled"}
            }

        if request.response_format == "json":
            payload["response_format"] = {"type": "json_object"}

        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice

        raw = await self.client.chat.completions.create(
            **drop_none(payload)
        )
        return self._normalize(raw)

    def _serialize_message(self, message):
        data = {
            "role": message.role,
            "content": message.content,
        }

        if message.reasoning_content is not None:
            data["reasoning_content"] = message.reasoning_content
        if message.tool_calls:
            data["tool_calls"] = [
                serialize_tool_call(call)
                for call in message.tool_calls
            ]
        if message.tool_call_id is not None:
            data["tool_call_id"] = message.tool_call_id

        return drop_none(data)
```

## 5. DeepSeek Agent Loop 伪代码

```python
async def deepseek_agent_round(agent, context):
    request = build_llm_request(
        context=context,
        tools=agent.registry.get_all_resource_definitions(),
        thinking_enabled=agent.config.thinking_enabled,
        response_format="json",
    )

    response = await agent.retry.llm.execute(
        agent.llm_adapter.complete,
        request,
    )

    context.append_assistant(
        content=response.content,
        reasoning_content=response.reasoning_content,
        tool_calls=response.tool_calls,
    )

    if response.finish_reason == "tool_calls":
        calls = validate_tool_calls(response.tool_calls)
        results = await agent.call_orchestrator.dispatch_calls(calls)

        for call in response.tool_calls:
            context.append_tool(
                tool_call_id=call.id,
                content=serialize_resource_result(results[call.id]),
            )

        return ContinueLoop()

    if response.finish_reason == "stop":
        return Final(
            validate_business_json(response.content)
        )

    if response.finish_reason == "length":
        raise OutputTruncatedError()

    if response.finish_reason == "insufficient_system_resource":
        raise RetryableProviderError()

    if response.finish_reason == "content_filter":
        raise ContentFilteredError()

    raise UnknownFinishReason(response.finish_reason)
```

## 6. 实施阶段

### P0：合同与 Fake Adapter

- LLMRequest / Message / Response
- Tool Call 序列化
- finish_reason 状态机
- fake DeepSeek responses

### P1：非流式 Tool Loop

- `/chat/completions`
- thinking disabled
- 多 Tool Call
- 参数本地校验
- JSON Output

### P2：思考模式

- `thinking` / `reasoning_effort`
- Tool Call 后完整回传 `reasoning_content`
- L0–L4 对活跃 reasoning 回合的保护

### P3：可靠性

- 400/401/402/422/429/500/503 分类
- `insufficient_system_resource`
- 指数退避
- deadline、取消、保活

### P4：流式与缓存观测

- SSE 与 keep-alive
- `stream_options.include_usage`
- cache hit/miss 指标
- reasoning token 指标

### P5：Strict Beta 实验

- strict-compatible schema builder
- Beta base URL 独立配置
- 与正式接口 A/B 测试

## 7. 测试重点

- 思考模式 Tool Call 后漏传 `reasoning_content` 必须被测试捕获
- 多工具并行结果必须按 `tool_call_id` 回传
- 非法 JSON arguments 不能执行工具
- JSON Output 空 content、length 截断不能判定成功
- 429/500/503 和资源不足进入重试
- 400/401/402/422 不原样重试
- L0–L4 不压缩活跃 Tool Call 协议消息
- Resource schema 顺序稳定，缓存命中指标可见
- 日志不展示 `reasoning_content`

## 8. 官方文档

- 首次调用：https://api-docs.deepseek.com/zh-cn/
- Chat Completions：https://api-docs.deepseek.com/zh-cn/api/create-chat-completion
- Tool Calls：https://api-docs.deepseek.com/zh-cn/guides/tool_calls
- JSON Output：https://api-docs.deepseek.com/zh-cn/guides/json_mode
- 上下文缓存：https://api-docs.deepseek.com/zh-cn/guides/kv_cache
- 思考模式：https://api-docs.deepseek.com/zh-cn/guides/thinking_mode
- 错误码：https://api-docs.deepseek.com/zh-cn/quick_start/error_codes
- 限速与隔离：https://api-docs.deepseek.com/zh-cn/quick_start/rate_limit
