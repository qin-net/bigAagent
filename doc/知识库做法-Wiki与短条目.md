# 知识库做法：Wiki 原料 + 短条目检索（不做全书 RAG）

> 状态：**K0–K2 已实现**（检索闸门、SQLite 卡片、追踪蒸馏 Task；跟踪心跳未做）  
> 日期：2026-08-24  
> 位置：本仓库 `doc/`  
> 关联：`InsightAgent-设计文档.md` §9、`知识库-PDF转Markdown.md`、`知识库条目清单-经管对照查找.md`、四份 `知识库资料-*.md`  
> 书目已齐之后的干活顺序：`知识库资料齐了怎么处理.md`  
> 通审用的结构稿：`知识库与追踪-架构.md`  
> 冻结口径（已有）：规则清单为主、条文为辅；禁止装饰性引用；正式条目要审核和版本；盈亏不得自动改方法论。

---

## 0. 结论：不要做全书 RAG，也不要把 Wiki 当运行时

资料齐了之后，知识库要做成 **三层**，不是「把经管交的 PDF 切块丢进向量库」。

| 层 | 给谁看 | 形态 | 进不进 `analyze` 的 Prompt |
|---|---|---|---|
| L0 原料库 | 人溯源；经管只交原件 | 本机 Markdown（PDF 本地转） | **不进** |
| L1 方法论条目 | Agent + 审核页 | 短卡片：trigger、action、例外、出处、版本 | **只进命中的几条** |
| L2 个人决策手册 | 普通用户 | 原则→清单→反例，只链 `approved` 条目 | 不进专家 System |

**运行时检索 = 带 scope 的关键词 / 触发词检索短卡片**（把现有 `search_methodology` 从内存数组换成库表）。不是对 Penman 全书做 embedding RAG。

向量检索 **可以以后加**，且只加在 **已批准的短条目标题+trigger+text** 上（几百条量级），用来补关键词同义词。不要对 L0 教材、准则全文做 chunk RAG。

理由很具体：

1. 你们已经否定过「大师语录 RAG + 单次研报」。全书 RAG 会把模型变成摘抄机，citations 装饰性——技术面已经出现过「RSI 没超买却引 `kb_rsi_overbought`」。  
2. Agent 每次只要几条可执行纪律，不要一章现金流量表。Token、速度、是否用对，都比召回率好看更重要。  
3. 历史报告必须能还原 **当时那一版** 条目。Wiki 页面会改；向量库里的 chunk 没有干净的 `entry_id + version`。卡片 + `methodology_versions` 才对得上。  
4. 教材/准则版权：L0 全文不宜进 git、不宜进云端 embedding 服务。蒸馏后的短句 + 出处文号可以进库。  
5. 真人要能审、能驳回、能从报告跳到条目。这是 Wiki/手册的事，不是 ANN 索引的事。

一句话：**Wiki 管「资料从哪来、人怎么读」；卡片管「Agent 碰到什么情况翻哪几条」。RAG 如果出现，只是卡片的同义词索引，不是知识库本体。**

---

## 1. 和现有东西怎么接

已经有的：

- 经管清单：四角色要找的全书/全文（L0 原料目录）。  
- `pdf2md`：PDF → 可校对 md（L0 生产）。  
- 设计 §9 条目 JSON：`id/title/type/scope/trigger/action/.../status/version`。  
- SQLite 已建 `methodology_entries` / `methodology_versions`，**写入和检索还没接到 analyze**。  
- 运行时：`search_methodology(query, scope=角色)`，关键字打在 trigger+text 上，最多 5 条，只 `approved`。

缺的就是：**蒸馏规程、条目合同冻结、从 md 进库、审核流、引用必须能对上 snapshot 字段**。经管不写卡片。**蒸卡 = 追踪 Agent 的蒸馏 Task**（与以后跟踪调度同一角色）；只写 candidate，咱们 CLI 批准。分析 Agent 运行时不读文献。小时心跳后做，不必另起蒸馏 Agent。

用户偏好（`UserPreference`）**不是**知识库。偏好立刻生效、绑用户；方法论要闸门、绑角色、所有用户同一套纪律。禁止用 `#remember` 写进 methodology 表。

---

## 2. 目录怎么放

