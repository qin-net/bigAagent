# InsightAgent SQLite 持久化设计

> 状态：实现基线  
> 日期：2026-08-18  
> 关联文档：`Agent运行时框架设计.md`、`InsightAgent-设计文档.md`

## 1. 目标

为本机和单实例部署提供零服务依赖的持久化底座，支持：

- 5 个 Agent 的隔离 State、版本历史和乐观并发
- 用户消息、最终回答、工具协议消息的上下文归档
- Run、报告、决策和跟踪时间线
- 方法论候选与正式版本
- 大型工具结果外置存储及完整性校验
- 结构化审计事件
- 数据库初始化、状态检查和后续迁移

第一期使用 Python 标准库 `sqlite3`，无需启动数据库服务。

## 2. 非目标

- 不在 SQLite 中保存 API Key、`.env` 或其他凭据
- 不长期保存 DeepSeek `reasoning_content`
- 不把大型财报、新闻全文直接塞进消息表
- 不用数据库保存普通 stdout/debug 日志
- 不支持多主机同时写入；多实例阶段迁移 PostgreSQL

## 3. 本机布局

```text
data/
  insightagent.db
  artifacts/
    ab/
      <sha256>.txt
```

Git 忽略：

```text
data/
*.db
*.db-wal
*.db-shm
```

数据库文件、WAL、用户数据和 Artifact 不进入仓库。

## 4. SQLite 配置

每个连接执行：

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;
```

说明：

- WAL 提升读写并发
- 外键保证引用完整
- busy timeout 避免短暂写锁立即失败
- NORMAL 在本地应用中平衡性能与可靠性

## 5. 数据分类与保留

### 5.1 必须持久化

- AgentState 当前版本
- AgentState 历史版本
- Session、Run 和父子关系
- 用户输入和最终业务输出
- Tool Call 与 Tool Result 协议消息
- Report、Decision、TrackingDeliverable
- ProcessRecord（facts / clues / judgment_basis / trace_steps / 结构化 reflection）
- EvidenceRef、Artifact 元数据
- 方法论条目及版本
- 重试、失败、State 冲突等结构化审计事件

### 5.2 只在内存中保留

- DeepSeek `reasoning_content`
- 模型隐藏思维过程
- 当前 HTTP 流式缓冲
- 未完成的临时解析对象

`reasoning_content` 在同一次 AgentLoop 内用于 DeepSeek Tool Call 回传；写入 ContextArchive 时剥离。第一期不支持进程退出后从工具调用中间点继续思考，恢复时从最后一个安全 checkpoint 重新执行该轮。

### 5.3 外置 Artifact

大型工具结果写入 `data/artifacts`：

- 文件名基于 SHA-256
- SQLite 保存 ref、hash、路径、大小、媒体类型和创建时间
- 重复内容复用同一个 Artifact
- 读取时重新校验 hash
- Context 只保存摘要和 `artifact://<sha256>` 引用

## 6. Schema

### 6.1 迁移

```sql
schema_migrations(
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
)
```

迁移只向前执行；每个版本在事务中原子应用。

### 6.2 Session

```sql
sessions(
  session_id TEXT PRIMARY KEY,
  parent_session_id TEXT,
  agent_name TEXT NOT NULL,
  stock_code TEXT,
  thesis_id TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(parent_session_id) REFERENCES sessions(session_id)
)
```

Session 是 State、Context 和审计事件的归属主体。父 Session 可以为空；创建 AgentSkill 子任务时记录父子关系。

### 6.3 Agent State

```sql
agent_states(
  session_id TEXT PRIMARY KEY,
  parent_session_id TEXT,
  agent_name TEXT NOT NULL,
  stock_code TEXT,
  thesis_id TEXT,
  status TEXT NOT NULL,
  version INTEGER NOT NULL,
  state_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
)

agent_state_history(
  session_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  state_json TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  PRIMARY KEY(session_id, version),
  FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
)
```

索引：

```text
agent_name + stock_code + thesis_id
parent_session_id
updated_at
```

### 6.4 Context

