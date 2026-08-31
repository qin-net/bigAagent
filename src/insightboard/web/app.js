let page = 1, total = 0, currentCode = null, currentJobId = null, currentRunId = null, currentTrackJobId = null, currentTrackRunId = null, profileStock = '', currentBars = [], chartMode = 'line', currentResearchData = null, currentTrackData = null;
const size = 50;
const text = (value) => value === null || value === undefined ? '-' : value;
const safe = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const money = (value) => value === null || value === undefined ? '-' : `${(value / 100000000).toFixed(2)} 亿`;
const yuan = (value) => value === null || value === undefined ? '-' : `${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} 元`;
const api = (path, options) => fetch(`/api/v1${path}`, options).then(async (response) => { const data = await response.json(); if (!response.ok) throw new Error(data.detail || '请求失败'); return data; });
const ZH = {
  stance: { buy: '买入', hold: '持有', sell: '卖出', abstain: '暂不判断' },
  rating: { buy: '买入', hold: '持有', sell: '卖出', abstain: '暂不判断' },
  effect: { this_run: '仅影响本轮', remember: '已记住', rerun: '按反馈重跑', remember_rerun: '记住并重跑' },
  track: { unchanged: '维持原判断', review: '需要复核', invalidate: '原判断失效' },
  verdict: { accept: '采纳', discount: '打折采用', reject: '不采用', insufficient: '证据不足' },
  reliability: { high: '高', medium: '中', low: '低', unusable: '不可用' },
  urgency: { low: '不急', medium: '适中', high: '尽快再看' },
  role: { fundamental: '基本面', technical: '技术面', sentiment: '情绪面', macro: '宏观', tracking: '追踪', decision: '综合决策' },
  status: { success: '成功', failed: '失败', queued: '排队中', running: '进行中', unchanged: '未改结论', completed: '已完成', abstained: '暂不判断', degraded: '降级完成' },
};
const zh = (kind, value) => {
  if (value == null || value === '' || value === 'none' || value === '-') return '';
  const table = ZH[kind] || {};
  const key = String(value);
  return table[key] || table[key.toLowerCase()] || '';
};
const zhTime = (value) => {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
};
const toneClass = (value) => {
  const key = String(value || '').toLowerCase();
  if (['buy', 'invalidate', 'reject'].includes(key)) return 'tone-strong';
  if (['sell', 'unchanged', 'accept'].includes(key)) return 'tone-calm';
  if (['hold', 'review', 'discount', 'abstain'].includes(key)) return 'tone-warn';
  return '';
};
const bullets = (items) => (items || []).filter(Boolean).map((item) => `<li>${safe(item)}</li>`).join('');
const chips = (items) => (items || []).filter(Boolean).map((item) => `<span class="result-chip">${safe(item)}</span>`).join('');
function view(name) {
  ['market', 'detail', 'watchlist', 'paper', 'profile', 'experts'].forEach((item) => {
    document.querySelector(`#${item}-view`).hidden = item !== name;
  });
  document.querySelectorAll('nav button').forEach((button) => {
    button.classList.toggle('active', button.dataset.view === name);
  });
}

function setStage(stage, rail, { unlocked = true, state = '', label = '' } = {}) {
  const section = document.querySelector(`#stage-${stage}`);
  const node = document.querySelector(`#rail-${rail || stage}`);
  const badge = document.querySelector(`#stage-${stage}-state`);
  section?.classList.toggle('is-locked', !unlocked);
  node?.classList.toggle('is-complete', state === 'complete');
  node?.classList.toggle('is-current', state === 'current');
  if (badge && label) {
    badge.textContent = label;
    badge.className = `stage-state${state === 'complete' ? ' is-complete' : state === 'current' ? ' is-active' : ''}`;
  }
}

function renderInvestmentState(paper, code, research) {
  const ready = Boolean(research);
  const position = (paper?.positions || []).find((item) => item.stock_code === code);
  const box = document.querySelector('#investment-summary');
  const buy = document.querySelector('#paper-buy');
  const sell = document.querySelector('#paper-sell');
  const quantity = document.querySelector('#paper-qty');
  const reason = document.querySelector('#paper-reason');
  [buy, sell, quantity, reason].forEach((element) => { if (element) element.disabled = !ready; });
  if (!ready) {
    box.innerHTML = '<p class="empty-state">先完成首次分析，再决定是否投入。</p>';
    setStage('invest', 'invest', { unlocked: false, label: '等待分析' });
    return;
  }
  if (!position) {
    box.innerHTML = `<dl><dt>分析基线</dt><dd>已完成</dd><dt>当前选择</dt><dd>暂未投入</dd><dt>可用虚拟资金</dt><dd>${yuan(paper?.cash)}</dd></dl><p class="notice investment-hint">可以投入，也可以保持自选观察。</p>`;
    setStage('invest', 'invest', { state: 'current', label: '待用户决策' });
    return;
  }
  box.innerHTML = `<dl><dt>当前持仓</dt><dd>${position.quantity} 股</dd><dt>平均成本</dt><dd>${yuan(position.avg_cost)}</dd><dt>当前市值</dt><dd>${yuan(position.market_value)}</dd><dt>持仓浮动</dt><dd class="${position.unrealized >= 0 ? 'up' : 'down'}">${yuan(position.unrealized)}</dd></dl>`;
  setStage('invest', 'invest', { state: 'complete', label: '已投入' });
}

