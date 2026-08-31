# bigAagent

面向 A 股的轻量投研协作系统：输入股票代码，得到可溯源的四维研究报告；用户决定是否投入、何时追踪。对外口径是**研究辅助，不是投资建议**。

仓库里有两层：

- **InsightAgent**：单实例 Agent 运行时 + 首次研究 workflow + 追踪。核心是「谁在想、记住了什么、下次怎么带上」。
- **InsightBoard**：本地看板（延迟行情、模拟盘、用户画像、专家工作台）。研究任务从页面排队，由同一套 Agent 执行。

专家之间不互辩、不共享可写状态。综合决策、编排、校验、落盘是 Python 系统环节，不包装成第六个 Agent。

---

## Agent 是谁

系统里只有 **5 个 Agent**：四位分析专家 + 一位追踪。首次研究由应用层 workflow **并行**叫醒四专家；追踪轮次由追踪 Agent 自主决定叫不叫、叫哪几位（也可以谁都不叫，直接 `unchanged`）。


| 角色  | 职责                                    | 记忆归谁                     |
| --- | ------------------------------------- | ------------------------ |
| 基本面 | 盈利质量、现金流、估值是否对得上                      | 只继承自己的 `private_memory`  |
| 技术面 | 趋势、位置、量价节奏                            | 同上                       |
| 情绪  | 公告/事件与持仓结构，不把舆情当事实                    | 同上                       |
| 宏观  | 政策与宏观约束，不给个股定价                        | 同上                       |
| 追踪  | 对照原判断看是否维持、复核或证伪；蒸馏时只写知识库 `candidate` | 自己的追踪记忆；**不能改**四专家 State |


约束（实现已按此冻结）：

- 专家只读本轮快照和 **已批准** 的方法论短卡片，不读用户原话、不读 L0 文献全文。
- 追踪蒸馏与跟踪调度是**同一个**角色、同一套 System，差别只在本轮 Task 和打开的工具。
- 知识库批准是人，不是任何 Agent。专家从不写卡片。

五位业务 Agent 都跑在同一套**单实例运行时**上，见下一节。应用层 workflow 只负责「叫醒谁、收作业、做综合决策」，不进入任一 Agent 的内部 Loop，也不共享 `AgentState`。

---

## 单实例 Agent 框架

这是仓库的内核（`src/insightagent/runtime.py` 及 `context.py` / `resources.py` / `state.py` / `retry.py`），业务专家和追踪都是挂在上面的角色，不是另一套编排器。

没有「统一调度所有 Agent 的全局 Agent」。每个 `AgentInstance` 自治，自带本地调度与生命周期：

```text
┌─────────────────────────────────────────────────┐
│ AgentInstance（按角色隔离）                       │
│  AgentLocalScheduler   本 Agent 生命周期与 Loop   │
│  AgentLoop             模型 ↔ 资源 标准循环       │
│  ContextCompactor      当次消息 L0–L4 压缩        │
│  AgentState            生命周期 + 私有记忆        │
│  ResourceRegistry      本 Agent 私有能力表        │
│    FunctionTool / Skill / KnowledgeBase / AgentSkill │
│  CallOrchestrator      串行或并行调资源           │
│  RetryPolicy           指数退避                   │
│  ContextBuffer         当前会话消息               │
│  ContextArchive        压缩前原文归档（不存思维链） │
└─────────────────────────────────────────────────┘
```

**State 与 Context 必须拆开：**


| 对象               | 是什么                                 | 压缩会不会改它   |
| ---------------- | ----------------------------------- | --------- |
| `ContextBuffer`  | 当次发给模型的消息、工具结果                      | 会，走 L0–L4 |
| `AgentState`     | session、checkpoint、`private_memory` | 不会        |
| `ContextArchive` | 压缩前可回放的原文                           | 不进 Prompt |
| 方法论库             | 外部资源，经 Registry 检索                  | 独立于压缩流水线  |


这里的 **L0–L4 是同一轮消息的压缩台阶**，不是知识库的 L0 原料 / L1 卡片，也不是五类记忆。

每轮 LLM 调用前：先估 token；超预算则依次 L0 工具结果预算 → L1 裁旧消息 → L2 微压缩长工具结果 → L3 折叠更早轮次 → L4 摘要。一层成功立刻停。`max_loop_round`（默认 15）和并发上限是**熔断**，不规定专家该想几步。

标准 Loop：

