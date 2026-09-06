function performanceChart(series, width = 920) {
  const points = (series.points || []).slice().sort((a,b) => Date.parse(a.time) - Date.parse(b.time));
  if (!points.length) return `<div class="empty">${series.holding ? '暂无持仓期间采样。开始采集后显示真实盈亏路径，历史缺失时段无法还原。' : '暂无已核实的到期结束组合。提前平仓和持仓中的组合仅在下方展示。'}</div>`;
  const height = 280, left = width < 600 ? 60 : 80, right = 24, top = 24, bottom = 44;
  const values = [0, ...points.map(point => Number(point.cumulative))];
  const min = values.reduce((a,b)=>Math.min(a,b),0), max = values.reduce((a,b)=>Math.max(a,b),0), pad = Math.max((max - min) * .15, 1);
  const firstTime = Date.parse(points[0].time), lastTime = Date.parse(points.at(-1).time);
  const x = point => firstTime === lastTime ? (left + width - right) / 2 : left + (Date.parse(point.time) - firstTime) / (lastTime - firstTime) * (width - left - right);
  const y = value => top + (max + pad - value) / (max - min + pad * 2) * (height - top - bottom);
  const n = value => Number(value.toFixed(2));
  let path = '', gaps = 0;
  points.forEach((point, i) => {
    const previous = points[i-1];
    if (!previous || Date.parse(point.time) - Date.parse(previous.time) > (series.sample_seconds || 60) * 3000) {
      path += ` M${n(x(point))},${n(y(point.cumulative))}`;
      if (previous) gaps++;
    } else {
      // Bounded cubic interpolation passes through actual samples without inventing extrema.
      const third = (x(point) - x(previous)) / 3;
      path += ` C${n(x(previous)+third)},${n(y(previous.cumulative))} ${n(x(point)-third)},${n(y(point.cumulative))} ${n(x(point))},${n(y(point.cumulative))}`;
    }
  });
  const labelTimes = [...new Set([firstTime, firstTime + (lastTime - firstTime) / 2, lastTime])];
  const labels = labelTimes.map((time,i) => `<text x="${n(x({time:new Date(time).toISOString()}))}" y="${height - 12}" text-anchor="${labelTimes.length === 1 ? 'middle' : i === 0 ? 'start' : i === labelTimes.length - 1 ? 'end' : 'middle'}">${new Date(time).toISOString().slice(firstTime === lastTime || lastTime-firstTime<86400000 ? 11 : 5, firstTime === lastTime || lastTime-firstTime<86400000 ? 16 : 10)}</text>`).join('');
  const grid = [min, (min + max) / 2, max].filter((v, i, all) => all.indexOf(v) === i).map(value =>
    `<line class="pp-grid" x1="${left}" x2="${width-right}" y1="${n(y(value))}" y2="${n(y(value))}"/><text x="${left-10}" y="${n(y(value)+4)}" text-anchor="end">${num(value, Math.abs(value) < 10 ? 2 : 0)}</text>`).join('');
  const dots = points.map((point, i) => (i % Math.max(1, Math.ceil(points.length / 80)) === 0 || i === points.length - 1 || point.terminal) ? `<circle class="performance-point" cx="${n(x(point))}" cy="${n(y(point.cumulative))}" r="${point.terminal ? 4 : 2}"><title>${esc(point.group_id || '')} · ${esc(point.time)} · ${point.terminal ? '结束净收益' : '真实采样'} ${money(point.pnl)} · 曲线值 ${money(point.cumulative)} ${esc(series.currency)}</title></circle>` : '').join('');
  return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${series.holding ? '单次组合持仓盈亏采样曲线' : '仅到期结束组合综合收益采样曲线'}，横轴为实际时间。${points.length} 个记录点，缺失区间断开显示。">${grid}<line class="pp-zero" x1="${left}" x2="${width-right}" y1="${n(y(0))}" y2="${n(y(0))}"/><path class="pp-line" d="${path}"/>${dots}${labels}</svg><p class="performance-sample-note">${points.filter(p=>!p.terminal).length} 个真实采样点 · UTC${gaps ? ` · ${gaps} 段采样缺口已断开` : ''}${points.every(p=>p.terminal) ? ' · 历史无持仓采样，仅显示结束净收益点' : ''}</p>`;
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
  const eligible = new Set((payload.groups || []).filter(group=>group.eligible === true).map(group=>group.id));
  const series = payload.series?.find(item => item.currency === selected && item.points.every(point=>eligible.has(point.group_id))) || {currency:selected,points:[],total_pnl:0,closed_count:0,wins:0,max_drawdown:0};
  $('performanceMode').textContent = payload.network === 'testnet' ? 'TESTNET · 测试网业绩' : 'MAINNET · 实盘业绩';
  const from = payload.history_start_ms ? new Date(payload.history_start_ms).toISOString().slice(0, 10) : '--';
  $('performanceStatus').textContent = payload.error || (payload.syncing ? `正在补齐历史 · 已同步至 ${new Date(payload.history_cursor_ms).toISOString().slice(0,10)}，统计暂为部分记录` : payload.credentials_available ? `台账起始 ${from} UTC · 自动同步成交与手续费` : '未配置交易所凭据 · 展示已保存台账');
  $('performanceStatus').dataset.warning = Boolean(payload.error || payload.syncing);
  $('performanceTotal').textContent = series.closed_count ? money(series.total_pnl) : '--';
  $('performanceTotal').className = series.total_pnl >= 0 ? 'profit' : 'loss';
  $('performanceClosed').textContent = String(series.closed_count);
  $('performanceWinRate').textContent = series.closed_count ? `${num(series.wins / series.closed_count * 100, 1)}%` : '--';
  $('performanceDrawdown').textContent = series.closed_count ? money(series.max_drawdown) : '--';
  $('performanceChart').innerHTML = performanceChart({...series, sample_seconds:payload.sample_seconds}, Math.max(300, Math.min(920, $('performanceChart').clientWidth || 920)));
  const groups = (payload.groups || []).filter(group => group.currency === selected);
  const pending = groups.filter(group => group.status === 'pending').length;
  $('performanceCoverage').textContent = `${groups.length} 次组合开仓 · ${groups.filter(g=>g.eligible).length} 组到期纳入 · ${groups.filter(g=>g.closure_kind === 'early').length} 组提前平仓排除 · ${pending} 组待核对${payload.unassigned_closes ? ` · ${payload.unassigned_closes} 笔平仓尚未可靠归属` : ''}`;
  const date = value => value ? new Date(value).toISOString().slice(0,16).replace('T',' ') : '--';
  const metric = (label, value, tone = '') => `<div><span>${label}</span><strong class="${tone}">${value == null ? '--' : money(value)}</strong></div>`;
  root.innerHTML = groups.length ? groups.map(group => `<details class="performance-group" data-group="${esc(group.id)}" ${expanded.has(group.id) ? 'open' : ''}><summary><span class="performance-identity"><strong>${esc(group.id)}</strong><small>${date(group.opened_at)} UTC · ${group.leg_count} 腿</small></span><span class="performance-result"><span class="source-tag">${(group.status === 'pending' ? '待核对' : group.status === 'open' ? (group.closure_kind === 'early' ? '提前减仓 · 持仓中' : '持仓中') : group.closure_kind === 'expiry' ? '到期结束' : group.closure_kind === 'early' ? '提前平仓' : '结束待验证') || '待核对'}</span><b class="${group.net_pnl >= 0 ? 'profit' : 'loss'}">${group.net_pnl == null ? '--' : money(group.net_pnl)}</b><small>${group.status === 'closed' ? '组合净收益' : '已实现部分'}</small></span></summary><div class="performance-detail"><div class="performance-metrics">${metric('开仓权利金净额',group.open_premium)}${metric('开仓手续费',group.open_fee)}${metric('平仓手续费',group.close_fee)}${metric('交割手续费',group.delivery_fee)}${metric('已实现净收益',group.net_pnl,group.net_pnl>=0?'profit':'loss')}${metric('剩余持仓浮盈亏（未扣费）',group.floating_pnl,group.floating_pnl>=0?'profit':'loss')}</div><p class="performance-detail-note">${group.status === 'closed' ? `结束于 ${date(group.closed_at)} UTC · ` : ''}${group.source === 'exchange_closed_positions' ? '收益已按交易所平仓／交割记录核实，包含手续费。' : '根据实际成交逐笔匹配开平仓，手续费按匹配数量分摊。'}${group.eligible ? ' 本组已纳入综合统计。' : ` 本组不纳入综合统计：${esc(group.exclusion_reason || '尚未核实到期结束')}。`}</p>${group.issues?.length ? `<p class="pp-warning">${group.issues.map(esc).join('；')}</p>` : ''}<h4 class="performance-sample-title">持仓盈亏过程<span>每 ${payload.sample_seconds || 60} 秒采样 · 已扣已知交易费用</span></h4><div class="pp-chart">${performanceChart({holding:true,currency:group.currency,points:(group.samples || []).map(p=>({...p,group_id:group.id,cumulative:p.pnl})),sample_seconds:payload.sample_seconds},Math.max(300,Math.min(920,root.clientWidth || 920)))}</div><div class="performance-fills">${(group.fills || []).map(fill=>`<div><strong>${esc(fill.symbol)}</strong><span>${esc(fill.side)} · ${num(fill.exec_qty,4)} BTC × ${money(fill.exec_price)}</span><span>手续费 ${num(fill.exec_fee,6)} ${esc(fill.fee_currency)}</span><small>${date(fill.exec_time)} UTC</small></div>`).join('') || '<p class="muted">逐笔成交历史尚未补齐，已核实的交割收益以交易所记录为准。</p>'}</div></div></details>`).join('') : '<div class="empty">暂无该币种的实盘组合记录。模拟开仓不会计入实盘业绩。</div>';
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
