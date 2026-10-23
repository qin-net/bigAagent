# 首次分析加入「综合分析」——initial synthesis 功能说明

日期：2026-10-23

## 一句话总结

首次分析（analyze）跑完 4 个专家之后，现在会让**追踪 agent 以一种新的受限任务模式**把 4 份专家报告和规则决策读一遍，写出一段真正的"综合分析"叙事（跨维度推理、给每个专家打分评价），并且这个过程留下的记忆会**自动成为后续追踪的起点**。

## 为什么要做

原来首次分析的流程是：4 个专家（基本面/技术面/情绪面/宏观）并行跑 → 一段**纯规则代码**（`decision.py` 的 `build_multi_factor_decision`）合成决策。规则合成有它的好处（可审计、可复现、证据链不断、省钱），但缺点也明显：

- rationale 是模板拼接的流水账："基本面：看多，score=4。技术面：……"
- 没有跨维度推理（比如"技术面看多但宏观偏紧，技术信号要打折"这种判断规则写不出来）
- 专家之间有分歧时只会"列出来"，不会"裁决"

而追踪（track）流程早就有一个主 agent 在干这件事。所以这次的思路不是新造一个"主 agent"，而是**复用追踪 agent**——额外的好处是上下文衔接是白送的（见下文"亮点"）。

## 做了什么

| 改动点 | 文件 | 说明 |
| --- | --- | --- |
| 新输出模型 `InitialSynthesis` | `business_contracts.py` | thinking + synthesis + expert_evaluations，复用现成的 `ExpertEvaluation` |
| 新任务模式 initial_synthesis | `tracking_agent.py` | `synthesize_initial()` 等一组函数，照 distill 任务变体的现成结构写 |
| 接入首次分析 | `workflows/initial_research.py` | 决策建好后、落库前跑合成；新参数 `synthesis_llm_adapter` |
| CLI 展示 | 同上 `format_cli_text` | 输出多两行：`综合分析:` 和 `专家评估: fundamental:accept,...` |
| 生产调用方 | `insightboard/research.py`、`__main__.py` | 两处都传入了合成 adapter |
| 看板 API | `insightboard/research.py` | research run 的投影新增独立 `synthesis` 键 |
| 看板前端 | `web/app.js` | 研究页「综合建议」卡下新增「综合分析」卡 + 专家评价卡 + 可折叠「查看判断过程」 |
| 测试 | `tests/test_initial_synthesis.py` | 6 个测试，全链路覆盖 |

## 怎么做到的

### 1. 职责切分：LLM 只写叙事，不碰决策

rating / confidence / citations **仍然由规则产出**，一个字都不交给 LLM。合成 agent 的系统提示词里明确写了 "the rating is already decided and is not yours to change"。它只负责：

- 写综合叙事（synthesis）
- 裁决维度间分歧
- 给每个出了报告的专家打评价（expert_evaluations：accept / discount / reject / insufficient）

这样可审计性和证据链都保住了，LLM 挂了也只是少一段话。

### 2. 复用追踪 agent 的"任务变体"机制

追踪 agent 本来就支持多任务模式——系统提示词里写着 "This round's Task JSON names the job. Use only the tools registered this round"，已有的 distill 任务就是先例。initial_synthesis 照抄这个结构：

- **工具只注册两个只读的**：`get_expert_reports`（全量报告，含 citations/risks/falsifiers，不走追踪上下文那种 200 字截断）和 `get_decision`
- **不注册任何分析师调用工具**（call_fundamental 等）——这是结构性保证，不是靠提示词求它别调，它**根本没有**那些工具

### 3. 记忆链：这是整个设计里最巧的部分

追踪流程取历史记忆的方式是：

```
prior_session_memory(agent_name="tracking", thesis_id="000858-initial")
```

这个查询**不区分任务类型**，只看 agent 名 + thesis_id + 成功状态。所以只要合成 session 用 `agent_name="tracking"` + 同一个 thesis_id 落库，**后续第一次真正的追踪会自动把它认作父 session、继承它的 private_memory——追踪侧一行代码都没改**。

为此合成任务的指令里明确要求模型在 submit_final 的 state_patch 里写 `private_memory.memory_summary`（以及 active_hypotheses / falsifiers_watched 等），这就是给追踪链播的种子。效果：追踪 agent 从第一天就"认识"这只股票，而不是从一张冷的基线报告开始。

## 注意了哪些 case

1. **LLM 失败不毁 run**。合成整个包在 try/except 里：失败只记一条 `synthesis_failed` 审计事件，run 状态不变，模板 rationale 兜底。这和项目里"每个维度独立降级"的哲学一致。
2. **四维全弃权时不跑合成**。严重缺数据的早退分支和全弃权的 run 没东西可叙述，白花一次调用。
3. **功能是 opt-in 的**。`synthesis_llm_adapter=None`（默认）= 完全跳过。注意这个语义和其他 `*_llm_adapter` 参数**不一样**（那些 None=回落到主 adapter），docstring 里写明了。这么设计是因为既有测试的 `ScriptedAgentLLM` 是相位机，容不下一次额外的 LLM 调用——opt-in 让全部存量测试免改。
4. **session 键名避开 "tracking"**。既有测试断言 `"tracking" not in run.session_ids`（它的本意是"analyze 不应该启动追踪"），所以合成 session 存在 `run.session_ids["synthesis"]`。落库的 agent 名仍是 "tracking"（记忆链需要），两者不冲突。
5. **前端路由键避开 "tracking"**。前端 `loadRun` 按 `data.tracking` 判断走追踪渲染器，research run 如果塞 `tracking` 键会被误渲染成追踪页，所以 API 投影用了独立的 `synthesis` 键。
6. **timeline 混入 research 行的安全性**。合成结果存进 `tracking_timeline` 表（见"亮点"），事先逐一验证了追踪侧的消费方：prescreen 的冷却判断 `_cooldown_hit` 和 `recent_timeline_refs` 都是防御性 `.get()` 读取，research 行没有 `suggested_agent`/`triggers`，对它们是惰性的，不会误触发冷却。
7. **输出永不校验炸**。`finalize_initial_synthesis` 照抄追踪的 `finalize_deliverable` 模式：坏的专家评价逐条 try/except 跳过；出了报告但模型漏评的专家自动补一条 `insufficient`；synthesis 为空回填 thinking。
8. **坏合成不污染记忆链**。如果合成 agent 跑挂了，session 不会以 SUCCESS 落库，`latest_for_agent` 查不到它，追踪不会继承垃圾记忆。