```text
data/kb/
  incoming/              # 原件 PDF/HTML，gitignore
  markdown/              # pdf2md + 人工校对，按角色分子目录
    fundamental/
    technical/
    sentiment/
    macro/
  wiki/                  # 给人逛的索引页（可 git）
    README.md            # 按角色/主题目录
    sources.md           # 书目与文号，不贴全书
doc/kb-entries/          # 蒸馏稿，可 git（短、无版权全书）
  kb_cashflow_lag.md
  ...
```

`doc/kb-entries/*.md` 是 L1 的人读形态，与库表同步（导入脚本）。不要把教材 md 提交进 `doc/`。

Wiki 第一期不必上飞书/Notion。Git 里一组 md + 以后看板或方法论页能渲染即可。经管要协作再镜像到飞书，**批准源仍以仓库/SQLite 版本为准**。

---

## 3. 蒸馏：怎么从「齐了的资料」变成卡片

禁止：脚本把 md 按 512 token 切片写入向量库当条目。

流程：

```text
一章/一份公告 md（本地 pdf2md，不发 PDF）
  → 追踪 Agent 蒸馏 Task（离线；不是四个分析 Agent）抽可执行纪律
  → 每张卡片：何时用、看哪些已有 flag、例外、出处
  → 写入 candidate（evidence_required 必须落在冻结字段表上）
  → 对照夹具（如 000858 现金流滞后）看会不会误召
  → 咱们 approved 才进 search_methodology
```

一次只喂 **一章或数页 md 字符串** + 允许的 flag 名。不喂整本、不喂 PDF、不把候选直接标 approved。经管不参与蒸卡。入口即追踪 Agent，见 `知识库资料齐了怎么处理.md`。

### 3.1 什么算一张合格卡片

必须同时有：

- 稳定 `id`（如 `kb_cashflow_lag`），批准后不改 id，只升 version。  
- `scope`：`fundamental` / `technical` / `sentiment` / `macro` / `tracking` 的子集，分析 Agent 检索时 **不得跨角色**（基本面搜不到 RSI 条）。  
- `type`：`rule`（可执行）优先；`principle` 少而短；`checklist`；`anti_pattern`；`lesson`（来自 Case，不是来自书）。  
- `trigger`：词表 + 可机器对齐的条件（见 §4）。  
- `action`：专家该检查什么，**不是买卖指令**。  
- `evidence_required`：snapshot / rule_id / 事件 flag 的字段名。没有这些字段就不得引用该条。  
- `exceptions`：何时不适用。  
- `source_refs`：文号或书名+章节，不贴大段原文。  
- `text`：给模型看的 ≤ 200 字（硬顶，超了截断并 audit）。  

反例（不要入库）：

- 「价值投资要有安全边际」而无字段。  
- 整段准则原文。  
- 与 `computed_flags` 无关却会被任意 query 命中的条（现网 `if not tokens or any(token in haystack)` 过宽，正式检索要改，见 §4）。

### 3.2 建议的第一批主题（有资料就能蒸）

不必等手册写完。按你们已经在跑的规则对齐，先把「机器已经在算、但条目还是口号」的补全：

| id 方向 | 对齐的数据/规则 |
|---|---|
| 盈利要现金流验证 | `cashflow_lag`、经营现金流 vs 净利 |
| ROE 与杠杆 | `roe_quality` |
| 估值看自身分位 | `pe_percentile_5y`，禁止只看绝对 PE |
| 均线排列不是买卖点 | `ma_*`，禁止空头排列直接 sell 话术升级成知识 |
| RSI 超买/超卖引用门槛 | **仅当** rsi 过线才允许对应 kb_id |
| 回购/增持以公告为准 | `has_buyback` 等 event_flags |
| 宏观利率对个股相关度 | 相关度 low 则弃权，条目写「不构成买卖理由」 |
| 非经常性损益 | 对应证监会 2023 年 65 号，字段缺则 missing，不编 |

每张卡片蒸完，用一次 fixture 或已有 live JSON 做「该引 / 不该引」对照，写进测试，比先上向量准。

---

## 4. Agent 怎么找（检索方法）

`search_methodology(query, scope)` 保持工具名不变。内部改成：

1. 只查 `status=approved` 且 `scope` 含本角色。  
2. **默认 query 为空或过短：返回 `[]`**，禁止「空查询把该维前 5 条全塞进去」（这是装饰性引用的温床）。  
3. 命中方式（按顺序，都是结构化，不是 RAG）：  
   - **字段门**：调用方传入当前 snapshot 的 `computed_flags` / `event_flags` / `rule_hits`（工具 schema 可加可选参数 `flags: string[]`）。条目 `evidence_required` 与 flags 有交集才进入候选。  
   - **触发词**：query 分词与 `trigger` 求交。  
   - 仍太多：按预先写的 `priority` 取最多 **3 条**（从 5 降到 3，省 token）。  
