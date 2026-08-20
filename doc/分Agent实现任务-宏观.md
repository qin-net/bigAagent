# 分 Agent 实现任务（宏观）

> 状态：可转交实现  
> 日期：2026-08-20  
> 运行时：**复用现有 `AgentInstance`，禁止另起框架**  
> 模板：`src/insightagent/technical_agent.py`（比基本面更新：工具只读 context、`submit_final`、`max_tokens=32768`）  
> 关联：`InsightAgent-设计文档.md` §4（宏观为轻）、`分Agent实现任务.md`（技术面/情绪已交）

系统已冻结 **4 个分析角色 + 1 个追踪角色**。基本面、技术面、情绪已经在跑。本次只把第四个分析角色交给你：

| 四角色 | 重量 | 本次谁做 |
|--------|------|----------|
| 基本面 `fundamental` | 重 | 已有，不要改 |
| 技术面 `technical` | 重 | 已有，不要改 |
| 情绪 `sentiment` | 中 | 已有，不要改 |
| **宏观 `macro`** | **轻** | **你写** |
| 追踪 `tracking` | 中 | 不做 |

这是分析角色，不是追踪、不是综合决策。宏观 **不主导买卖**。宏观失败或弃权时，整次研究 **降级继续**，不要把 Run 打成 failed（基本面失败才失败）。

---

## 0. 框架怎么用

和另外三个一样，不要包装：

```python
agent = AgentInstance(name="macro", llm_adapter=..., config=macro_runtime_config())
register_macro_tools(agent, context)
final = await agent.run(user_query, session_id=..., business_context={...})
report = parse_macro_report(final.output)  # AgentFinalResponse.output.report
```

`name` 必须是 `"macro"`。后面 `reports.agent_name`、session、审计都靠它区分。

禁止新建 Loop、Agent 基类、LangGraph/A2A/MCP、自己的 LLM adapter。

**不要动：** `runtime.py`、`resources.py`、`llm.py`、`state.py`、`akshare_map.py`、`tools.py`、`fundamental_agent.py`、`technical_agent.py`、`sentiment_agent.py`、`persistence.py`。

**可以改（为了接上首次研究）：**

- `business_contracts.py`：给 `Report` 补宏观扩展字段（见下）
- `market.py`：只 **新增** `compose_macro_fields`，不要改已有 compose
- `fundamentals.py`：加 `AkshareMacroAdapter` / `FixtureMacroAdapter`，可加 1 条宏观 KB 占位
- `evidence.py`：宏观报告单独绑定，不要拿财务 snapshot 去绑 LPR
- `workflows/initial_research.py`：第四路并行，照技术面抄
- `decision.py`：`build_multi_factor_decision` 吃第四份 Report
- `research_store.py` / CLI：能 `save_report(..., "macro", ...)` 并打印一行（`save_report` 已按 agent_name 通用，多半只用改 workflow 和 `format_cli_text`）

没有「主 Agent」对聊。分 Agent 之间不通信。

---

## 职能

回答三件事：

1. 当前利率环境标签是什么（有数据就说 LPR/Shibor **原值**）  
2. 和这只股票 **相不相关**  
3. 不相关或没数据 → 弃权，不要编宏观故事

`stance`：只允许 `hold` 或 `abstain`。禁止 `buy` / `sell`（宏观不是买卖信号）。  
`score`：1–5，表示「环境标签有多清楚」，不是该不该买。低相关弃权时 score 可给 1–2。

停止：标签说完且相关性已判定，或关键利率缺失则弃权。

禁止：估值/财务、均线、公告减持、预测股价、用 PMI/社融/汇率（工具里没有）、把宏观写成「所以应该买五粮液」。

---

## 数据从哪来（你不打 AKShare）

编排先拉切片再塞进 context。现成能力：

- `MarketService.get_macro_snapshot()` → `MacroSnapshot`：`lpr_1y`、`lpr_5y`、`shibor_overnight`、`notes`、`missing_fields`  
- fixture：`synthetic_market_fixture()["macro"]` 已有 `lpr_1y=3.0`、`lpr_5y=3.5`、`shibor_overnight=1.4`  
- 行业：从 `fetch_stock_profile` 的 `industry` 带一行进 context（只要字符串，不要整份财务）

在 `market.py` **只新增**：

```python
async def compose_macro_fields(self, stock_code: str) -> dict:
    code = normalize_stock_code(stock_code)
    macro = await self.get_macro_snapshot()
    profile = await self.fetch_stock_profile(code)
    return {
        "macro": macro.model_dump(mode="json"),
        "industry": profile.industry,
        "stock_code": code,
        "company_name": profile.company_name,
    }
```