function renderLearningState(research, track) {
  const box = document.querySelector('#learning-summary');
  const memories = Object.entries(research?.memories || {});
  const preferences = research?.preferences || [];
  const trackMemory = track?.memories?.tracking;
  if (!memories.length && !preferences.length && !trackMemory) {
    box.innerHTML = '<p class="empty-state">产生分析、反馈或追踪后，这里会汇总本标的的记忆沉淀。</p>';
    return;
  }
  const names = { fundamental: '基本面', technical: '技术面', sentiment: '情绪', macro: '宏观', tracking: '追踪' };
  const pills = [
    ...memories.filter(([, item]) => item?.memory_summary).map(([name]) => `${names[name] || name}记忆`),
    ...preferences.map((item) => `${names[item.scope] || item.scope}口径`),
    ...(trackMemory?.memory_summary && !memories.some(([name]) => name === 'tracking') ? ['追踪记忆'] : []),
  ];
  box.innerHTML = `<p><b>本标的已沉淀</b>：${memories.length} 份专家记忆，${preferences.length} 条用户口径${track ? '，并已完成追踪复核' : ''}。</p><p class="notice">后续分析会继续携带有效私有记忆。<button class="button-quiet" id="open-experts-from-learn">打开专家工作台</button></p><div class="learning-pills">${pills.map((item) => `<span class="learning-pill">${item}</span>`).join('')}</div>`;
  box.querySelector('#open-experts-from-learn').onclick = loadExperts;
}

