# 口径修复切片：ROE / 现金流归因 / RSI Wilder

> 状态：**已实现（按文档默认三项）**  
> 日期：2026-08-26  
> 位置：本仓库 `doc/`（`未命名/doc/`）  
> 起因：经管 `FUND规则更新说明-20260824.md`、`技术面规则-修改说明-20260824.md`；对照本仓库 live 000858（单期 ROE 6.5 被写成不合格、RSI SMA 假超卖）。  
> 范围：**只做经管意见里已验证会误伤、且改动能落地的三块。** 不把 FUND-01～17 打分表整包搬进 Agent。  
> 默认拍板：无 MA60 不算空头排列；`value_trap_risk` 强制 rule citation；FCF 本期不取。

---

## 0. 结论（先拍板）

本期三件事，按这个顺序做，可以分三个 PR，也可以一个 PR 分三节测试：

| # | 改什么 | 解决什么 | 不动什么 |
|---|---|---|---|
| A | `market.py::rsi` 改为 Wilder；技术规则/Prompt 锁「空头排列」定义 | 和通达信/同花顺对齐；禁止把「价在均线下方」写成空头 | MACD 算法、均线仍用 SMA |
| B | ROE 规则分层；单期不判 `roe_quality` 不合格 | 中报 ROE 6.5 不再被写成「质量门槛未过」 | `REQUIRED_FIELDS`、决策模板 |
| C | `cashflow_lag` 保留；有数据则拆季节性/含金量；便宜+非季节性滞后 → `value_trap_risk` | 白酒中报负现金流不一定是崩了；便宜不能压过质量 | 格雷厄姆 PE≤15 绝对档、F-Score、重述爬虫 |

**明确不做（本期）：**

- FUND-01/02 用 PE≤15、PB≤1.5 替换五年分位 `valuation_cheap` / `valuation_rich`  
- 17 条 `-1/0/+1/+2` 求和当基本面总分（专家仍交 1–5 分 Report）  
- FUND-10 内控意见、FUND-13 F-Score 九项、FUND-17 重述（CNINFO 全文）  
- 杜邦四字段、股息率（可留字段口子，不取数、不打 flag）  
- 用户意图/理解层、U2、看板 B2  
- 把 `基本面Agent规则FUND.md` 全文塞进 System Prompt  

经管文档里「规则代码已就位」指的是另一份工程路径；**本仓库 `未命名` 仍是旧 5 条规则 + SMA RSI**。本期在本仓库从零改，不拷 `bigAagent-main`。

---

## 1. 现状（实现层对照）

| 点 | 现在代码 | live 000858 表现 |
|---|---|---|
| ROE | `roe>=15 and debt_ratio<60` 才 `roe_quality` hit；否则 hit=false，模型写成「不合格」 | 中报 6.5 → 未过门槛 |
| 现金流 | `profit_yoy>0 and operating_cf<=0` → `cashflow_lag` | 触发；无季节性/含金量区分 |
| 估值 | 五年 PE 分位 ≤30 → `valuation_cheap` | 触发；与负 OCF 并存时靠模型 hold |
| RSI | `rsi()` 对最近 14 根涨跌幅做简单平均（SMA 口径） | 曾报 ~19.5 / ~28「超卖」；经管 Wilder 同日 ~37 |
| 空头排列 | `ma5<ma10<ma20` 且（无 ma60 或 `ma20<ma60`）才打 `ma_bear_align` | flag 可能对；**文案**仍写「空头排列」 |
| 快照 | `FundamentalSnapshot` extra=forbid，无 `roe_report_period` 等 | 缺字段就无法年化 |

证据层：`evidence.py::require_cashflow_lag_citation` 仍要求 flag 在时必须引用 `cashflow_lag`。**本期不删这条。**

---

## 2. 切片 A — 技术面口径

### 2.1 RSI：Wilder（必做）

改 `src/insightagent/market.py` 的 `rsi(values, window=14)`。

**算法（与经管文、通达信一致）：**