```sql
context_messages(
  message_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  sequence_no INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT,
  tool_calls_json TEXT NOT NULL,
  tool_call_id TEXT,
  priority INTEGER NOT NULL,
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(session_id, sequence_no),
  FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
)
```

不设置 `reasoning_content` 列，避免误持久化。

### 6.5 Artifact

```sql
artifacts(
  ref TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL UNIQUE,
  relative_path TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  media_type TEXT NOT NULL,
  created_at TEXT NOT NULL
)
```

### 6.6 审计事件

```sql
audit_events(
  event_id TEXT PRIMARY KEY,
  run_id TEXT,
  session_id TEXT,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE SET NULL
)
```

### 6.7 业务结果

第一版预建以下表，业务 Agent 接入后使用：

```text
runs
reports
decisions
tracking_timeline
methodology_entries
methodology_versions
```

业务 JSON 保留 schema version，后续通过应用迁移升级，不直接修改历史内容。

`reports` 只存给人看的 `Report`。过程性日志不塞进 reports 行，见 §6.8。

### 6.8 过程性日志落盘

复查时要能回答：当时凭什么判、走过哪些步骤、证据是哪条、哪些只是线索。合同见 `InsightAgent-设计文档.md` §6.3 `ProcessRecord`。

| 内容 | 存哪 | 说明 |
|------|------|------|
| 本维结论 | `reports` | `Report` JSON；citations 是证据入口 |
| 抽取事实、线索、判断依据、有序步骤、reflection | **artifact**（`ProcessRecord` 全文）+ `audit_events.process_logged` | audit payload 只存 `artifact_ref`、fact/clue 条数、`used_fact_ids`、stance |
| 当时数据切片 | `artifacts` | snapshot 原文；Report / Fact 只引用 ref |
| 工具调用链 | `context_messages` | assistant tool_calls + tool 结果；大结果 L0 外置 |
| 本维记忆 | `agent_states` + `agent_state_history` | `memory_summary`、`key_evidence_refs`、已见 event_id |
| 综合拍板依据 | `decisions` | 写满 rationale / citations / falsifiers / dimensions_* |
| 发生了什么 | `audit_events` | 结构化事件，不含 CoT |

`runs` 建议增加 `process_refs`（或等价地写在 `snapshot_refs` 旁的应用字段）：`{"fundamental": "artifact://...", "technical": "..."}`。历史行不原地改；新 Run 才带。

`decision_written` payload 最少：

```text
rating, confidence, value_score, timing_score
dimensions_used, dimensions_missing
report_refs
process_artifact_refs
snapshot_refs
citation_ids
disagreements
falsifiers
as_of
```

跟踪：只追加新的 ProcessRecord / reports row / 时间线条，不覆盖首次研究那份。`agent_dispatched` 必须有 trigger_reason、target_agent、questions、input_refs。

明确不存：`reasoning_content`、API Key、未外置的全量 K 线/公告全文、事后盈亏、`LoopTracer` 内存事件（默认不入库）。

## 7. State 事务语义

读取：

```text
SELECT state WHERE session_id = ?
```

提交：

```text
BEGIN IMMEDIATE
校验 current.version == expected_version
写入 agent_states(version + 1)
追加 agent_state_history
COMMIT
```

冲突时：

- 抛出 `StateConflictError`
- 不覆盖当前版本
- 调用方重新读取并决定恢复或重跑

所有 State 返回深拷贝，避免调用者绕过 Store 修改持久值。

## 8. Context 语义

- 每个 Session 内 `sequence_no` 单调递增
- 消息按 sequence 加载
- assistant Tool Call 和对应 Tool Result 均保存
- `reasoning_content` 入库前必须置空
- 审计读取可查看原始 Tool Call/Result；恢复给模型时将旧协议链折叠为带来源引用的历史数据摘要，禁止缺少 reasoning 的 Tool Call 原样重放
- 大 Tool Result 由 L0 外置后只存摘要和 ArtifactRef
- 删除 Session 时由外键策略决定 Context 是否级联；第一期不提供自动删除命令

## 9. Audit 语义