function applyWorkflowState(research, track, paper) {
  currentResearchData = research || null;
  currentTrackData = track || null;
  const hasResearch = Boolean(research);
  const hasTrack = Boolean(track);
  setStage('research', 'research', { state: hasResearch ? 'complete' : 'current', label: hasResearch ? '分析完成' : '待分析' });
  setStage('track', 'track', { unlocked: hasResearch, state: hasTrack ? 'complete' : hasResearch ? 'current' : '', label: hasTrack ? '已追踪' : hasResearch ? '可追踪' : '等待基线' });
  document.querySelector('#track-start').disabled = !hasResearch;
  document.querySelector('#track-prompt').disabled = !hasResearch;
  document.querySelector('#track-feedback-send').disabled = !hasTrack;
  document.querySelector('#track-feedback-prompt').disabled = !hasTrack;
  document.querySelector('#feedback-send').disabled = !hasResearch;
  document.querySelector('#feedback-prompt').disabled = !hasResearch;
  renderInvestmentState(paper, currentCode, research);
  renderLearningState(research, track);
  document.querySelector('#rail-learn')?.classList.add('is-current');
}
async function load() { const q = document.querySelector('#search').value.trim(); const params = new URLSearchParams({ page, size, q, sort: 'turnover', order: 'desc' }); const data = await (await fetch(`/api/v1/quotes?${params}`)).json(); total = data.total; document.querySelector('#quotes').innerHTML = data.items.map((item) => `<tr data-code="${item.stock_code}"><td>${item.stock_code}</td><td>${item.name}</td><td>${text(item.market)}</td><td>${text(item.price)}</td><td class="${item.change_pct >= 0 ? 'up' : 'down'}">${text(item.change_pct)}%</td><td>${money(item.turnover)}</td></tr>`).join('') || '<tr><td colspan="6">暂无行情。点击「采集行情」拉取延迟数据。</td></tr>'; document.querySelectorAll('#quotes tr[data-code]').forEach((row) => row.onclick = () => detail(row.dataset.code)); document.querySelector('#page').textContent = `第 ${page} 页，共 ${total} 条`; document.querySelector('#previous').disabled = page === 1; document.querySelector('#next').disabled = page * size >= total; }
function chartScale(values) {
  const numbers = values.filter((value) => value !== null && value !== undefined);
  if (!numbers.length) return null;
  const min = Math.min(...numbers), max = Math.max(...numbers);
  return {
    min, max,
    y: (value) => canvasHeight() - 20 - ((value - min) / (max - min || 1)) * (canvasHeight() - 40),
  };
}
function canvasHeight() { return document.querySelector('#chart').height; }
function drawChart() {
  const canvas = document.querySelector('#chart');
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const bars = currentBars || [];
  const status = document.querySelector('#chart-status');
  document.querySelector('#chart-mode-line').className = chartMode === 'line' ? '' : 'button-quiet';
  document.querySelector('#chart-mode-k').className = chartMode === 'k' ? '' : 'button-quiet';
  if (!bars.length) {
    if (status) status.textContent = '暂无日线。点击「拉取日线」后显示收盘折线。';
    return;
  }
  if (status) status.textContent = `${chartMode === 'k' ? '日K' : '收盘折线'} · ${bars.length} 根`;
  if (chartMode === 'k') {
    const scale = chartScale(bars.flatMap((bar) => [bar.high, bar.low]));
    if (!scale) return;
    const width = canvas.width / bars.length;
    bars.forEach((bar, index) => {
      const up = bar.close >= bar.open;
      ctx.strokeStyle = up ? '#fb7185' : '#34d399';
      ctx.fillStyle = ctx.strokeStyle;
      const x = index * width + width / 2;
      ctx.beginPath();
      ctx.moveTo(x, scale.y(bar.high));
      ctx.lineTo(x, scale.y(bar.low));
      ctx.stroke();
      const top = scale.y(Math.max(bar.open, bar.close));
      ctx.fillRect(x - Math.max(1, width * .3), top, Math.max(2, width * .6), Math.max(1, Math.abs(scale.y(bar.open) - scale.y(bar.close))));
    });
    return;
  }
  const closes = bars.map((bar) => bar.close).filter((value) => value !== null && value !== undefined);
  const scale = chartScale(closes);
  if (!scale) return;
  const width = canvas.width / Math.max(closes.length - 1, 1);
  ctx.strokeStyle = '#38bdf8';
  ctx.lineWidth = 2;
  ctx.beginPath();
  closes.forEach((value, index) => {
    const x = closes.length === 1 ? canvas.width / 2 : index * width;
    const y = scale.y(value);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}
async function collectBars() {
  if (!currentCode) return;
  const status = document.querySelector('#chart-status');
  status.textContent = '正在拉取日线...';
  try {
    await api(`/stocks/${currentCode}/bars/collect`, { method: 'POST' });
  } catch (error) {
    if (!String(error.message).includes('already')) {
      status.textContent = error.message;
      return;
    }
  }
  const poll = async (attempt) => {
    if (currentCode == null) return;
    const data = await api(`/bars/${currentCode}`);
    if (data.items.length) {
      currentBars = data.items;
      drawChart();
      const notices = await api(`/notices/${currentCode}`).catch(() => ({ items: [] }));
      document.querySelector('#notices').innerHTML = notices.items.map((notice) => `<li>${text(notice.published_at)} ${notice.url ? `<a href="${notice.url}" target="_blank">${notice.title}</a>` : notice.title}</li>`).join('') || document.querySelector('#notices').innerHTML;
      return;
    }
    if (attempt >= 40) {
      status.textContent = '日线仍为空，请稍后重试「拉取日线」。';
      return;
    }
    setTimeout(() => poll(attempt + 1), 1000);
  };
  poll(0);
}
function renderResearch(data) {
  const decision = data.decision || {};
  const rating = zh('rating', decision.rating);
  const confidence = decision.confidence != null ? `${Math.round(decision.confidence * 100)}%` : '';
  const intent = zh('effect', data.intent?.effect);
  const reruns = (data.rerun_dimensions || []).map((item) => zh('role', item) || item).filter(Boolean);
  const prefs = (data.preferences || []).map((item) => `<li><b>${zh('role', item.scope) || '口径'}</b> ${safe(item.statement)}</li>`).join('');
  const dimCards = ['fundamental', 'technical', 'sentiment', 'macro'].map((name) => {
    const item = (data.dimensions || {})[name];
    const memory = (data.memories || {})[name];
    const hasItem = Boolean(item && (item.summary || item.stance || item.score != null && item.score !== ''));
    if (!hasItem && !memory?.memory_summary) return '';
    const stance = zh('stance', item?.stance);
    const score = !hasItem || item?.score == null || item?.score === '' ? '' : `${item.score} 分`;
    const flags = [
      item?.abstain ? '本维暂不判断' : '',
      item?.degraded ? '数据不完整' : '',
    ].filter(Boolean).map((flag) => `<span class="result-chip">${flag}</span>`).join('');
    return `<article class="result-card dim-card">
      <header><span>${zh('role', name)}</span>${stance ? `<em class="${toneClass(item.stance)}">${stance}</em>` : ''}</header>
      ${score ? `<p class="result-score">${score}</p>` : ''}
      ${item?.summary ? `<p class="result-body">${safe(item.summary)}</p>` : (hasItem ? '' : '')}
      ${flags ? `<div class="result-chips">${flags}</div>` : ''}
      ${memory?.memory_summary ? `<p class="result-note">${memory.carried ? '沿用上次记忆' : '本维记忆'}：${safe(memory.memory_summary)}</p>` : ''}
    </article>`;
  }).join('');
  const riskList = bullets(decision.risks);
  document.querySelector('#research-result').innerHTML = `
    <div class="result-stack">
      <p class="result-meta">${data.parent_run_id ? '反馈修订版' : '首次分析'}${zhTime(data.created_at) ? ` · ${zhTime(data.created_at)}` : ''}${reruns.length ? ` · 重跑 ${reruns.join('、')}` : ''}${intent ? ` · ${intent}` : ''}</p>
      ${decision.rating || decision.advice_one_liner ? `<article class="result-card verdict-card">
        <header><span>综合建议</span>${rating ? `<em class="${toneClass(decision.rating)}">${rating}</em>` : ''}</header>
        ${confidence ? `<p class="result-score">信心 ${confidence}</p>` : ''}
        ${decision.advice_one_liner ? `<p class="result-lead">${safe(decision.advice_one_liner)}</p>` : ''}
        ${riskList ? `<div class="result-block"><h5>需要留意</h5><ul>${riskList}</ul></div>` : ''}
      </article>` : ''}
      ${prefs ? `<article class="result-card"><header><span>已记住的口径</span></header><ul class="result-list">${prefs}</ul></article>` : ''}
      ${dimCards ? `<div class="dim-grid">${dimCards}</div>` : ''}
    </div>`;
  currentRunId = data.run_id;
  currentResearchData = data;
  api('/paper').then((paper) => applyWorkflowState(data, currentTrackData, paper)).catch(() => {});
}
function renderTrack(data) {
  const track = data.tracking || {};
  const user = track.user_output || {};
  const statusKey = track.status || user.holding_advice;
  const status = zh('track', statusKey);
  const summary = user.summary || track.work_summary || '';
  const changes = bullets(user.key_changes);
  const watch = chips(user.next_watch_items);
  const evals = (track.expert_evaluations || []).map((item) => {
    const role = zh('role', item.agent) || '专家';
    const verdict = zh('verdict', item.verdict);
    const reliability = zh('reliability', item.reliability);
    return `<article class="result-card eval-card">
      <header><span>${role}</span>${verdict ? `<em class="${toneClass(item.verdict)}">${verdict}</em>` : ''}</header>
      ${reliability ? `<p class="result-note">材料可信度：${reliability}</p>` : ''}
      ${item.notes ? `<p class="result-body">${safe(item.notes)}</p>` : ''}
    </article>`;
  }).join('');
  const next = track.next_check_suggestion || {};
  const memory = data.memories?.tracking?.memory_summary
    ? `<p class="result-note">${data.memories.tracking.carried ? '沿用上次记忆' : '追踪记忆'}：${safe(data.memories.tracking.memory_summary)}</p>`
    : '';
  const deeper = [
    track.thinking ? `<div class="result-block"><h5>本轮怎么判断的</h5><p>${safe(track.thinking)}</p></div>` : '',
    track.synthesis && track.synthesis !== summary ? `<div class="result-block"><h5>综合看法</h5><p>${safe(track.synthesis)}</p></div>` : '',
  ].join('');
  document.querySelector('#track-result').innerHTML = `
    <div class="result-stack">
      <p class="result-meta">追踪复核${zhTime(data.created_at) ? ` · ${zhTime(data.created_at)}` : ''}</p>
      <article class="result-card verdict-card">
        <header><span>下一步策略</span>${status ? `<em class="${toneClass(statusKey)}">${status}</em>` : ''}</header>
        ${user.title ? `<p class="result-lead">${safe(user.title)}</p>` : ''}
        ${summary ? `<p class="result-body">${safe(summary)}</p>` : ''}
        ${next.reason || zh('urgency', next.urgency) ? `<p class="result-note">下次关注：${[zh('urgency', next.urgency), next.reason ? safe(next.reason) : ''].filter(Boolean).join(' · ')}</p>` : ''}
        ${memory}
      </article>
      ${changes ? `<article class="result-card"><header><span>相对基线的变化</span></header><ul class="result-list">${changes}</ul></article>` : ''}
      ${watch ? `<article class="result-card"><header><span>继续盯紧</span></header><div class="result-chips">${watch}</div></article>` : ''}
      ${evals ? `<div class="eval-grid">${evals}</div>` : ''}
      ${deeper ? `<details class="result-details"><summary>查看判断过程</summary>${deeper}</details>` : ''}
    </div>`;
  currentTrackRunId = data.run_id;
  currentTrackData = data;
  if (data.job_id) currentTrackJobId = data.job_id;
  api('/paper').then((paper) => applyWorkflowState(currentResearchData, data, paper)).catch(() => {});
}
function jobLabels(kind) {
  const track = String(kind || '').startsWith('track');
  return track
    ? { queued: '追踪排队', running: '追踪中', success: '追踪完成', unchanged: '未重跑追踪', failed: '追踪失败' }
    : { queued: '排队中', running: '研究中', success: '研究完成', unchanged: '未改变结论，旧报告未改', failed: '失败' };
}
async function pollResearch(jobId) { try { const job = await api(`/research/jobs/${jobId}`); const labels = jobLabels(job.kind); const trackish = String(job.kind || '').startsWith('track'); const statusEl = document.querySelector(trackish ? '#track-status' : '#research-status'); const failed = job.status === 'failed' ? `${labels.failed}：${job.error || '未知错误'}` : ''; statusEl.textContent = failed || labels[job.status] || job.status; if (job.status === 'success') { if (job.run_id) { const data = await api(`/research/runs/${job.run_id}`); if (trackish) { renderTrack(data); currentTrackJobId = job.job_id || jobId; } else { renderResearch(data); currentJobId = job.job_id || jobId; } } await loadHistory(currentCode); return; } if (job.status === 'unchanged') { if (trackish) { const current = await api(`/research/stocks/${currentCode}/track`).catch(() => null); if (current) { currentTrackJobId = current.job_id; renderTrack(current); } } else { const current = await api(`/research/stocks/${currentCode}/current`); currentJobId = current.job_id; renderResearch(current); } await loadHistory(currentCode); return; } if (job.status !== 'failed') setTimeout(() => pollResearch(jobId), 2500); } catch (error) { document.querySelector('#research-status').textContent = error.message; } }
async function startResearch() { try { setStage('research', 'research', { state: 'current', label: '分析中' }); const data = await api('/research/jobs', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({stock_code: currentCode, kind: 'analyze', prompt: document.querySelector('#research-prompt').value || 'none'}) }); currentJobId = data.job_id; pollResearch(currentJobId); } catch (error) { document.querySelector('#research-status').textContent = error.message; } }
async function startTrack() { try { setStage('track', 'track', { state: 'current', label: '追踪中' }); const data = await api('/research/jobs', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({stock_code: currentCode, kind: 'track', prompt: document.querySelector('#track-prompt').value || 'none'}) }); currentTrackJobId = data.job_id; pollResearch(currentTrackJobId); } catch (error) { document.querySelector('#track-status').textContent = error.message; } }
async function sendFeedback() { if (!currentJobId || !currentRunId) { document.querySelector('#research-status').textContent = '暂无可反馈的研究结果'; return; } try { const data = await api(`/research/jobs/${currentJobId}/feedback`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({prompt: document.querySelector('#feedback-prompt').value}) }); currentJobId = data.job_id; pollResearch(currentJobId); } catch (error) { document.querySelector('#research-status').textContent = error.message; } }
async function sendTrackFeedback() { if (!currentTrackJobId || !currentTrackRunId) { document.querySelector('#track-status').textContent = '暂无可反馈的追踪结果'; return; } try { const data = await api(`/research/jobs/${currentTrackJobId}/feedback`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({prompt: document.querySelector('#track-feedback-prompt').value}) }); currentTrackJobId = data.job_id; pollResearch(currentTrackJobId); } catch (error) { document.querySelector('#track-status').textContent = error.message; } }
async function detail(code) {
  currentCode = code;
  currentJobId = null;
  currentRunId = null;
  currentTrackJobId = null;
  currentTrackRunId = null;
  currentResearchData = null;
  currentTrackData = null;
  view('detail');
  const [quote, bars, notices, current, track, paper] = await Promise.all([
    api(`/quotes/${code}`),
    api(`/bars/${code}`),
    api(`/notices/${code}`),
    api(`/research/stocks/${code}/current`).catch(() => null),
    api(`/research/stocks/${code}/track`).catch(() => null),
    api('/paper').catch(() => ({ positions: [], cash: null })),
  ]);
  const item = quote.item;
  document.querySelector('#detail').innerHTML = `<div class="stock-context"><span class="stock-label">当前研究标的</span><h2>${item.name} <small>${code}</small></h2><p class="price">${text(item.price)} <span class="${item.change_pct >= 0 ? 'up' : 'down'}">${text(item.change_pct)}%</span></p></div><div class="stock-actions"><button id="watch">加入自选</button><button id="open-profile" class="button-secondary">用户画像</button><button id="open-paper" class="button-secondary">模拟账户</button></div>`;
  document.querySelector('#watch').onclick = async () => {
    await api(`/watchlist/${code}`, { method: 'PUT' });
    document.querySelector('#watch').textContent = '已加入自选';
  };
  document.querySelector('#open-profile').onclick = () => loadProfile(code);
  document.querySelector('#open-paper').onclick = () => loadPaper();
  document.querySelector('#paper-buy').onclick = () => paperTrade(code, 'buy');
  document.querySelector('#paper-sell').onclick = () => paperTrade(code, 'sell');
  document.querySelector('#workflow-open-profile').onclick = () => loadProfile(code);
  currentBars = bars.items || [];
  drawChart();
  if (!currentBars.length) collectBars();
  document.querySelector('#notices').innerHTML = notices.items.map((notice) => `<li>${text(notice.published_at)} ${notice.url ? `<a href="${notice.url}" target="_blank">${notice.title}</a>` : notice.title}</li>`).join('') || '<li>暂无公告。拉取日线时会一并尝试采集。</li>';
  document.querySelector('#research-status').textContent = current ? '已有研究基线' : '尚未进行首次分析';
  document.querySelector('#track-status').textContent = track ? '已有追踪结果' : '';
  document.querySelector('#research-result').innerHTML = '<p class="empty-state">完成首次分析后，四维结论和决策会显示在这里。</p>';
  document.querySelector('#track-result').innerHTML = '<p class="empty-state">手动触发追踪后，下一步策略会显示在这里。</p>';
  if (current) {
    renderResearch(current);
    currentRunId = current.run_id;
    currentJobId = current.job_id;
  }
  if (track) renderTrack(track);
  applyWorkflowState(current, track, paper);
  await loadHistory(code);
}
function historyKind(item) {
  if (item.noop) return '未改变';
  if (item.kind === 'track_feedback') return '追踪反馈';
  if (item.kind === 'track') return '追踪';
  if (item.kind === 'feedback') return '反馈版';
  return '首次研究';
}
async function loadHistory(code) { const data = await api(`/research/stocks/${code}/history`); document.querySelector('#research-history').innerHTML = `<h3>研究历史</h3>` + data.items.map((item) => `<p>${zhTime(item.created_at) || '时间未知'} · ${historyKind(item)} · ${zh('status', item.status) || '已记录'}${item.run_id ? ` <button class="history-view" data-run-id="${item.run_id}">查看此版本</button>` : ''}</p>`).join(''); document.querySelectorAll('.history-view').forEach((button) => button.addEventListener('click', () => loadRun(button.getAttribute('data-run-id')))); }
async function loadRun(runId) { const data = await api(`/research/runs/${runId}`); if (data.mode === 'track_day' || data.tracking) { renderTrack(data); document.querySelector('#track-status').textContent = '已切换到历史追踪'; document.querySelector('#track-result').scrollIntoView({behavior: 'smooth', block: 'start'}); return; } renderResearch(data); currentRunId = runId; if (data.job_id) currentJobId = data.job_id; document.querySelector('#research-status').textContent = '已切换到历史分析'; document.querySelector('#research-result').scrollIntoView({behavior: 'smooth', block: 'start'}); }
async function loadWatchlist() { view('watchlist'); const data = await api('/watchlist'); document.querySelector('#watchlist').innerHTML = data.items.map((item) => `<p><button class="stock" data-code="${item.stock_code}">${item.stock_code} ${text(item.name)}</button> ${text(item.price)} <span class="${item.change_pct >= 0 ? 'up' : 'down'}">${text(item.change_pct)}%</span></p>`).join('') || '<p>尚未加入自选股。</p>'; document.querySelectorAll('.stock').forEach((button) => button.onclick = () => detail(button.dataset.code)); }
async function paperTrade(code, side) {
  const status = document.querySelector('#paper-trade-status');
  try {
    const quantity = Number(document.querySelector('#paper-qty').value);
    const reason = document.querySelector('#paper-reason').value;
    const data = await api('/paper/trades', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({stock_code: code, side, quantity, reason}) });
    status.textContent = `${side === 'buy' ? '买入' : '卖出'}成功。总资产 ${yuan(data.equity)}，现金 ${yuan(data.cash)}，浮动 ${yuan(data.pnl)}`;
    renderInvestmentState(data, code, currentResearchData);
  } catch (error) {
    status.textContent = error.message;
  }
}
async function loadPaper() {
  view('paper');
  const box = document.querySelector('#paper');
  try {
    const data = await api('/paper');
    const pnlClass = data.pnl >= 0 ? 'up' : 'down';
    const positions = (data.positions || []).map((item) => `<tr data-code="${item.stock_code}"><td>${item.stock_code}</td><td>${text(item.name)}</td><td>${item.quantity}</td><td>${text(item.avg_cost)}</td><td>${text(item.last_price)}</td><td>${yuan(item.market_value)}</td><td class="${item.unrealized >= 0 ? 'up' : 'down'}">${yuan(item.unrealized)}</td></tr>`).join('') || '<tr><td colspan="7">暂无持仓</td></tr>';
    const fills = (data.fills || []).map((item) => `<p>${item.created_at} · ${item.side === 'buy' ? '买' : '卖'} ${item.stock_code} ${item.quantity} 股 @ ${item.price}，金额 ${yuan(item.amount)}</p>`).join('') || '<p class="notice">暂无成交</p>';
    const picks = (data.picks || []).map((item) => `<div class="profile-pref"><div><b>${item.stock_code === 'none' ? '全局选股纪律' : item.stock_code}</b><br>${item.statement}</div><button class="pick-retire" data-id="${item.memory_id}">停用</button></div>`).join('') || '<p class="notice">尚无选股记忆。买入时可写理由，或在下方记下纪律。</p>';
    box.innerHTML = `<p class="portfolio-summary"><span>总资产 <b>${yuan(data.equity)}</b></span><span>现金 ${yuan(data.cash)}</span><span>持仓市值 ${yuan(data.market_value)}</span><span>相对本金 <em class="${pnlClass}">${yuan(data.pnl)}（${(data.pnl_pct * 100).toFixed(2)}%）</em></span></p><p class="notice">${data.disclaimer}</p><h3>持仓</h3><section class="table-wrap"><table><thead><tr><th>代码</th><th>名称</th><th>数量</th><th>成本</th><th>现价</th><th>市值</th><th>浮动</th></tr></thead><tbody id="paper-positions">${positions}</tbody></table></section><h3>最近成交</h3><section class="content-card fill-list">${fills}</section><h3>选股记忆</h3><section class="content-card memory-list">${picks}</section><section class="paper-box"><h3>新增选股记忆</h3><input id="pick-code" placeholder="股票代码，空则为全局纪律"><input id="pick-statement" placeholder="选股纪律或这只票为什么在池子里"><button id="pick-save">记下</button></section>`;
    box.querySelectorAll('#paper-positions tr[data-code]').forEach((row) => row.onclick = () => detail(row.dataset.code));
    box.querySelectorAll('.pick-retire').forEach((button) => button.onclick = async () => { await api(`/paper/picks/${button.dataset.id}`, { method: 'DELETE' }); await loadPaper(); });
    document.querySelector('#pick-save').onclick = async () => {
      await api('/paper/picks', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({stock_code: document.querySelector('#pick-code').value || 'none', statement: document.querySelector('#pick-statement').value}) });
      await loadPaper();
    };
  } catch (error) {
    box.innerHTML = `<p class="notice">模拟账户读取失败：${error.message}</p>`;
  }
}
function renderGeneratedProfile(profile) {
  if (!profile) {
    return `<section class="persona-empty"><p class="section-kicker">AI PERSONA</p><h3>还没有生成投资者画像</h3><p>模型会根据你已记住的选股口径、模拟持仓与研究行为，归纳策略偏好和决策风格。</p><button id="profile-generate">生成我的投资者画像</button></section>`;
  }
  const riskNames = { conservative: '偏稳健', balanced: '攻守平衡', aggressive: '偏进取', barbell: '杠铃型', unclear: '尚不明确' };
  const strategies = (profile.strategy_preferences || []).map((item) => `<li>${safe(item)}</li>`).join('');
  const strengths = (profile.strengths || []).map((item) => `<li>${safe(item)}</li>`).join('') || '<li>证据暂不足</li>';
  const blindSpots = (profile.blind_spots || []).map((item) => `<li>${safe(item)}</li>`).join('') || '<li>证据暂不足</li>';
  const basis = (profile.evidence_basis || []).map((item) => `<li>${safe(item)}</li>`).join('');
  const generatedAt = profile.generated_at ? new Date(profile.generated_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '-';
  return `<section class="persona-hero"><div><p class="section-kicker">AI PERSONA</p><h3>${safe(profile.persona_title)}</h3><p class="persona-overview">${safe(profile.overview)}</p></div><div class="persona-meta"><span>${safe(riskNames[profile.risk_tendency] || profile.risk_tendency)}</span><span>可信度 ${safe(profile.confidence)}</span></div></section><section class="persona-section"><h3>偏好的选股策略</h3><ol class="strategy-list">${strategies}</ol></section><section class="persona-section"><h3>决策方式</h3><p>${safe(profile.decision_style)}</p></section><section class="persona-columns"><div><h3>已有优势</h3><ul>${strengths}</ul></div><div><h3>可能的盲点</h3><ul>${blindSpots}</ul></div></section><details class="persona-evidence"><summary>这份画像依据什么</summary><ul>${basis}</ul></details><div class="persona-footer"><span>生成于 ${generatedAt}</span><button id="profile-generate" class="button-secondary">重新生成画像</button></div>`;
}

async function loadProfile(stock) {
  const names = {fundamental:'基本面', technical:'技术面', sentiment:'情绪', macro:'宏观', tracking:'追踪', decision:'决策'};
  profileStock = stock || '';
  view('profile');
  const box = document.querySelector('#profile');
  try {
    const query = profileStock ? `?stock_code=${encodeURIComponent(profileStock)}` : '';
    const [data, paper] = await Promise.all([api(`/profile${query}`), api('/paper').catch(() => ({picks: []}))]);
    const pickRows = (paper.picks || []).filter((item) => !profileStock || item.stock_code === profileStock || item.stock_code === 'none');
    const prefs = (data.preferences || []).map((item) => `<div class="profile-pref"><div><b>${safe(names[item.scope] || item.scope)}</b><span>${item.stock_code === 'none' ? '全局策略' : safe(item.stock_code)}</span><p>${safe(item.statement)}</p></div><button class="pref-retire" data-id="${safe(item.preference_id)}">停用</button></div>`).join('') || '<p class="notice">还没有明确记住的研究口径。</p>';
    const picks = pickRows.map((item) => `<div class="profile-pref"><div><b>选股依据</b><span>${item.stock_code === 'none' ? '全局策略' : safe(item.stock_code)}</span><p>${safe(item.statement)}</p></div></div>`).join('') || '<p class="notice">还没有选股记忆。</p>';
    box.innerHTML = `${renderGeneratedProfile(data.generated_profile)}<section class="profile-source"><div class="profile-source-heading"><div><p class="section-kicker">YOUR RULES</p><h3>你明确表达过的策略</h3></div><p class="notice">这些内容是画像的事实依据，可随时停用</p></div><div class="profile-rule-grid"><div><h4>研究口径</h4>${prefs}</div><div><h4>选股记忆</h4>${picks}</div></div></section>`;
    box.querySelectorAll('.pref-retire').forEach((button) => button.onclick = async () => { await api(`/profile/preferences/${button.dataset.id}`, { method: 'DELETE' }); await loadProfile(profileStock); });
    document.querySelector('#profile-generate').onclick = generateProfile;
  } catch (error) {
    box.innerHTML = `<p class="notice">画像读取失败：${error.message}</p>`;
  }
}

function renderMemoryList(title, items) {
  const rows = (items || []).filter(Boolean);
  if (!rows.length) return '';
  return `<div class="expert-list"><h5>${title}</h5><ul>${rows.map((item) => `<li>${safe(item)}</li>`).join('')}</ul></div>`;
}

function uniqueTexts(items) {
  const seen = new Set();
  const rows = [];
  (items || []).forEach((item) => {
    const text = String(item || '').trim();
    if (!text || seen.has(text)) return;
    seen.add(text);
    rows.push(text);
  });
  return rows;
}

function stripTaskLabel(text) {
  return String(text || '')
    .replace(/^[^：:]{1,16}[：:]\s*/, '')
    .replace(/[（(]第[^)）]+[)）]\s*$/, '')
    .trim() || String(text || '').trim();
}