1. 需要至少 `window + 1` 根收盘价（现有提前返回条件可保留：`len(values) <= window` → `None`）。  
2. 先算相邻涨跌：`delta = close[i]-close[i-1]`；涨记 gain，跌记 loss（绝对值）。  
3. **首个平均**：前 `window` 个 gain/loss 的简单平均，得到 `avg_gain`、`avg_loss`。  
4. **之后 Wilder：**  
   `avg_gain = (avg_gain * (n-1) + current_gain) / n`  
   等价于 `ewm(alpha=1/n, adjust=False)`。  
5. `RS = avg_gain/avg_loss`（loss=0 则 RSI=100）；`RSI = 100 - 100/(1+RS)`。  
6. 返回 **序列最后一根** 的 RSI。

禁止：只用最近 14 根再平均一次（这就是现在的假超卖）。

`compute_indicators` 仍把结果写入 `rsi14`。可在 `IndicatorSnapshot` **不新增字段**（避免大面积合同）；在 `source` 或 note 里现有结构若没有算法字段，则：

- `apply_technical_rules` 的返回 dict 增加 `rsi_method: "wilder"`（给工具 JSON 看）。  
- 或在 `get_indicator_snapshot` 的 payload 里加只读键 `rsi_smoothing: "wilder"`。  

**推荐：** 工具返回的 indicator JSON 增加 `rsi_smoothing: "wilder"`，`IndicatorSnapshot` 若 extra=forbid 则 **加该可选字段**，默认 `"wilder"`。fixture 技术快照不测 RSI 数值的保持原样。

超买/超卖阈值不变：≥70 / ≤30。Wilder 后 000858 中性则 **不应** 再打 `rsi_oversold`。

### 2.2 均线排列：flag 已接近，锁文案

保持 `technicals.py`：

- 多头：`ma5>ma10>ma20` 且（`ma60 is None` 或 `ma20>ma60`）→ `ma_bull_align`  
- 空头：`ma5<ma10<ma20` 且（`ma60 is None` 或 `ma20<ma60`）→ `ma_bear_align`  

**收紧（建议本期做）：** `ma60` **有值时必须** `ma20>ma60` / `ma20<ma60` 才打排列 flag；`ma60 is None` 只打「短中期顺向」，**不要**叫多头/空头排列。实现：

- 有 ma60 且四线严格：`ma_bull_align` / `ma_bear_align`（与经管 MA5…MA60 一致）  
- 仅三线顺向、或价在均线下方但 MA20>MA60：新 flag `ma_below_all` 或只用文案、不打 bear  

为少加 flag，可用：

- `ma_bear_align` **仅当** 四值齐全且 `ma5<ma10<ma20<ma60`  
- 价低于 ma5/10/20/60 但非严格空头：不打 `ma_bear_align`；`trend` 文案改为「价格位于均线下方、均线系统走弱」（改 `_describe_trend`）

`_describe_trend`：

- 有 `ma_bear_align` → 「均线空头排列（MA5<MA10<MA20<MA60）」  
- 无该 flag、但现价低于 ma20 → 「价格位于均线下方、均线系统走弱」  
- 禁止单独「空头排列」四字打在非严格结构上  

### 2.3 技术面 Agent Prompt

`technical_agent.py` 增加硬规则（短，不贴全书）：

- 指标数值以工具为准，禁止自己用收盘价重算 RSI。  
- 仅当 `computed_flags` 含 `rsi_oversold` / `rsi_overbought` 才可写超卖/超买；**禁止**只凭 rsi14 数字自行改阈值。  
- 仅当 `ma_bear_align` 才可写「空头排列」；否则用工具 `trend` 原文。  
- 继续：只在 rsi14≥70 时引 `kb_rsi_overbought`（已有）。补：只在 `rsi_oversold` 时引超卖类 kb（若还没有超卖条目，就不引）。

### 2.4 测试（A）