适配器照技术面：

```python
class AkshareMacroAdapter:
    async def fetch_macro(self, stock_code: str) -> dict:
        return await MarketService(AkshareMarketClient()).compose_macro_fields(stock_code)

class FixtureMacroAdapter:
    async def fetch_macro(self, stock_code: str) -> dict:
        return await MarketService(FixtureMarketClient(fixtures)).compose_macro_fields(stock_code)
```

`workflows/initial_research.py` 里 `build_market_adapter_for(..., dimension="macro")` 接上这两类。

---

## 工具清单（只读 context）

| 工具 | 合同 | 干什么 |
|------|------|--------|
| `get_macro_snapshot` | `MacroSnapshot` | 主工具；返回 payload 时附上 `computed_flags`（规则打的）和 `industry` |
| `get_artifact` | 同基本面 | 校验 ref 后展开本次 macro artifact |
| `search_methodology` | 本地 KB | 检索词含 LPR/利率/宏观；没有条目返回 `[]` |

不要注册：基本面 snapshot、指标/K 线、公告、新闻、股东变动。

Prompt：先调 `get_macro_snapshot`。`lpr_missing` 或 `low_relevance` → `abstain=true`、`stance=abstain`。数字必须从工具原样抄，不四舍五入。

EmptyArgs 用和其他 Agent 一样的 `dummy: str = ""` 占位（DeepSeek 不允许空 object）。

---

## 代码规则（`macros.py`，进模型前打标）

```text
lpr_missing          # lpr_1y 为空
low_relevance        # 行业不在利率敏感名单
rate_sensitive       # 行业在名单内（银行、地产、非银金融等）
```

利率敏感行业（先写死小名单，大小写/子串匹配即可，不要引入新数据源）：

- 含「银行」「地产」「房地产」「保险」「证券」「信托」→ `rate_sensitive`  
- 其他（含白酒、食品、医药、空行业）→ `low_relevance`  
- 两边不要同时出现

`low_relevance` 时 Agent **必须弃权**，summary 写明「与当前利率环境相关性低，本维不形成方向」。  
`lpr_missing` 时同样弃权，`degraded=true`。

不要根据三个利率点子发明「宽松/紧缩周期」的硬阈值（没有历史序列）。`cycle_tag` 只允许：

- `rate_data_available`（有 LPR）  
- `insufficient`（缺 LPR）  
- 或 `None`

`market_bias` 只允许 `neutral` / `unclear` / `None`。禁止 `bullish`/`bearish` 这种像荐股的词。

---

## 输出合同

`Report.role = "macro"`。

在 `business_contracts.Report` **新增可选字段**（`extra=forbid`，不补会校验失败）：

```python
cycle_tag: Optional[str] = None
market_bias: Optional[str] = None          # 仅 neutral | unclear
relevance_to_stock: Optional[str] = None   # high | low | unknown
```

已有 `valuation` / `trend` / `event_flags` 宏观不要填。citation：`kind=field`（id=`lpr_1y` 等）或 `kind=rule`（id=`low_relevance` / `lpr_missing`）。非弃权必须有 citations；弃权时 citations 可空。

`AgentFinalResponse`：与技术面相同——`submit_final`，不要让模型写 `base_version` / `loop_round`。弃权则 `status="abstained"`。

过程块同样要齐：`output.report` + `facts` + `clues` + `judgment_basis` + `trace_steps`，`reflection` 走顶层。宏观几乎没有新闻线索，`clues` 可 `[]`。`facts` 里的数字必须来自 snapshot。禁止写入 `reasoning_content`。

---

## 接到首次研究 workflow

照 `_run_technical_agent` 抄 `_run_macro_agent`：

1. ingest：`fetch_macro` + artifact，`run.snapshot_refs["macro"] = ...`  
2. `asyncio.gather` 里加第四个 future  
3. 宏观抛错 → `_abstain_macro()`，audit `agent_failed`，**不要 raise**（基本面除外）  
4. `save_report(run_id, "macro", report)`  
5. `AnalysisOutcome` 增加 `macro_report`  
6. `format_cli_text` 加一行：`宏观 stance / score / abstain / relevance`

宏观拉数失败：当空 snapshot + 弃权报告，Run 仍可以是 `degraded`。

---

## 决策怎么吃宏观（`decision.py`）

改 `build_multi_factor_decision(...)`，增加参数 `macro_report: Report`。

规则：