function mergeExpertExperience(memories) {
  const rows = memories || [];
  const pick = (key) => uniqueTexts(rows.flatMap((item) => item[key] || []));
  const summaries = uniqueTexts(rows.map((item) => stripTaskLabel(item.memory_summary || '')).filter(Boolean));
  const lessons = pick('lessons');
  return {
    digest: lessons.length ? lessons.join('；') : summaries.join('；'),
    lessons,
    hypotheses: pick('active_hypotheses'),
    falsifiers: pick('falsifiers_watched'),
    questions: pick('open_questions'),
    tasks: pick('pending_tasks'),
  };
}

function renderExpertMemory(item) {
  const stock = item.stock_code ? safe(item.stock_code) : '未绑定标的';
  const carried = item.carried ? '已迭代' : '首轮沉淀';
  const updated = zhTime(item.updated_at);
  return `<article class="expert-memory">
    <div class="expert-memory-head"><b>${stock}</b><span>${carried}</span>${updated ? `<span>${updated}</span>` : ''}</div>
    ${item.memory_summary ? `<p>${safe(item.memory_summary)}</p>` : ''}
    ${renderMemoryList('经验教训', item.lessons)}
    ${renderMemoryList('当前假设', item.active_hypotheses)}
    ${renderMemoryList('证伪条件', item.falsifiers_watched)}
    ${renderMemoryList('未决问题', item.open_questions)}
    ${renderMemoryList('待办', item.pending_tasks)}
  </article>`;
}