```text
load_or_create State
  → 写入本轮 Task（可带 prior_memory / 用户口径）
  → while RUNNING:
        compact → LLM（tools 含 submit_final）
        → 普通工具：CallOrchestrator 可并行
        → submit_final：校验 schema，盖章 state_patch，SUCCESS
  → 超轮次或非法输出：失败 / 重试 / 不覆盖冲突版本
```

模型不直接写库。业务字段只能出现在 `state_patch` 的 set / append / remove；`base_version`、`loop_round` 由调度器盖章。真正的版本冲突不覆盖，重新加载后再跑。归档与审计**不保存** `reasoning_content`。

资源对模型长得一样，不必区分底层是函数还是另一个 Agent：


| 类型              | 例子                                                |
| --------------- | ------------------------------------------------- |
| `FunctionTool`  | `get_fundamental_snapshot`、`get_macro_snapshot`   |
| `Skill`         | 多步封装（若注册）                                         |
| `KnowledgeBase` | `search_methodology`（只命中已批准卡片）                    |
| `AgentSkill`    | 追踪调用 `call_fundamental` 等；调用方不进对方 Loop、不写对方 State |


LLM 与工具失败走 `ExponentialBackoff`（可尊重 `Retry-After`）。适配器是 DeepSeek Chat Completions；测试可用 Fake LLM 把整条 Loop 跑通。

首次研究：workflow **并行**四个 `AgentInstance.run()`，然后系统做综合决策。追踪：一个追踪实例把四专家注册成 `AgentSkill`，由它的本地调度器决定叫谁。

---

## 记忆分三套，不要混


| 套           | 存什么                      | 谁读写                        | 进 Prompt 的位置                      |
| ----------- | ------------------------ | -------------------------- | --------------------------------- |
| **专家私有记忆**  | 对某只票、某个 thesis 的假设、证伪、教训 | 仅该 `agent_name`            | 下次 Task 的 `prior_memory`          |
| **用户记忆**    | 意图槽位 + 明确要求「记住」的口径       | 编排写入，按维注入对应专家 Task         | Task 约束句；**不进** System / Evidence |
| **方法论库 L1** | 已批准的短规则卡片                | 专家 `search_methodology` 只读 | 工具返回的短文，须能引用                      |


另外还有看板侧的**选股记忆**（「这只票为什么进池子」）和可选的 **AI 投资者画像**（根据已记住口径与模拟持仓归纳，不存原话）。它们不是专家 State。

上下文压缩（L0–L4）只压缩**当次消息**，不是第五套记忆库。

---

## 专家记忆怎么活过下一轮

每次成功分析或追踪都开 **新 session**，不复用聊天记录。

```text
上次 SUCCESS 的 AgentState.private_memory
        │  仅同一 agent_name + thesis_id
        ▼
snapshot（白名单字段，截断）
        │
        ├─ 拷进新 session 的 private_memory（可再 patch）
        └─ 写入本轮 Task JSON 的 prior_memory
```

白名单字段：

- `memory_summary`
- `lessons`
- `active_hypotheses`
- `falsifiers_watched`
- `open_questions`
- `pending_tasks`
- `key_evidence_refs`
- `prior_output_refs`

模型只能通过 `state_patch`（set / append / remove）改这些业务字段。版本号和 loop 由运行时盖章。基本面写的记忆，技术面下次看不到。

看板「专家工作台」按专家合并跨标的经验总结；按股票的最新沉淀放在「分次分析记录」里，默认收起。数字是**覆盖了几只标的的最新记忆**，不是追踪把四专家全员重跑了几次。

---

## 用户记忆怎么进系统

用户在分析前、结论后或追踪时可以写一句话（可带 `#记住` `#基本面` `#重跑` 等 tag）。编排**先剥 tag，再用模型抽槽**，得到六维都填满的 `UserIntent`（抽不出就 `"none"`，允许全 `"none"`）。

```text
用户输入 ──parse_tags──► tags / effect / body
        ──LLM 抽槽──► UserIntent
                      fundamental / technical / sentiment
                      macro / decision / tracking
        ──分流──►
              this_run     只注入本轮对应专家 Task
              remember     过记忆闸门后写入 UserPreference
              rerun        按被点名的维重跑（可与 remember 组合）
```

硬规则：

1. **不落用户原话。** `UserUtterance` 只留元数据；audit 不带原文；prompt 只在 worker 内存里传到抽取。
2. **用户内容只进 Task。** System 与证据绑定看不到用户句子，避免把口径当成行情事实。
3. **记住要过闸门。** 必须是可执行的短规则（例如带「对照 / 核对」），不是情绪发泄或事后涨跌总结。事后盈亏可以进复盘对照当初的 `falsifiers`，但**不得**自动回写旧报告、自动改偏好或晋升方法论。
4. **按维分发。** `#基本面` 的口径只约束基本面专家；`#决策` 进综合决策约束；追踪口径留给追踪 Agent。