- **单元：** 用经管表 2026-08-21 五粮液前复权收盘序列（测试夹具自备一小段 closes，或从现有 kline fixture 截）。断言 Wilder RSI 与 37.1 **误差 < 0.5**；旧 SMA 函数不再被 `compute_indicators` 调用。  
- **规则：** MA20>MA60 且价在均线下方 → 无 `ma_bear_align`，trend 含「走弱」不含「空头排列」。四线严格空头 → 有 `ma_bear_align`。  
- **回归：** 现有 `test_market_tools` / 技术夹具不因 RSI 公式微变而整片失败；若断言了旧 19.52，改为 Wilder 或改为「存在 rsi14」。

---

## 3. 切片 B — ROE 长期口径

### 3.1 快照新字段（`FundamentalSnapshot`，Optional，默认空，extra=forbid 故必须写进模型）

| 字段 | 类型 | 默认 | 用途 |
|---|---|---|---|
| `roe_series` | `List[float]` | `[]` | 最近若干年 **年度** ROE，最新在前 |
| `roe_report_period` | `Optional[str]` | `None` | `q1` / `h1` / `q3` / `fy` / 未知则 `None` |

**不加**本期用不到的 12 个财务字段（FCF 等放到切片 C 仅 3 个，见下）。避免一次改 14 个却取不到数。

`REQUIRED_FIELDS` **不变**。新字段缺失 **不 abstain**。

### 3.2 `_annualize_roe(roe, period) -> Optional[float]`

| `roe_report_period` | 年化 |
|---|---|
| `q1` | ×4 |
| `h1` | ×2 |
| `q3` | ×4/3 |
| `fy` | ×1 |
| `None` 或无法识别 | **不年化、不拿去比 15** |

单期年化只作 detail 里的参考数字，**不作为 `roe_quality` hit=true 的充分条件**（与经管 0.2 第③层一致：年化后仍不能判长期合格/不合格）。

### 3.3 `roe_quality` 判定（flag 名不变）

按层，**先命中先停**：

1. `len(roe_series) >= 3`：`mean(series)>=15` 且 `min(series)>=12` 且 `debt_ratio<60` → `hit=true`，detail 写多年口径。不满足 → `hit=false`，detail 写多年未达，**可以**视为质量未达（有年报序列才允许「不合格」语义）。  
2. 否则 `roe_stable is True`：`roe>=12` 且 `debt_ratio<60` → `hit=true`（保住现 fixture：000858 fixture `roe=16.8`、`roe_stable=true`）。不满足 → `hit=false`，detail 写稳定标志下未达。  
3. 否则仅单期 `roe`：  
   - **`hit=false` 一律**（既不因 6.5 也不因年化后 ≥15 而 hit=true）  
   - detail 必须含固定英文片段 `single-period is NOT enough to judge long-term ROE quality`  
   - 年化值得了就写在 detail 里：`annualized_roe=… period=h1`  
   - **新 flag** `roe_insufficient_history` 写入 `computed_flags`，供 Prompt 识别  

`hit=false` + `roe_insufficient_history`：**禁止**专家写「ROE 不合格 / 未过 15 门槛」。  
`hit=false` 且无该 flag：可以写未达长期标准。

负债率缺失时：多年/稳定层也不得 `hit=true`（与现在一致需要 debt_ratio）。

### 3.4 取数（live 才有意义；fixture 可手填）

`market.py` 组基本面时：

- **`roe_report_period`：** 从现有财务接口「报告期」日期解析：`03-31→q1`，`06-30→h1`，`09-30→q3`，`12-31→fy`。解析失败保持 `None`（则走第 3 层且不年化）。  
- **`roe_series`：** 能从 `stock_financial_analysis_indicator` 安全取到 **年报** ROE 则填最近 3～5 年；取不到就 `[]`，规则自动降级。失败不得把中报 ROE 三次填进 series。  

夹具：`000858_fundamental.json` 保持 `roe_stable=true`、`roe=16.8`，**不**加 series，现有 `roe_quality` 断言仍过。  
另加单元用的内存 snapshot：`roe=6.5`、`roe_stable=None`、`roe_report_period="h1"`、`roe_series=[]` → 无 `roe_quality`，有 `roe_insufficient_history`，detail 含 `NOT enough`。