function renderGroupedPreferences(preferences) {
  const kinds = { constraint: '约束', preference: '偏好', anti_pattern: '避免再犯' };
  const groups = [
    { id: 'fundamental', title: '基本面专家' },
    { id: 'technical', title: '技术面专家' },
    { id: 'sentiment', title: '情绪面专家' },
    { id: 'macro', title: '宏观专家' },
  ];
  const extra = [
    { id: 'decision', title: '综合决策' },
    { id: 'tracking', title: '追踪专家' },
  ];
  if (!(preferences || []).length) {
    return '<p class="notice">还没有记住的决策偏好。分析或追踪时选择「记住」，会按维度归到对应专家。</p>';
  }
  const renderGroup = (group) => {
    const rows = (preferences || []).filter((item) => item.scope === group.id);
    const body = rows.length
      ? rows.map((item) => `<div class="profile-pref"><div><span>${kinds[item.kind] || '口径'}</span><span>${item.stock_code === 'none' ? '全局' : safe(item.stock_code)}</span><p>${safe(item.statement)}</p></div></div>`).join('')
      : '<p class="notice">暂无该专家口径</p>';
    return `<div class="pref-expert-group"><h4>${group.title}</h4>${body}</div>`;
  };
  const extras = extra.filter((group) => (preferences || []).some((item) => item.scope === group.id));
  return `<div class="pref-expert-grid">${groups.map(renderGroup).join('')}</div>${extras.length ? `<div class="pref-expert-extra">${extras.map(renderGroup).join('')}</div>` : ''}`;
}

