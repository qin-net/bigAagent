let page = 1;
let total = 0;
const size = 50;
const text = (value) => value === null || value === undefined ? '-' : value;
const money = (value) => value === null || value === undefined ? '-' : `${(value / 100000000).toFixed(2)} 亿`;

async function load() {
  const q = document.querySelector('#search').value.trim();
  const params = new URLSearchParams({ page, size, q, sort: 'turnover', order: 'desc' });
  const response = await fetch(`/api/v1/quotes?${params}`);
  const data = await response.json();
  total = data.total;
  document.querySelector('#quotes').innerHTML = data.items.map((item) => `<tr><td>${item.stock_code}</td><td>${item.name}</td><td>${text(item.market)}</td><td>${text(item.price)}</td><td class="${item.change_pct >= 0 ? 'up' : 'down'}">${text(item.change_pct)}%</td><td>${money(item.turnover)}</td></tr>`).join('') || '<tr><td colspan="6">暂无可展示行情。请先运行 collect-once。</td></tr>';
  document.querySelector('#page').textContent = `第 ${page} 页，共 ${total} 条`;
  document.querySelector('#previous').disabled = page === 1;
  document.querySelector('#next').disabled = page * size >= total;
}

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
loadStatus();
load();
