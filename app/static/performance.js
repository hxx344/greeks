function performanceChart(series, width = 920) {
  const points = series.points || [];
  if (!points.length) return '<div class="empty">暂无已核实的完整平仓组合，完成后将自动累计收益。</div>';
  const height = 280, left = width < 600 ? 60 : 80, right = 24, top = 24, bottom = 44;
  const values = [0, ...points.map(point => Number(point.cumulative))];
  const min = Math.min(...values), max = Math.max(...values), pad = Math.max((max - min) * .15, 1);
  const x = i => left + i / points.length * (width - left - right);
  const y = value => top + (max + pad - value) / (max - min + pad * 2) * (height - top - bottom);
  const n = value => Number(value.toFixed(2));
  let path = `M${left},${n(y(0))}`;
  points.forEach((point, i) => { path += ` H${n(x(i + 1))} V${n(y(point.cumulative))}`; });
  const labels = [...new Set([0, Math.floor(points.length / 2), points.length])].map(i =>
    `<text x="${n(x(i))}" y="${height - 12}" text-anchor="${i === 0 ? 'start' : i === points.length ? 'end' : 'middle'}">${i === 0 ? '起点 0' : new Date(points[i - 1].time).toISOString().slice(5, 10)}</text>`).join('');
  const grid = [min, (min + max) / 2, max].filter((v, i, all) => all.indexOf(v) === i).map(value =>
    `<line class="pp-grid" x1="${left}" x2="${width-right}" y1="${n(y(value))}" y2="${n(y(value))}"/><text x="${left-10}" y="${n(y(value)+4)}" text-anchor="end">${num(value, Math.abs(value) < 10 ? 2 : 0)}</text>`).join('');
  const dots = points.map((point, i) => `<circle class="performance-point" cx="${n(x(i+1))}" cy="${n(y(point.cumulative))}" r="4"><title>${esc(point.group_id)} · ${esc(point.time)} · 本次 ${money(point.pnl)} · 累计 ${money(point.cumulative)} ${esc(series.currency)}</title></circle>`).join('');
  return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="已结束组合累计净收益曲线，按组合平仓顺序。当前累计 ${num(series.total_pnl)} ${esc(series.currency)}">${grid}<line class="pp-zero" x1="${left}" x2="${width-right}" y1="${n(y(0))}" y2="${n(y(0))}"/><path class="pp-line" d="${path}"/>${dots}${labels}</svg>`;
}

function renderPerformance(payload) {
  const root = $('performanceGroups');
  const expanded = new Set([...(root.querySelectorAll?.('details[open]') || [])].map(el => el.dataset.group));
  const currencySelect = $('performanceCurrency');
  const currency = currencySelect.value;
  const currencies = [...new Set([...(payload.series || []).map(item => item.currency), ...(payload.groups || []).map(item => item.currency)])];
  currencySelect.innerHTML = currencies.map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join('') || '<option value="USDT">USDT</option>';
  currencySelect.value = currencies.includes(currency) ? currency : (currencies[0] || 'USDT');
  const selected = currencySelect.value;
  const series = payload.series?.find(item => item.currency === selected) || {currency:selected,points:[],total_pnl:0,closed_count:0,wins:0,max_drawdown:0};
  $('performanceMode').textContent = payload.network === 'testnet' ? 'TESTNET · 测试网业绩' : 'MAINNET · 实盘业绩';
  const from = payload.history_start_ms ? new Date(payload.history_start_ms).toISOString().slice(0, 10) : '--';
  $('performanceStatus').textContent = payload.error || (payload.syncing ? `正在补齐历史 · 已同步至 ${new Date(payload.history_cursor_ms).toISOString().slice(0,10)}，统计暂为部分记录` : payload.credentials_available ? `台账起始 ${from} UTC · 自动同步成交与手续费` : '未配置交易所凭据 · 展示已保存台账');
  $('performanceStatus').dataset.warning = Boolean(payload.error || payload.syncing);
  $('performanceTotal').textContent = series.closed_count ? money(series.total_pnl) : '--';
  $('performanceTotal').className = series.total_pnl >= 0 ? 'profit' : 'loss';
  $('performanceClosed').textContent = String(series.closed_count);
  $('performanceWinRate').textContent = series.closed_count ? `${num(series.wins / series.closed_count * 100, 1)}%` : '--';
  $('performanceDrawdown').textContent = series.closed_count ? money(series.max_drawdown) : '--';
  $('performanceChart').innerHTML = performanceChart(series, Math.max(300, Math.min(920, $('performanceChart').clientWidth || 920)));
  const groups = (payload.groups || []).filter(group => group.currency === selected);
  const pending = groups.filter(group => group.status === 'pending').length;
  $('performanceCoverage').textContent = `${groups.length} 次组合开仓 · ${pending} 组待核对${payload.unassigned_closes ? ` · ${payload.unassigned_closes} 笔平仓尚未可靠归属` : ''}`;
  const date = value => value ? new Date(value).toISOString().slice(0,16).replace('T',' ') : '--';
  const metric = (label, value, tone = '') => `<div><span>${label}</span><strong class="${tone}">${value == null ? '--' : money(value)}</strong></div>`;
  root.innerHTML = groups.length ? groups.map(group => `<details class="performance-group" data-group="${esc(group.id)}" ${expanded.has(group.id) ? 'open' : ''}><summary><span class="performance-identity"><strong>${esc(group.id)}</strong><small>${date(group.opened_at)} UTC · ${group.leg_count} 腿</small></span><span class="performance-result"><span class="source-tag">${({closed:'已结束',open:'持仓中',pending:'待核对'})[group.status] || '待核对'}</span><b class="${group.net_pnl >= 0 ? 'profit' : 'loss'}">${group.net_pnl == null ? '--' : money(group.net_pnl)}</b><small>${group.status === 'closed' ? '组合净收益' : '已实现部分'}</small></span></summary><div class="performance-detail"><div class="performance-metrics">${metric('开仓权利金净额',group.open_premium)}${metric('开仓手续费',group.open_fee)}${metric('平仓手续费',group.close_fee)}${metric('交割手续费',group.delivery_fee)}${metric('已实现净收益',group.net_pnl,group.net_pnl>=0?'profit':'loss')}${metric('剩余持仓浮盈亏（未扣费）',group.floating_pnl,group.floating_pnl>=0?'profit':'loss')}</div><p class="performance-detail-note">${group.status === 'closed' ? `结束于 ${date(group.closed_at)} UTC · ` : ''}${group.source === 'exchange_closed_positions' ? '收益已按交易所平仓／交割记录核实，包含手续费。' : '根据实际成交逐笔匹配开平仓，手续费按匹配数量分摊。'}${group.status !== 'closed' ? ' 本组尚未计入累计收益曲线。' : ''}</p>${group.issues?.length ? `<p class="pp-warning">${group.issues.map(esc).join('；')}</p>` : ''}<div class="performance-fills">${(group.fills || []).map(fill=>`<div><strong>${esc(fill.symbol)}</strong><span>${esc(fill.side)} · ${num(fill.exec_qty,4)} BTC × ${money(fill.exec_price)}</span><span>手续费 ${num(fill.exec_fee,6)} ${esc(fill.fee_currency)}</span><small>${date(fill.exec_time)} UTC</small></div>`).join('') || '<p class="muted">逐笔成交历史尚未补齐，已核实的交割收益以交易所记录为准。</p>'}</div></div></details>`).join('') : '<div class="empty">暂无该币种的实盘组合记录。模拟开仓不会计入实盘业绩。</div>';
}

async function loadPerformance() {
  if (window.__performanceLoading) return;
  window.__performanceLoading = true;
  try {
    const payload = await getJson('/api/dashboard/performance');
    window.__performance = payload;
    renderPerformance(payload);
  } catch (error) {
    $('performanceStatus').textContent = '收益台账同步失败，等待重试；下方如有数据则为上次快照。';
    $('performanceStatus').dataset.warning = true;
  } finally { window.__performanceLoading = false; }
}