function renderExperts(data) {
  const prefs = renderGroupedPreferences(data.preferences || []);
  const cards = (data.experts || []).map((expert) => {
    const memories = expert.memories || [];
    const merged = mergeExpertExperience(memories);
    const digest = merged.digest || expert.duty;
    const rounds = memories.length
      ? `<details class="expert-rounds"><summary>分次分析记录<span>${memories.length} 条</span></summary>${memories.map(renderExpertMemory).join('')}</details>`
      : '<p class="notice">还没有分次分析记录。</p>';
    return `<article class="expert-card result-card">
      <header><p class="result-kicker">跨任务经验</p><h3>${safe(expert.title)}</h3></header>
      <p class="expert-duty">${safe(expert.duty)}</p>
      <div class="result-chips"><span class="result-chip">覆盖 ${expert.stock_count || 0} 只标的</span><span class="result-chip">${expert.iterated ? '已跨轮继承' : '尚未跨轮'}</span></div>
      <h4>经验总结</h4>
      <p class="expert-digest">${safe(digest)}</p>
      ${renderMemoryList('惯用假设', merged.hypotheses)}
      ${renderMemoryList('惯用证伪', merged.falsifiers)}
      ${renderMemoryList('未决问题', merged.questions)}
      ${rounds}
    </article>`;
  }).join('');
  return `<section class="content-card expert-pref-panel"><p class="section-kicker">DECISION PREFERENCE</p><h3>决策偏好</h3><p class="notice">按四位专家归类；综合决策与追踪口径单独列出。</p>${prefs}</section><div class="expert-grid">${cards}</div>`;
}