- `rating` **仍然**只由基本面/技术面/情绪的非弃权 stance 多数决定，**宏观不投票**  
- `value_score` 仍来自基本面；`timing_score` 仍来自技术面  
- 宏观未弃权：`dimensions_used` 加上 `"macro"`；rationale 加一句环境标签 + 相关性  
- 宏观弃权：`dimensions_missing` 含 `"macro"`  
- **修正现有 bug：** `dimensions_missing` 不要写成「used 里没有 macro」。改为：四维里谁没跑或谁 `abstain`，谁进 missing  
- `confidence`：缺宏观仍按缺维打折（现在已经按 missing 乘 0.6；改 missing 定义后宏观弃权会继续打折，这是预期）  
- citations：宏观非弃权时把其 citations 并进去；宏观弃权不加空引用  

不要因为 LPR=3.0 就把综合 rating 改成 buy/sell。

---

## 证据绑定

不要对宏观报告调用现有 `bind_report_evidence(report, fundamental_snapshot)`（会把 LPR 当无出处数字干掉）。

新增 `bind_macro_report_evidence(report, macro: MacroSnapshot) -> None`：

- 从 summary / cycle_tag / market_bias / relevance / risks 抽数字  
- 允许：snapshot 里的 `lpr_1y`、`lpr_5y`、`shibor_overnight`；年份和 1–5 分跳过（与现有 YEAR_RE / SCORE_RE 一致）  
- 日期抽取规则跟 `evidence.py` 已有的 `strip_dates` 走  
- 对不上则 `EvidenceBindingError`；workflow 里 `unbound_policy=abstain` 时改成宏观弃权，不要让整次 failed（除非你明确对宏观也 fail）

数字必须与 snapshot **原值**一致，不要放宽约数。

---

## 文件与符号

| 文件 | 内容 |
|------|------|
| `src/insightagent/macros.py` | `apply_macro_rules(macro, industry) -> {flags, cycle_tag, relevance}` |
| `src/insightagent/macro_agent.py` | Agent |
| `tests/test_macro_agent.py` | FakeLLM，不必 live DeepSeek |
| 上述 workflow / decision / contracts / adapters | 接线 |

符号：`MACRO_SYSTEM_PROMPT`、`macro_runtime_config`、`register_macro_tools`、`parse_macro_report`、`MacroToolContext`。

`macro_runtime_config` 对齐技术面：`deepseek-v4-flash`、`thinking_enabled`、`reasoning_effort="high"`、`max_tokens=32768`、`max_loop_round=8`、`strict_tools=True`。

`search_methodology` 可在 `METHODOLOGY_ENTRIES` 加一条占位，例如 id=`kb_macro_rates`，trigger 含 `lpr 利率 宏观`，text 写「宏观只提供环境标签，不构成个股买卖理由」。

---

## 单测最低要求（FakeLLM）

1. `apply_macro_rules`：白酒 / 空行业 → `low_relevance`；行业含「银行」→ `rate_sensitive`；无 LPR → `lpr_missing`  
2. FakeLLM 先 `get_macro_snapshot` 再 `submit_final`，得到 `role=macro` 的 Report  
3. `low_relevance` 路径：报告 `abstain=true`、`stance=abstain`  
4. 报告里写了 snapshot 没有的数字 → `bind_macro_report_evidence` 抛错  
5. 宏观 Agent 抛错时，workflow 仍能给出基本面决策（可用现有 `analyze_stock` + FakeLLM；至少单测 `_abstain_macro` 与 decision 缺宏观）

不要加 live DeepSeek 测试。

---

## 过程日志（与技术面同一套，编排写库）

`output` 必须带齐 `report` + `facts` + `clues` + `judgment_basis` + `trace_steps`。  
`save_report(run_id, "macro", report)`。ProcessRecord artifact + `process_logged` 由编排做；你保证 JSON 齐。  
不存 `reasoning_content`。

决策审计 `decision_written` 的 `report_refs` / `process_artifact_refs` 增加 `macro`。

---

## 明确不存 / 不做

- 不写追踪 Agent、不写 cron  
- 不把宏观 stance 算进买卖多数票  
- 不接 PMI、社融、汇率（字段没有）  
- 不改另外三个 Agent 的 Prompt 和工具  
- 不用经管 PDF 当运行时输入；KB 仍是短文本检索  

---

## 验收摘要

- `AgentInstance.name == "macro"`  
- 先 `get_macro_snapshot`，只读预计算切片  
- 白酒等低相关 → 弃权；银行等利率敏感且有 LPR → 可非弃权，但 stance 只能 hold，且写清 relevance  
- 宏观挂了不影响基本面成功  
- `Decision.dimensions_used` / `missing` 正确反映宏观；`rating` 不因宏观改方向  
- CLI 看得到宏观一行  
- FakeLLM 单测通过
