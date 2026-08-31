let page = 1, total = 0, currentCode = null, currentJobId = null, currentRunId = null, currentTrackJobId = null, currentTrackRunId = null, profileStock = '', currentBars = [], chartMode = 'line';
const size = 50;
const text = (value) => value === null || value === undefined ? '-' : value;
const money = (value) => value === null || value === undefined ? '-' : `${(value / 100000000).toFixed(2)} 亿`;
const yuan = (value) => value === null || value === undefined ? '-' : `${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} 元`;
const api = (path, options) => fetch(`/api/v1${path}`, options).then(async (response) => { const data = await response.json(); if (!response.ok) throw new Error(data.detail || '请求失败'); return data; });
function view(name) { ['market', 'detail', 'watchlist', 'paper', 'profile'].forEach((item) => document.querySelector(`#${item}-view`).hidden = item !== name); }
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
  const names = {fundamental:'基本面', technical:'技术面', sentiment:'情绪', macro:'宏观', tracking:'追踪', decision:'决策'};
  const dimKeys = Object.keys(data.dimensions || {});
  const memoryKeys = Object.keys(data.memories || {});
  const keys = [...new Set([...dimKeys, ...memoryKeys])];
  const dims = keys.map((name) => {
    const item = (data.dimensions || {})[name];
    const memory = (data.memories || {})[name];
    const memoryLine = memory && memory.memory_summary
      ? `<br><span class="research-memory">${memory.carried ? '沿用上次记忆' : '本维记忆'}：${memory.memory_summary}</span>`
      : '';
    const body = item ? `${item.stance} / ${item.score}分 — ${item.summary}` : '';
    return `<p><b>${names[name] || name}</b>：${body}${memoryLine}</p>`;
  }).join('');
  const decision = data.decision ? `<p><b>决策</b>：${data.decision.rating}（信心 ${(data.decision.confidence * 100).toFixed(0)}%）— ${data.decision.advice_one_liner}</p>` : '';
  const prefs = (data.preferences || []).map((item) => `${names[item.scope] || item.scope}：${item.statement}`).join('；');
  const prefLine = prefs ? `<p class="research-memory"><b>已记住口径</b>：${prefs}</p>` : '';
  const reruns = data.rerun_dimensions || [];
  const lineage = `<p class="research-lineage">当前报告：${data.parent_run_id ? '反馈版' : '首次研究'} · ${text(data.created_at || '')}<br>编号：${text(data.short_id || data.run_id?.slice(0, 8))}${data.parent_run_id ? ` · 基于 ${data.parent_run_id.slice(0, 8)}` : ''}${reruns.length ? ` · 重跑：${reruns.join('、')}` : ''}</p>`;
  document.querySelector('#research-result').innerHTML = `${lineage}<p><b>意图理解</b>：${data.intent?.effect || 'none'}</p>${prefLine}${dims}${decision}`;
  currentRunId = data.run_id;
}
function renderTrack(data) {
  const track = data.tracking || {};
  const user = track.user_output || {};
  const evals = (track.expert_evaluations || []).map((item) => `${item.agent}：${item.verdict}/${item.reliability}${item.notes ? ` — ${item.notes}` : ''}`).join('；');
  const changes = (user.key_changes || []).join('；');
  const watch = (user.next_watch_items || []).join('；');
  const memory = data.memories && data.memories.tracking && data.memories.tracking.memory_summary
    ? `<p class="research-memory">${data.memories.tracking.carried ? '沿用上次记忆' : '本维记忆'}：${data.memories.tracking.memory_summary}</p>`
    : '';
  const next = track.next_check_suggestion && (track.next_check_suggestion.reason || track.next_check_suggestion.urgency)
    ? `<p>下次关注：${text(track.next_check_suggestion.urgency)} — ${text(track.next_check_suggestion.reason)}</p>`
    : '';
  document.querySelector('#track-result').innerHTML = `<p class="research-lineage">追踪 · ${text(data.created_at || '')}<br>编号：${text(data.short_id || data.run_id?.slice(0, 8))}${data.parent_run_id ? ` · 基于 ${data.parent_run_id.slice(0, 8)}` : ''}</p><p><b>结论</b>：${text(track.status || user.holding_advice)} — ${text(user.summary || track.work_summary)}</p>${user.title ? `<p>${user.title}</p>` : ''}${track.thinking ? `<p><b>思考</b>：${track.thinking}</p>` : ''}${track.synthesis ? `<p><b>汇总</b>：${track.synthesis}</p>` : ''}${evals ? `<p><b>专家评测</b>：${evals}</p>` : ''}${changes ? `<p>关键变化：${changes}</p>` : ''}${watch ? `<p>继续盯：${watch}</p>` : ''}${next}${memory}`;
  currentTrackRunId = data.run_id;
  if (data.job_id) currentTrackJobId = data.job_id;
}
function jobLabels(kind) {
  const track = String(kind || '').startsWith('track');
  return track
    ? { queued: '追踪排队', running: '追踪中', success: '追踪完成', unchanged: '未重跑追踪', failed: '追踪失败' }
    : { queued: '排队中', running: '研究中', success: '研究完成', unchanged: '未改变结论，旧报告未改', failed: '失败' };
}
async function pollResearch(jobId) { try { const job = await api(`/research/jobs/${jobId}`); const labels = jobLabels(job.kind); const trackish = String(job.kind || '').startsWith('track'); const statusEl = document.querySelector(trackish ? '#track-status' : '#research-status'); const failed = job.status === 'failed' ? `${labels.failed}：${job.error || '未知错误'}` : ''; statusEl.textContent = failed || labels[job.status] || job.status; if (job.status === 'success') { if (job.run_id) { const data = await api(`/research/runs/${job.run_id}`); if (trackish) { renderTrack(data); currentTrackJobId = job.job_id || jobId; } else { renderResearch(data); currentJobId = job.job_id || jobId; } } await loadHistory(currentCode); return; } if (job.status === 'unchanged') { if (trackish) { const current = await api(`/research/stocks/${currentCode}/track`).catch(() => null); if (current) { currentTrackJobId = current.job_id; renderTrack(current); } } else { const current = await api(`/research/stocks/${currentCode}/current`); currentJobId = current.job_id; renderResearch(current); } await loadHistory(currentCode); return; } if (job.status !== 'failed') setTimeout(() => pollResearch(jobId), 2500); } catch (error) { document.querySelector('#research-status').textContent = error.message; } }
async function startResearch() { try { const data = await api('/research/jobs', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({stock_code: currentCode, kind: 'analyze', prompt: document.querySelector('#research-prompt').value || 'none'}) }); currentJobId = data.job_id; pollResearch(currentJobId); } catch (error) { document.querySelector('#research-status').textContent = error.message; } }
async function startTrack() { try { const data = await api('/research/jobs', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({stock_code: currentCode, kind: 'track', prompt: document.querySelector('#track-prompt').value || 'none'}) }); currentTrackJobId = data.job_id; pollResearch(currentTrackJobId); } catch (error) { document.querySelector('#track-status').textContent = error.message; } }
async function sendFeedback() { if (!currentJobId || !currentRunId) { document.querySelector('#research-status').textContent = '暂无可反馈的研究结果'; return; } try { const data = await api(`/research/jobs/${currentJobId}/feedback`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({prompt: document.querySelector('#feedback-prompt').value}) }); currentJobId = data.job_id; pollResearch(currentJobId); } catch (error) { document.querySelector('#research-status').textContent = error.message; } }
async function sendTrackFeedback() { if (!currentTrackJobId || !currentTrackRunId) { document.querySelector('#track-status').textContent = '暂无可反馈的追踪结果'; return; } try { const data = await api(`/research/jobs/${currentTrackJobId}/feedback`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({prompt: document.querySelector('#track-feedback-prompt').value}) }); currentTrackJobId = data.job_id; pollResearch(currentTrackJobId); } catch (error) { document.querySelector('#track-status').textContent = error.message; } }
async function detail(code) { currentCode = code; currentJobId = null; currentRunId = null; currentTrackJobId = null; currentTrackRunId = null; view('detail'); const [quote, bars, notices, current, track] = await Promise.all([api(`/quotes/${code}`), api(`/bars/${code}`), api(`/notices/${code}`), api(`/research/stocks/${code}/current`).catch(() => null), api(`/research/stocks/${code}/track`).catch(() => null)]); const item = quote.item; document.querySelector('#detail').innerHTML = `<h2>${item.name} <small>${code}</small></h2><p class="price">${text(item.price)} <span class="${item.change_pct >= 0 ? 'up' : 'down'}">${text(item.change_pct)}%</span></p><button id="watch">加入自选</button> <button id="open-profile">用户画像</button> <button id="open-paper">模拟账户</button>
<section class="paper-box"><h3>模拟交易</h3><p class="notice">虚拟资金，按当前延迟价成交，持仓市值随行情涨跌。</p><input id="paper-qty" type="number" value="100" min="100" step="100"><input id="paper-reason" placeholder="选股理由（写入选股记忆，可选）"><button id="paper-buy">买入</button> <button id="paper-sell">卖出</button><p id="paper-trade-status"></p></section>`; document.querySelector('#watch').onclick = async () => { await api(`/watchlist/${code}`, { method: 'PUT' }); document.querySelector('#watch').textContent = '已加入自选'; }; document.querySelector('#open-profile').onclick = () => loadProfile(code); document.querySelector('#open-paper').onclick = () => loadPaper(); document.querySelector('#paper-buy').onclick = () => paperTrade(code, 'buy'); document.querySelector('#paper-sell').onclick = () => paperTrade(code, 'sell'); currentBars = bars.items || []; drawChart(); if (!currentBars.length) collectBars(); document.querySelector('#notices').innerHTML = notices.items.map((notice) => `<li>${text(notice.published_at)} ${notice.url ? `<a href="${notice.url}" target="_blank">${notice.title}</a>` : notice.title}</li>`).join('') || '<li>暂无公告。拉取日线时会一并尝试采集。</li>'; document.querySelector('#research-status').textContent = current ? '已有研究结果' : ''; document.querySelector('#track-status').textContent = track ? '已有追踪结果' : ''; document.querySelector('#research-result').innerHTML = ''; document.querySelector('#track-result').innerHTML = ''; if (current) { renderResearch(current); currentRunId = current.run_id; currentJobId = current.job_id; } if (track) { renderTrack(track); } await loadHistory(code); }
function historyKind(item) {
  if (item.noop) return '未改变';
  if (item.kind === 'track_feedback') return '追踪反馈';
  if (item.kind === 'track') return '追踪';
  if (item.kind === 'feedback') return '反馈版';
  return '首次研究';
}
async function loadHistory(code) { const data = await api(`/research/stocks/${code}/history`); document.querySelector('#research-history').innerHTML = `<h3>研究历史</h3>` + data.items.map((item) => `<p>${item.created_at} · ${historyKind(item)} · ${item.noop ? `基于 ${item.parent_run_id?.slice(0, 8) || '-'}` : `编号 ${item.run_id?.slice(0, 8) || '-'}`}：${item.status}${item.run_id ? ` <button class="history-view" data-run-id="${item.run_id}">查看此版本</button>` : ''}</p>`).join(''); document.querySelectorAll('.history-view').forEach((button) => button.addEventListener('click', () => loadRun(button.getAttribute('data-run-id')))); }
async function loadRun(runId) { const data = await api(`/research/runs/${runId}`); if (data.mode === 'track_day' || data.tracking) { renderTrack(data); document.querySelector('#track-status').textContent = `已切换到历史追踪：${runId}`; document.querySelector('#track-result').scrollIntoView({behavior: 'smooth', block: 'start'}); return; } renderResearch(data); currentRunId = runId; if (data.job_id) currentJobId = data.job_id; document.querySelector('#research-status').textContent = `已切换到历史报告：${runId}`; document.querySelector('#research-result').scrollIntoView({behavior: 'smooth', block: 'start'}); }
async function loadWatchlist() { view('watchlist'); const data = await api('/watchlist'); document.querySelector('#watchlist').innerHTML = data.items.map((item) => `<p><button class="stock" data-code="${item.stock_code}">${item.stock_code} ${text(item.name)}</button> ${text(item.price)} <span class="${item.change_pct >= 0 ? 'up' : 'down'}">${text(item.change_pct)}%</span></p>`).join('') || '<p>尚未加入自选股。</p>'; document.querySelectorAll('.stock').forEach((button) => button.onclick = () => detail(button.dataset.code)); }
async function paperTrade(code, side) {
  const status = document.querySelector('#paper-trade-status');
  try {
    const quantity = Number(document.querySelector('#paper-qty').value);
    const reason = document.querySelector('#paper-reason').value;
    const data = await api('/paper/trades', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({stock_code: code, side, quantity, reason}) });
    status.textContent = `${side === 'buy' ? '买入' : '卖出'}成功。总资产 ${yuan(data.equity)}，现金 ${yuan(data.cash)}，浮动 ${yuan(data.pnl)}`;
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
    box.innerHTML = `<p>总资产 <b>${yuan(data.equity)}</b> · 现金 ${yuan(data.cash)} · 持仓市值 ${yuan(data.market_value)} · 相对本金 <span class="${pnlClass}">${yuan(data.pnl)}</span>（${(data.pnl_pct * 100).toFixed(2)}%）</p><p class="notice">${data.disclaimer}</p><h3>持仓</h3><section class="table-wrap"><table><thead><tr><th>代码</th><th>名称</th><th>数量</th><th>成本</th><th>现价</th><th>市值</th><th>浮动</th></tr></thead><tbody id="paper-positions">${positions}</tbody></table></section><h3>最近成交</h3>${fills}<h3>选股记忆</h3>${picks}<section class="paper-box"><input id="pick-code" placeholder="股票代码，空则为全局纪律"><input id="pick-statement" placeholder="选股纪律或这只票为什么在池子里"><button id="pick-save">记下</button></section>`;
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
function countLine(map, names) {
  const entries = Object.entries(map || {}).filter(([, count]) => count);
  return entries.length ? entries.map(([key, count]) => `${(names && names[key]) || key} ${count}`).join(' · ') : '无';
}
async function loadProfile(stock) {
  const names = {fundamental:'基本面', technical:'技术面', sentiment:'情绪', macro:'宏观', tracking:'追踪', decision:'决策', this_run:'当次', remember:'记住', rerun:'重跑', remember_rerun:'记住并重跑', pre_run:'研究前', post_decision:'决策后', post_track:'追踪后'};
  profileStock = stock || '';
  view('profile');
  const box = document.querySelector('#profile');
  try {
    const query = profileStock ? `?stock_code=${encodeURIComponent(profileStock)}` : '';
    const [data, paper] = await Promise.all([api(`/profile${query}`), api('/paper').catch(() => ({picks: []}))]);
    const highlights = (data.highlights || []).length ? `<p class="profile-highlights">${data.highlights.join(' · ')}</p>` : '';
    const pickRows = (paper.picks || []).filter((item) => !profileStock || item.stock_code === profileStock || item.stock_code === 'none');
    const empty = !data.utterance_count && !(data.preferences || []).length && !(data.expert_memories || []).length && !pickRows.length;
    const emptyLine = empty ? '<p class="notice">还没有可聚合的口径。研究里 #remember、模拟页选股记忆、专家跨次记忆都会出现在这里。</p>' : '';
    const prefs = (data.preferences || []).map((item) => `<div class="profile-pref"><div><b>${names[item.scope] || item.scope}</b> · ${item.stock_code === 'none' ? '全局' : item.stock_code}<br>${item.statement}</div><button class="pref-retire" data-id="${item.preference_id}">停用</button></div>`).join('') || '<p class="notice">尚无已记住口径。</p>';
    const expert = (data.expert_memories || []).map((item) => `<p class="research-memory"><b>${names[item.agent_name] || item.agent_name}</b> · ${item.stock_code || '-'}：${item.carried ? '沿用上次记忆' : '本维记忆'} ${item.memory_summary}</p>`).join('') || '<p class="notice">尚无专家跨次记忆。</p>';
    const picks = pickRows.map((item) => `<div class="profile-pref"><div><b>选股</b> · ${item.stock_code === 'none' ? '全局' : item.stock_code}<br>${item.statement}</div></div>`).join('') || '<p class="notice">尚无选股记忆。</p>';
    box.innerHTML = `${highlights}${emptyLine}<p>行为 ${data.utterance_count || 0} 次</p><p>效力：${countLine(data.effects, names)}</p><p>点名：${countLine(data.tags, names)}</p><p>写过槽位：${countLine(data.dims, names)}</p><p>涉及股票：${(data.stocks || []).map((item) => `${item.stock_code} ${item.count}`).join(' · ') || '无'}</p><h3>已记住口径${profileStock ? `（含 ${profileStock} 与全局）` : ''}</h3>${prefs}<h3>选股记忆</h3>${picks}<h3>专家记忆</h3>${expert}`;
    box.querySelectorAll('.pref-retire').forEach((button) => button.onclick = async () => { await api(`/profile/preferences/${button.dataset.id}`, { method: 'DELETE' }); await loadProfile(profileStock); });
  } catch (error) {
    box.innerHTML = `<p class="notice">画像读取失败：${error.message}</p>`;
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
document.querySelector('#refresh').onclick = () => { page = 1; load(); }; document.querySelector('#collect').onclick = collectQuotes; document.querySelector('#collect-market').onclick = collectQuotes; document.querySelector('#search').onkeydown = (event) => { if (event.key === 'Enter') { page = 1; load(); } }; document.querySelector('#search').onkeydown = (event) => { if (event.key === 'Enter') { page = 1; load(); } }; document.querySelector('#previous').onclick = () => { page -= 1; load(); }; document.querySelector('#next').onclick = () => { page += 1; load(); }; document.querySelector('#research-start').onclick = startResearch; document.querySelector('#feedback-send').onclick = sendFeedback; document.querySelector('#track-start').onclick = startTrack; document.querySelector('#track-feedback-send').onclick = sendTrackFeedback; document.querySelector('#chart-mode-line').onclick = () => { chartMode = 'line'; drawChart(); }; document.querySelector('#chart-mode-k').onclick = () => { chartMode = 'k'; drawChart(); }; document.querySelector('#chart-collect').onclick = collectBars; document.querySelectorAll('nav button').forEach((button) => button.onclick = () => { if (button.dataset.view === 'watchlist') loadWatchlist(); else if (button.dataset.view === 'profile') loadProfile(); else if (button.dataset.view === 'paper') loadPaper(); else view('market'); }); document.querySelector('.back').onclick = () => view('market'); loadStatus(); load();