### 3.5 基本面 Prompt

`fundamental_agent.py` 增加：

- `roe_insufficient_history` 时：只写「单期/报告期 ROE，不足以判断长期盈利质量」，**禁止**「不合格」「未过 15」。  
- `roe_quality` 未出现在 flags 且无 insufficient：**不要**把「没打 flag」理解成不合格；看 `rule_hits` 的 detail。  
- 仍禁止自己年化；年化数字只抄 detail 里已有的 `annualized_roe`。

### 3.6 测试（B）

- 单期 h1、6.5：flag 含 `roe_insufficient_history`，不含 `roe_quality`；detail 含 `NOT enough`。  
- fixture 000858：仍可有 `roe_quality`（stable 层）。  
- `REQUIRED_FIELDS` 缺 roe 仍 abstain 逻辑不变。  
- 三年 series 均值 16、最低 13、负债 40 → `roe_quality`。  
- 三年均值 16、最低 10 → 无 `roe_quality`，无 `roe_insufficient_history`。

---

## 4. 切片 C — 现金流归因与价值陷阱

### 4.1 再加字段（仍 Optional）

| 字段 | 含义 |
|---|---|
| `cashflow_yoy` | 经营现金流同比 %（与 `profit_yoy` 同报告期口径） |
| `ocf_to_np` | 同报告期 `operating_cf / net_profit`（净利≤0 则不计算，保持 None） |

本期 **不取** `fcf`（用户偏好对照 FCF 可在 missing_information 继续写「快照无 FCF」）。取 FCF 单列下一期，避免 capex 口径再错一档。

### 4.2 flags（`cashflow_lag` 触发条件不变）

基础仍：`profit_yoy>0 and operating_cf<=0` → `cashflow_lag` + 原 rule_hit。

其后仅当 `cashflow_lag` 为真：

| 条件 | flag |
|---|---|
| `cashflow_yoy is not None` 且 `> 0` | `cashflow_seasonal` |
| `ocf_to_np is not None` 且 `< 0.5` | `cashflow_quality_issue` |
| 两字段都无 | 只保留 `cashflow_lag`，detail 加 `undetermined`（需年报/同比确认） |

两者可同时存在（同比改善但仍含金量差）——允许两 flag 都打，Prompt 写「季节性可能存在，但含金量仍弱」。

**价值陷阱：**

```
valuation_cheap 且 cashflow_lag 且 没有 cashflow_seasonal
→ value_trap_risk
```

有季节性则 **不** 打 `value_trap_risk`（经管：预收款/年中为负可以是常态）。

`evidence.py`：`cashflow_lag` 引用要求不变。可新增：若 `value_trap_risk` 在 flags，非弃权报告应引用该 rule id（与 cashflow_lag 类似）。没有该 citation 则绑定失败——**建议做**，否则模型会继续只喊便宜。

### 4.3 取数

- `ocf_to_np`：已有 `operating_cf`、`net_profit` 且净利>0 时 **程序直接除**，不必新接口。注意单位一致（现在都有 scale 到亿元的逻辑，必须同口径相除）。  
- `cashflow_yoy`：`cashflow()` 拉相邻两期经营现金流再算同比；失败则 None。  

### 4.4 Prompt 裁决（三条，对应经管 0.1）

写进基本面 System，短句：

1. `value_trap_risk`：便宜不能压过质量，**stance 不得 buy**，最多 hold。  
2. `cashflow_seasonal`：不得把 `cashflow_lag` 写成「盈利质量崩塌」。  
3. 仅 `cashflow_lag` 无归因：风险里保留「现金流转正」证伪，不升格为崩盘。

### 4.5 测试（C）

- 便宜 + lag + 无 yoy → `value_trap_risk`。  
- 便宜 + lag + `cashflow_yoy>0` → 有 seasonal，**无** value_trap。  
- lag + `ocf_to_np=0.2` → `cashflow_quality_issue`。  
- 无 lag → 不出现上述新 flag。  
- 绑定：flags 含 value_trap 时缺 citation 应失败（若你同意 4.2 证据门）。

