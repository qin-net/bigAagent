from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class AgentState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(default_factory=lambda: str(uuid4()))
    parent_session_id: Optional[str] = None
    agent_name: str
    stock_code: Optional[str] = None
    thesis_id: Optional[str] = None
    loop_round: int = 0
    status: TaskStatus = TaskStatus.READY
    business_context: Dict[str, Any] = Field(default_factory=dict)
    private_memory: Dict[str, Any] = Field(default_factory=dict)
    checkpoint: Optional[Dict[str, Any]] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    version: int = 0
    meta: Dict[str, Any] = Field(default_factory=dict)


class StatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version: int
    set: Dict[str, Any] = Field(default_factory=dict)
    append: Dict[str, List[Any]] = Field(default_factory=dict)
    remove: Dict[str, List[Any]] = Field(default_factory=dict)


class ResourceType(str, Enum):
    FUNCTION = "function"
    SKILL = "skill"
    KNOWLEDGE_BASE = "knowledge_base"
    AGENT_SKILL = "agent_skill"


class SideEffect(str, Enum):
    NONE = "none"
    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"


class ResourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: ResourceType
    description: str
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    timeout_seconds: float = 30.0
    retry_policy: str = "default"
    parallel_safe: bool = True
    side_effect: SideEffect = SideEffect.NONE
    permission_tags: List[str] = Field(default_factory=list)
    version: str = "1"

    def to_deepseek_tool(self, strict: bool = False) -> Dict[str, Any]:
        function: Dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "parameters": self.input_schema,
        }
        if strict:
            function["strict"] = True
        return {"type": "function", "function": function}


class ResourceCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    resource_name: str
    arguments: Dict[str, Any]
    execution: Literal["serial", "parallel"] = "serial"
    depends_on: List[str] = Field(default_factory=list)


class ResourceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    resource: str
    status: Literal["success", "failed"]
    data: Any = None
    data_ref: Optional[str] = None
    error: Optional[Dict[str, Any]] = None
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime = Field(default_factory=utc_now)
    attempts: int = 1


class LLMToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    arguments: str


class LLMMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(default_factory=lambda: str(uuid4()))
    role: Literal["system", "user", "assistant", "tool"]
    content: Optional[str] = None
    reasoning_content: Optional[str] = None
    tool_calls: List[LLMToolCall] = Field(default_factory=list)
    tool_call_id: Optional[str] = None
    priority: int = 50
    created_at: datetime = Field(default_factory=utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class LLMUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0
    reasoning_tokens: int = 0


class LLMRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    messages: List[LLMMessage]
    tools: List[Dict[str, Any]] = Field(default_factory=list)
    tool_choice: Any = None
    thinking_enabled: bool = True
    reasoning_effort: Literal["low", "high", "max"] = "high"
    response_format: Literal["text", "json"] = "json"
    max_tokens: int = 4096
    stream: bool = False
    user_id: Optional[str] = None


class LLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    model: str
    content: Optional[str] = None
    reasoning_content: Optional[str] = None
    tool_calls: List[LLMToolCall] = Field(default_factory=list)
    finish_reason: Literal[
        "stop",
        "length",
        "content_filter",
        "tool_calls",
        "insufficient_system_resource",
    ]
    usage: LLMUsage = Field(default_factory=LLMUsage)
    system_fingerprint: Optional[str] = None


class AgentFinalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "abstained", "degraded"] = "completed"
    output: Dict[str, Any]
    reflection: Dict[str, Any] = Field(default_factory=dict)
    state_patch: StatePatch


class AgentTaskPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    target_agent: Literal["fundamental", "technical", "sentiment", "macro"]
    objective: str
    reason: str
    required_questions: List[str] = Field(default_factory=list)
    required_evidence: List[str] = Field(default_factory=list)
    context_refs: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    completion_criteria: List[str] = Field(default_factory=list)
    output_schema_version: str = "1"
    as_of: datetime = Field(default_factory=utc_now)
