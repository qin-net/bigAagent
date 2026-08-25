let page = 1;
let total = 0;
const size = 50;
const text = (value) => value === null || value === undefined ? '-' : value;
const money = (value) => value === null || value === undefined ? '-' : `${(value / 100000000).toFixed(2)} 亿`;
const api = (path, options) => fetch(`/api/v1${path}`, options).then((response) => response.json());

function view(name) {
  ['market', 'detail', 'watchlist'].forEach((item) => document.querySelector(`#${item}-view`).hidden = item !== name);
}

async function load() {
  const q = document.querySelector('#search').value.trim();
  const params = new URLSearchParams({ page, size, q, sort: 'turnover', order: 'desc' });
  const response = await fetch(`/api/v1/quotes?${params}`);
  const data = await response.json();
  total = data.total;
  document.querySelector('#quotes').innerHTML = data.items.map((item) => `<tr data-code="${item.stock_code}"><td>${item.stock_code}</td><td>${item.name}</td><td>${text(item.market)}</td><td>${text(item.price)}</td><td class="${item.change_pct >= 0 ? 'up' : 'down'}">${text(item.change_pct)}%</td><td>${money(item.turnover)}</td></tr>`).join('') || '<tr><td colspan="6">暂无可展示行情。请先运行 collect-once。</td></tr>';
  document.querySelectorAll('#quotes tr[data-code]').forEach((row) => row.onclick = () => detail(row.dataset.code));
  document.querySelector('#page').textContent = `第 ${page} 页，共 ${total} 条`;
  document.querySelector('#previous').disabled = page === 1;
  document.querySelector('#next').disabled = page * size >= total;
}

function chart(bars) {
  const canvas = document.querySelector('#chart'); const ctx = canvas.getContext('2d'); ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!bars.length) return; const values = bars.flatMap((bar) => [bar.high, bar.low]).filter((value) => value !== null); const min = Math.min(...values); const max = Math.max(...values); const width = canvas.width / bars.length;
  bars.forEach((bar, index) => { const y = (value) => canvas.height - 20 - ((value - min) / (max - min || 1)) * (canvas.height - 40); const up = bar.close >= bar.open; ctx.strokeStyle = up ? '#fb7185' : '#34d399'; ctx.fillStyle = ctx.strokeStyle; const x = index * width + width / 2; ctx.beginPath(); ctx.moveTo(x, y(bar.high)); ctx.lineTo(x, y(bar.low)); ctx.stroke(); const top = y(Math.max(bar.open, bar.close)); const height = Math.max(1, Math.abs(y(bar.open) - y(bar.close))); ctx.fillRect(x - Math.max(1, width * .3), top, Math.max(2, width * .6), height); });
}

async function detail(code) {
  view('detail');
  const [quote, bars, notices] = await Promise.all([api(`/quotes/${code}`), api(`/bars/${code}`), api(`/notices/${code}`)]);
  const item = quote.item; document.querySelector('#detail').innerHTML = `<h2>${item.name} <small>${code}</small></h2><p class="price">${text(item.price)} <span class="${item.change_pct >= 0 ? 'up' : 'down'}">${text(item.change_pct)}%</span></p><button id="watch">加入自选</button><p id="deep-status">${bars.items.length ? `K 线数据更新于 ${bars.items.at(-1).updated_at}` : '日 K 与公告正在后台补充...'}</p>`;
  document.querySelector('#watch').onclick = async () => { await api(`/watchlist/${code}`, { method: 'PUT' }); document.querySelector('#watch').textContent = '已加入自选'; };
  if (!bars.items.length && !notices.items.length) await api(`/stocks/${code}/refresh-request`, { method: 'POST' });
  chart(bars.items); document.querySelector('#notices').innerHTML = notices.items.map((notice) => `<li>${text(notice.published_at)} ${notice.url ? `<a href="${notice.url}" target="_blank">${notice.title}</a>` : notice.title}</li>`).join('') || '<li>暂无公告，已加入后台采集队列。</li>';
}

async function loadWatchlist() { view('watchlist'); const data = await api('/watchlist'); document.querySelector('#watchlist').innerHTML = data.items.map((item) => `<p><button class="stock" data-code="${item.stock_code}">${item.stock_code} ${text(item.name)}</button> ${text(item.price)} <span class="${item.change_pct >= 0 ? 'up' : 'down'}">${text(item.change_pct)}%</span></p>`).join('') || '<p>尚未加入自选股。</p>'; document.querySelectorAll('.stock').forEach((button) => button.onclick = () => detail(button.dataset.code)); }

async function loadStatus() {
  const response = await fetch('/api/v1/meta/ingest');
  const status = await response.json();
  const asOf = status.quotes_as_of ? new Date(status.quotes_as_of).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '暂无成功采集';
  document.querySelector('#status').textContent = status.stale ? `数据可能过期，最近成功：${asOf}` : `数据截至：${asOf}，覆盖 ${status.stock_count} 只股票`;
  document.querySelector('#status').className = status.stale ? 'stale' : '';
}

document.querySelector('#refresh').onclick = () => { page = 1; load(); };
document.querySelector('#search').onkeydown = (event) => { if (event.key === 'Enter') { page = 1; load(); } };
document.querySelector('#previous').onclick = () => { page -= 1; load(); };
document.querySelector('#next').onclick = () => { page += 1; load(); };
document.querySelectorAll('nav button').forEach((button) => button.onclick = () => button.dataset.view === 'watchlist' ? loadWatchlist() : view('market'));
document.querySelector('.back').onclick = () => view('market');
loadStatus();
load();