---

## 5. 方法论短条目（少量，不 RAG）

`METHODOLOGY_ENTRIES` 本期只加/改这些，仍 keyword + scope：

| id | 何时允许引用 |
|---|---|
| `kb_roe_quality` 改 text | 强调多年年度；单期不够判 |
| `kb_cashflow_lag` 改 text | 先归因再定性 |
| `kb_value_trap` **新增** | 仅 flags 含 `value_trap_risk` |
| `kb_rsi_wilder` **不必新 id** | 改技术 kb 或 Prompt 即可；避免空搜乱引 |

检索仍最多 3 条、空 query 不返回（若 K0 已做）；未做 K0 则至少 **Prompt 禁止无 flag 引新 kb**。

---

## 6. 文件清单

| 文件 | A | B | C |
|---|---|---|---|
| `market.py` | `rsi`；indicator 可选 smoothing | 解析报告期、可选年报 ROE 序列、现金流同比 | `ocf_to_np` 计算 |
| `technicals.py` | 排列 flag 收紧；trend 文案 |  |  |
| `technical_agent.py` | Prompt |  |  |
| `business_contracts.py` | 可选 rsi 字段或放 data_contracts | `roe_series`、`roe_report_period` | `cashflow_yoy`、`ocf_to_np` |
| `fundamentals.py` |  | ROE 三层 | lag 归因 + trap |
| `fundamental_agent.py` |  | Prompt | 裁决三条 |
| `data_contracts.py` | Indicator 可选 |  |  |
| `evidence.py` |  |  | 可选 trap citation |
| `akshare_map.py` | 仅当报告期列名需要 | 可能 | 可能 |
| fixtures / tests | RSI 夹具、排列 | ROE 单期 | trap / seasonal |
| `技术面规则.md` | 已改过，代码对齐即可 |  |  |
| `基本面Agent规则FUND.md` |  | 实现 0.2 | 实现 0.1/0.3；**不**实现 01 绝对 PE 替换分位 |

不改：`decision.py` 评分公式、用户意图、runtime。

---

## 7. 兼容与发布检查

- 空新字段：规则跳过，行为接近今天，但 **ROE 单期不再 hit=true**（今天 6.5 本就 hit=false）。变化是 **detail + 新 flag + Prompt**，让模型别说「不合格」。  
- fixture `roe_stable=true` 的 000858 **仍可** `roe_quality`。  
- live 000858：期望 `roe_insufficient_history`，报告不再写「ROE 6.5 低于 15 不合格」；RSI 若 Wilder≥30 则无超卖 flag、禁止超卖话术；若仍 cheap+lag 且无 yoy，可出现 `value_trap_risk`、stance≠buy。  
- `cashflow_lag` 证据门保持。  

---

## 8. 验收（你审完实现后用）

1. 单元：Wilder RSI 贴近 37.1（经管 8/21 样本或等价夹具）。  
2. 单元：中报 ROE 6.5 → NOT enough，无「质量不合格」依赖的 flag。  
3. 单元：cheap+lag 无季节性 → `value_trap_risk`；有 `cashflow_yoy>0` → 无 trap。  
4. 夹具 000858 旧断言（stable ROE + cashflow_lag）仍过。  
5. 一次 live `analyze 000858`（可 `--prompt none`）：基本面不写单期 ROE 不合格；技术面不把非严格四线叫空头；RSI 超卖仅当 flag 存在。  

---

## 9. 请你拍板的三问

1. `ma60` 缺失时：打不打「空头排列」？本文建议 **不打** `ma_bear_align`。  
2. `value_trap_risk` 是否做 **强制 citation**（没有则绑定失败）？本文建议做。  
3. FCF 是否坚持下一期再取？本文建议本期只在 missing 里声明无字段。

通过这三问后按 A→B→C 改代码。