4. 返回 `id, version, text, trigger`。citations 只允许这些 id；sanitize 时 **id 不在本次检索结果里的 kb 引用丢掉**。  
5. 检索结果写入该次 session 的工具返回，报告引用必须能对上这次返回。历史回放用 version。

向量（可选，L1 后期）：对 `title+trigger+text` 做本地 embedding，仅在关键词 0 命中且 flags 已收窄时补 1～2 条，仍要过 `evidence_required` 门。不做「语义上有点像价值投资」的跨条乱入。

专家 System 里写死：未检索到不得编 kb_id；flags 对不上不得引。这比换 RAG 模型更有效。

---

## 5. 治理（Wiki 审，库生效）

```text
人/模型蒸馏 → candidate（可检索仅限审核页）
  → 评测夹具 + 人审
  → approved（search_methodology 可见）
  → 改内容只加新 version，旧报告仍指向旧 version
  → retired（不再命中，页面仍能打开）
```

追踪 Agent 只能写 candidate、去重提案，**不能** self-approve（设计已冻结）。蒸馏轮次同样走这条闸门。CLI：

```text
python -m insightagent kb import doc/kb-entries/kb_cashflow_lag.md
python -m insightagent kb approve kb_cashflow_lag
python -m insightagent kb show kb_cashflow_lag
```

审核页（可并进看板后期）：按角色筛选、看出处、批准/驳回、版本 diff。从报告 citation 跳到 `id@version`。

Case 晋升（P3）：同类过程错误才开 lesson 类 candidate，不得因「后来跌了」批准一条。

---

## 6. 和 Wiki / RAG 的对照（给经管）

| | 全书 RAG | 纯 Wiki（Agent 读页面） | 本方案 |
|---|---|---|---|
| 召回 | 段落相似，难审计 | 人能读，模型易整页塞进 Prompt | 每次 0～3 条短纪律 |
| 引用 | 页码飘、易装饰 | 页面改了旧报告对不上 | `kb_id` + version |
| 成本 | 每次嵌入+长上下文 | 页面一长就爆 token | 固定上限 |
| 合规 | 全书进向量库风险高 | 全文托管要版权 | 短句+文号 |
| 和规则引擎 | 两套真相 | 和 `cashflow_lag` 对不齐 | 字段门对齐机算规则 |

真人 Wiki 仍然要：目录、书目、某条纪律「为什么这么写」。它服务的是 **人**，通过蒸馏才服务 Agent。

---

## 7. 分期

| 期 | 做什么 |
|---|---|
| K0 | 冻结本文件；改 `search_methodology`：空 query 不返回、scope 隔离、最多 3 条；sanitize 丢掉未检索 kb_id |
| K1 | 条目改走 SQLite 版本表；从 `doc/kb-entries` 导入；把现有内存 10 条迁成正式卡片并补 `evidence_required` |
| K2 | 追踪 Agent 蒸馏 Task 出第一批 candidate；夹具「该引/不该引」；咱们批准 |
| K3 | 审核 CLI/简单页；手册 L2 只链 approved |
| K4 | 可选：短卡片本地向量；跟踪心跳 + AgentSkill（同一追踪 Agent） |

K0 不依赖资料是否齐，先止住乱引用。K2 才消耗经管全文。蒸馏与跟踪共用追踪 Agent，不另起角色。

---

## 8. 不做

```text
对 L0 教材/准则做 chunk embedding RAG
Agent 运行时读 PDF 或整本 md
空 query 返回该维全部条目
用户偏好写入方法论库
自动 approved
用事后涨跌晋升条目
citations 引用未检索到的 kb_id
把飞书当唯一真相源
```

---

## 9. 验收（怎样算知识库「做成了」）

- 基本面在 `cashflow_lag` 为真时能引到现金流条目；为假时引不到。  
- 技术面 RSI 未超买不得出现 `kb_rsi_overbought`。  
- 宏观利率条不得出现在基本面工具返回里。  
- 同一 `id` 改文案后，旧 run 的 JSON 仍显示旧 version 正文。  
- `analyze` 一次方法论相关 token 可估算：≤ 3×200 字 / 专家。  
- 经管能在 Wiki 索引里从条目点回书目/文号，而不是点进向量命中片段。