## 亮点

- **零成本的上下文衔接**：没有为"把首评记忆传给追踪"写任何胶水代码，纯靠对既有记忆查询机制（按 agent 名 + thesis_id 查）的理解，让合成 session "长"在正确的位置上。
- **结构性约束优于提示词约束**：不许调分析师不是靠 prompt 恳求，而是不给工具。
- **复用度高**：输出模型复用 `ExpertEvaluation`，任务模式照抄 distill 先例，存储复用 timeline 表，前端复用 eval-card 样式。真正的新代码不多。
- **概念上自洽**：首次合成存成 thesis timeline 的第一条记录——"初评"本来就应该是时间线的起点，后面每次追踪往后排。

## 遇到的困难

**最大的一个：伪装 LLM 的测试桩被 runtime 的严格校验拒了，而且报错信息有误导性。**

新测试里用 `FakeLLMAdapter` 排队返回"工具调用 → 工具调用 → 最终提交"。最初照专家测试的写法，state_patch 用了字典形制：

```json
{"set": {"private_memory.memory_summary": "..."}, "append": {}, "remove": {}}
```

结果合成一直走降级路径，审计里只有一句 `FakeLLMAdapter has no queued response`（队列被多消耗了一次——因为 runtime 把被拒的 final 退回去重试了）。翻到被拒的那条工具消息才看到真正的报错：

> submit_final.output.report is required and must include role, score, and stance

这句话**本身就是误导的**——它是给默认输出模型准备的文案，我们的自定义输出模型根本没有 report 字段。真正的原因在 `runtime.py` 的 `SubmitStatePatch`：**submit_final 走工具调用路径时，state_patch 必须用 DeepSeek-strict 的列表形制**：

```json
{"set": [{"path": "private_memory.memory_summary", "value": "..."}], "append": [], "remove": []}
```

专家测试之所以字典形制能过，是因为专家的 final 走的是另一条宽容的回退校验路径（content JSON + 默认输出模型），自定义输出模型没有这条回退。线上不受影响——真实模型拿到的 schema 本来就是列表形制。

**次要的：决定在哪一步跑合成。** 合成需要读 4 份报告，如果走数据库读就依赖"报告先落库"，会把合成挤到 `_complete` 之后、引入次序耦合。最后选择把内存里的 Report 对象直接传给合成函数，完全不依赖持久化次序。

## 可能遭遇什么 bug（风险清单）

1. **真实模型不写 state_patch → 记忆链断**。指令里要求了写 private_memory，但模型可以不听。后果不是崩溃，只是追踪拿不到首评记忆（退回旧行为）。如果线上发现 `prior_memory` 经常为空，优先查这里。
2. **strict schema 下 InitialSynthesis 的提交失败率**。strict 模式下模型输出必须严丝合缝。runtime 有重试机制，重试仍失败就走降级。可以观察 `synthesis_failed` 审计事件的频率。
3. **timeline 查询的隐含假设**。`timeline_for_run` 按 run_id 取"最新一条"。目前一个 research run 只会写一条合成行，但如果以后有人给同一 run_id 写了第二条 timeline，看板展示的会是后写的那条。
4. **成本**。每次 analyze 多一次 LLM 调用（flash 档、多轮工具循环）。量大时注意 token 消耗。
5. **feedback/rerun 路径没有接合成**。目前只有全新 analyze 跑合成；`feedback_on_run` 触发的修订 run 不会生成新的综合分析，看板上修订版 run 的「综合分析」区块会是空的。这是已知的未覆盖场景，不是 bug，后续可以按需补。
6. **记忆继承"最新的赢"**。`latest_for_agent` 按时间取最新成功 session。合成和追踪共用 "tracking" 这个 agent 名，意味着追踪跑过之后，下一次追踪继承的是追踪自己的记忆而非首评记忆——这正是想要的行为，但如果有人 analyze 两次同一只股票，第二次的合成会继承第一次合成的记忆（也合理，但要知道有这回事）。

## 怎么验证

```bash
# 新功能 + 相邻回归
.venv/bin/python -m pytest tests/test_initial_synthesis.py tests/test_p0_research.py \
  tests/test_tracking_agent.py tests/test_expert_memory.py tests/test_insightboard.py -q

# 全量（live 测试无 key 自动跳过）
.venv/bin/python -m pytest -q
```

当前状态：全量 112 passed, 3 skipped。

手工验证：

```bash
# CLI（需要 DEEPSEEK_API_KEY），输出会多「综合分析:」「专家评估:」两行
python -m insightagent analyze 000858 --fixture

# 看板：跑一次 analyze，研究页「综合建议」卡下方出现「综合分析」卡
python -m insightboard serve
```