`UserPreference` 有版本、可停用，默认按标的 + 全局（`stock_code=none`）一起生效。看板「用户画像」里可以查看并停用已记住口径。

---

## 一次研究怎么走

```text
看板选股 → 首次分析（四专家并行）→ 综合决策
        → 用户可记住口径 / 按维反馈重跑
        → 是否用虚拟资金买入（可选）
        → 用户手动触发追踪（对照 thesis，可少叫或不叫专家）
        → 专家私有记忆与用户口径进入下一轮
```

追踪默认输出维持 / 需要复核 / 原判断失效，不做高频荐股。没有小时巡检 cron。

---

## 数据落在哪


| 文件                     | 内容                      | Git            |
| ---------------------- | ----------------------- | -------------- |
| `data/kb.db`           | 方法论卡片（L1）               | 跟踪知识库          |
| `data/insightagent.db` | Run、报告、Agent 会话、用户意图与偏好 | 忽略（本地日志）       |
| `data/board.db`        | 行情、自选、模拟盘、选股记忆          | 忽略             |
| `data/kb/markdown/`    | 文献转写（L0）                | 忽略；不把 PDF 发给模型 |


默认研究库与知识库是拆开的：本地分析日志不会进 git。

---

## 使用说明

延迟行情，仅供研究辅助，不构成投资建议。虚拟资金不是实盘。

### 启动

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev,board,akshare]"
cp .env.example .env                 # 填入 DEEPSEEK_API_KEY
.venv/bin/python -m insightboard serve
```

浏览器打开 [http://127.0.0.1:8000](http://127.0.0.1:8000) 。一条命令同时拉起接口、页面和研究后台。改完 Python 路由后要重启服务；页面脚本刷新即可。

### 在看板上怎么用

1. **采集行情。** 点「采集行情」，等全市场延迟报价入库。市场页可搜代码或名称。
2. **点进一只股票。** 打开「从研究到持续决策」四步：首次分析 → 投资决策 → 持续追踪 → 沉淀迭代。
3. **首次分析。** 口径框可空，也可写一句（例如「记住：估值须核对经营现金流」），点「开始首次分析」。四位专家并行，完成后看四维结论和综合决策。
4. **反馈。** 对结论不满意，在反馈框写要求，点「提交并按需重跑」。带「记住」且过闸门的短规则会留下；点名某一维可只重跑那一维。原话不存盘、不当行情证据。
5. **模拟投入（可选）。** 分析完成后解锁。填写数量和买入理由，用虚拟资金按当前延迟价成交，持仓随行情重估。可先加入自选、不买。
6. **手动追踪。** 有基线之后，写本轮追踪重点（可空），点触发追踪。系统对照当初判断，可能维持、建议复核或宣布证伪，不默认每天重写研报。追踪后同样可以反馈。
7. **看沉淀。** 「用户画像」管理已记住口径，并可生成投资者画像。「专家工作台」看四位专家（及追踪）的跨任务经验；「分次分析记录」是各标的最新沉淀，默认收起。

口径常用说法：`记住`、`基本面` / `技术面` / `情绪` / `宏观` / `决策` / `追踪`、`重跑`。记住须是可执行的短规则（如带「对照」「核对」），不是情绪发泄。

### 开发与维护

```bash
PYTHONPATH=src:. .venv/bin/python -m pytest    # 离线测运行时；test_live_* 会出网
.venv/bin/python -m insightagent db init
.venv/bin/python -m pip install -e ".[docs]"
.venv/bin/python -m insightagent pdf2md         # 本地 PDF 转 Markdown，不上传
```

---

## 设计文档

细节以 `doc/` 为准，不要把旧的 `Aagent/doc/` 当现行稿。

- `doc/InsightAgent-设计文档.md` — 产品与五 Agent 合同
- `doc/Agent运行时框架设计.md` — 单实例运行时、State 与上下文压缩
- `doc/用户意图与偏好记忆设计.md` — 用户意图、两道闸门、不存原话
- `doc/知识库与追踪-架构.md` — 五角色、三库、L0/L1、蒸馏与跟踪
- `doc/SQLite持久化设计.md` — 落盘合同
- `doc/看板接入研究功能-B2.md` — 看板如何排队研究任务