Audit 记录“发生了什么”，不记录隐藏思维：

```text
agent_started
agent_paused
agent_completed
agent_failed
resource_called
resource_retried
resource_failed
state_committed
state_conflict
context_compacted
artifact_written
process_logged
agent_dispatched
decision_written
run_started
snapshot_ready
run_failed
```

payload 必须结构化，不允许写入 API Key 或 `reasoning_content`。

## 10. 本机 CLI

```bash
python -m insightagent db init
python -m insightagent db status
```

可选路径：

```bash
python -m insightagent db init --path ./data/custom.db
```

`db status` 输出：

- 数据库路径
- schema version
- journal mode
- 表是否齐全
- AgentState / Context / Artifact / Audit 数量

CLI 不回显敏感业务内容。

## 11. Runtime 接入

`AgentInstance` 通过依赖注入选择：

```python
AgentInstance(
    state_store=SQLiteStateStore(db),
    context_archive=SQLiteContextArchive(db),
    compactor=ContextCompactor(
        artifact_store=FileArtifactStore(db, artifact_root)
    ),
)
```

测试默认继续使用内存实现；本机正式运行使用 SQLite/File 实现。

## 12. 故障与恢复

- 数据库不存在：CLI/init 自动创建
- schema 过旧：执行未应用迁移
- schema 过新：拒绝启动，防止旧程序破坏数据
- WAL 写锁：等待 busy timeout；之后返回明确错误
- State CAS 冲突：不自动覆盖
- Artifact 文件缺失/hash 不匹配：抛出完整性错误并记录 Audit
- 进程在事务中退出：SQLite 自动回滚
- 进程在 Tool Call 中退出：从最后安全 checkpoint 重跑，不恢复隐藏 reasoning

## 13. 测试要求

- 新数据库初始化和重复初始化
- PRAGMA 生效
- State create/load/save/patch/history
- State 版本冲突
- 两个 Store 实例读取同一数据库
- Context 顺序和跨实例恢复
- `reasoning_content` 不落盘
- 恢复投影不包含缺少 reasoning 的历史 Tool Call 协议消息
- Artifact 去重、读取和 hash 校验
- Audit 追加和查询
- CLI init/status
- 原有 11 个运行时测试全部回归通过

## 14. 安全要求

- DB、WAL、Artifact 全部 Git ignore
- 不提供保存 API Key 的字段
- SQL 全部参数化
- Artifact 路径由 hash 生成，禁止用户路径穿越
- 用户文本作为数据保存，不拼接 SQL
- 日志中仅记录数据库路径，不输出记录正文

## 15. 后续迁移条件

出现以下任一情况时评估 PostgreSQL：

- 多主机部署
- 多个 worker 高频并发写
- 需要远程数据库备份和权限隔离
- SQLite 写锁成为可测量瓶颈

Store 接口保持不变，业务 Agent 不依赖 SQLite SQL。

## 16. 自审结论

实现前按完整性、一致性、安全性和可测试性复查：

- **完整性**：补充了 Session 主表，State、Context 和 Audit 均有明确归属
- **一致性**：统一 DeepSeek 协议策略——`reasoning_content` 仅存当前进程内存，不进入持久化 Archive
- **并发**：State 使用事务和版本 CAS；SQLite 使用 WAL 与 busy timeout
- **恢复**：只从安全 checkpoint 恢复；不尝试恢复中断的隐藏 reasoning
- **大对象**：Artifact 外置并校验 SHA-256，避免数据库无限膨胀
- **安全**：凭据无字段、数据目录 Git ignore、SQL 参数化、CLI 不展示正文
- **演进**：迁移有 schema version；Store 接口允许未来替换 PostgreSQL
- **验证**：测试要求覆盖重复初始化、跨实例恢复、冲突、脱敏、完整性和原运行时回归

仍保留的明确限制：

- 第一版是单机/单实例设计
- Python 标准库 sqlite 调用会在线程中执行，避免阻塞 Agent 事件循环
- 不提供自动备份、加密或数据清理策略；在引入真实用户数据前另行设计