async function loadExperts() {
  view('experts');
  const box = document.querySelector('#experts');
  box.innerHTML = '<p class="notice">正在读取专家状态…</p>';
  try {
    box.innerHTML = renderExperts(await api('/experts'));
  } catch (error) {
    box.innerHTML = `<p class="notice">专家状态读取失败：${error.message}</p>`;
  }
}

async function generateProfile() {
  const button = document.querySelector('#profile-generate');
  button.disabled = true;
  button.textContent = '模型正在形成画像…';
  try {
    await api('/profile/generate', { method: 'POST' });
    await loadProfile(profileStock);
  } catch (error) {
    button.disabled = false;
    button.textContent = '重新生成画像';
    const message = document.createElement('p');
    message.className = 'operation-status';
    message.textContent = `生成失败：${error.message}`;
    button.parentElement.appendChild(message);
  }
}
async function loadStatus() { const status = await (await fetch('/api/v1/meta/ingest')).json(); const asOf = status.quotes_as_of ? new Date(status.quotes_as_of).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '暂无成功采集'; document.querySelector('#status').textContent = status.collecting ? '正在采集延迟行情…' : (status.stale ? `数据可能过期，最近成功：${asOf}` : `数据截至：${asOf}，覆盖 ${status.stock_count} 只股票`); document.querySelector('#status').className = status.collecting ? 'stale' : (status.stale ? 'stale' : ''); setCollecting(!!status.collecting); return status; }
function setCollecting(running) {
  ['#collect', '#collect-market'].forEach((selector) => {
    const button = document.querySelector(selector);
    if (!button) return;
    button.disabled = running;
    button.textContent = running ? '采集中…' : '采集行情';
  });
}
async function collectQuotes() {
  setCollecting(true);
  document.querySelector('#status').textContent = '正在采集延迟行情…';
  document.querySelector('#status').className = 'stale';
  try {
    await api('/quotes/collect', { method: 'POST' });
  } catch (error) {
    if (!/already running/.test(error.message)) {
      document.querySelector('#status').textContent = `采集失败：${error.message}`;
      setCollecting(false);
      return;
    }
  }
  const poll = async () => {
    const status = await loadStatus();
    if (status.collecting) {
      setTimeout(poll, 1000);
      return;
    }
    page = 1;
    await load();
    if (status.last_run && status.last_run.status === 'failed') {
      document.querySelector('#status').textContent = `采集失败：${status.last_run.error || '未知错误'}`;
      document.querySelector('#status').className = 'stale';
    }
  };
  setTimeout(poll, 800);
}
document.querySelector('#refresh').onclick = () => { page = 1; load(); }; document.querySelector('#collect').onclick = collectQuotes; document.querySelector('#collect-market').onclick = collectQuotes; document.querySelector('#search').onkeydown = (event) => { if (event.key === 'Enter') { page = 1; load(); } }; document.querySelector('#search').onkeydown = (event) => { if (event.key === 'Enter') { page = 1; load(); } }; document.querySelector('#previous').onclick = () => { page -= 1; load(); }; document.querySelector('#next').onclick = () => { page += 1; load(); }; document.querySelector('#research-start').onclick = startResearch; document.querySelector('#feedback-send').onclick = sendFeedback; document.querySelector('#track-start').onclick = startTrack; document.querySelector('#track-feedback-send').onclick = sendTrackFeedback; document.querySelector('#chart-mode-line').onclick = () => { chartMode = 'line'; drawChart(); }; document.querySelector('#chart-mode-k').onclick = () => { chartMode = 'k'; drawChart(); }; document.querySelector('#chart-collect').onclick = collectBars; document.querySelectorAll('nav button').forEach((button) => button.onclick = () => { if (button.dataset.view === 'watchlist') loadWatchlist(); else if (button.dataset.view === 'profile') loadProfile(); else if (button.dataset.view === 'paper') loadPaper(); else if (button.dataset.view === 'experts') loadExperts(); else view('market'); }); document.querySelector('.back').onclick = () => view('market'); loadStatus(); load();
