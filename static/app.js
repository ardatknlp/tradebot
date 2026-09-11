const $ = s => document.querySelector(s);
const fmt = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? '–' : Number(v).toLocaleString('tr-TR', {minimumFractionDigits: d, maximumFractionDigits: d});
const pn = v => `<span class="${v >= 0 ? 'pos' : 'neg'}">${v >= 0 ? '+' : ''}${fmt(v)}</span>`;
const px = (v) => v >= 1000 ? fmt(v, 1) : v >= 10 ? fmt(v, 3) : fmt(v, 5);
const ts = t => new Date(t * 1000).toLocaleString('tr-TR', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'});

async function api(path, method = 'GET', body) {
  const r = await fetch(path, {method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  return r.json();
}

function drawLine(canvas, points, startVal) {
  const ctx = canvas.getContext('2d');
  const base = Number(canvas.dataset.h || (canvas.dataset.h = canvas.getAttribute('height')));
  canvas.style.height = base + 'px';
  const W = canvas.width = canvas.clientWidth * 2, H = canvas.height = base * 2;
  ctx.clearRect(0, 0, W, H);
  if (!points || points.length < 2) { ctx.fillStyle = '#8b95ab'; ctx.font = '24px sans-serif'; ctx.fillText('Veri bekleniyor…', 20, H / 2); return; }
  const ys = points.map(p => p[1]);
  let min = Math.min(...ys, startVal ?? Infinity), max = Math.max(...ys, startVal ?? -Infinity);
  if (max === min) { max += 1; min -= 1; }
  const pad = 10, X = i => pad + (W - 2 * pad) * i / (points.length - 1), Y = v => H - pad - (H - 2 * pad) * (v - min) / (max - min);
  if (startVal) { ctx.strokeStyle = '#3a4460'; ctx.setLineDash([6, 6]); ctx.beginPath(); ctx.moveTo(0, Y(startVal)); ctx.lineTo(W, Y(startVal)); ctx.stroke(); ctx.setLineDash([]); }
  const last = ys[ys.length - 1];
  ctx.strokeStyle = last >= (startVal ?? ys[0]) ? '#2ecc71' : '#ff5c5c'; ctx.lineWidth = 3; ctx.beginPath();
  points.forEach((p, i) => i ? ctx.lineTo(X(i), Y(p[1])) : ctx.moveTo(X(i), Y(p[1]))); ctx.stroke();
  ctx.fillStyle = '#8b95ab'; ctx.font = '22px sans-serif';
  ctx.fillText(fmt(max), 8, 26); ctx.fillText(fmt(min), 8, H - 12);
}

let currentMode = 'demo';
async function refresh() {
  let s;
  try { s = await api('/api/status'); } catch (e) { return; }
  currentMode = s.mode;
  const mb = $('#modeBadge'); mb.textContent = s.mode.toUpperCase(); mb.className = 'badge ' + (s.mode === 'live' ? 'red' : s.mode === 'testnet' ? 'yel' : '');
  const rb = $('#runBadge'); rb.textContent = s.running ? 'ÇALIŞIYOR' : 'DURDU'; rb.className = 'badge ' + (s.running ? 'green' : 'gray');
  $('#liveWarn').hidden = s.mode !== 'live';
  $('#errBar').hidden = !s.last_error; $('#errBar').textContent = s.last_error ? 'Son hata: ' + s.last_error : '';
  $('#btnStart').disabled = s.running; $('#btnStop').disabled = !s.running;

  $('#cBalance').textContent = fmt(s.balance) + ' USDT';
  $('#cStart').textContent = s.mode === 'demo' ? `Başlangıç: ${fmt(s.start_balance)} USDT · ${s.leverage}x` : `${s.leverage}x kaldıraç`;
  $('#cEquity').textContent = fmt(s.equity) + ' USDT';
  $('#cUnreal').innerHTML = 'Gerçekleşmemiş: ' + pn(s.unrealized);
  $('#cNet').innerHTML = pn(s.stats.net_pnl) + ' USDT';
  $('#cGross').innerHTML = 'Brüt: ' + pn(s.stats.gross_pnl);
  $('#cFees').innerHTML = `<span class="neg">-${fmt(s.stats.fees + s.stats.funding)}</span> USDT`;
  $('#cFeeRate').textContent = `Komisyon ${fmt(s.stats.fees)} · Fonlama ${fmt(s.stats.funding)} · gidiş-dönüş %${fmt(s.fee_round_trip_pct, 3)}`;
  $('#cWin').textContent = `${s.stats.trades} / %${fmt(s.stats.win_rate, 1)}`;
  $('#cTrades').textContent = `${s.stats.wins} kazanan · ${s.stats.losses} kaybeden`;
  $('#cDaily').innerHTML = pn(s.guard.daily_realized) + ' USDT';
  const g = s.guard; let gtxt = g.halted_reason ? '⛔ ' + g.halted_reason : 'Limitler normal';
  if (g.cooldown_until * 1000 > Date.now()) gtxt = `⏸ soğuma: ${Math.ceil((g.cooldown_until * 1000 - Date.now()) / 60000)} dk`;
  $('#cGuard').textContent = gtxt + ` · ardışık kayıp ${g.consecutive_losses}`;

  drawLine($('#eqChart'), s.equity_history, s.mode === 'demo' ? s.start_balance : null);

  const mt = $('#tblMarket tbody'); mt.innerHTML = '';
  $('#uniInfo').textContent = `(${s.symbols.length} coin)`;
  for (const sym of s.symbols) {
    const sn = s.snapshot[sym] || {}, p = s.prices[sym];
    const atrp = sn.atr && p ? sn.atr / p * 100 : null;
    mt.innerHTML += `<tr><td>${sym}</td><td>${p ? px(p) : '–'}</td><td class="${sn.trend === 'YUKARI' ? 'pos' : sn.trend ? 'neg' : ''}">${sn.trend || '–'}</td><td class="${sn.htf === '↑' ? 'pos' : sn.htf === '↓' ? 'neg' : ''}">${sn.htf || '–'}</td><td>${fmt(sn.adx, 1)}</td><td>${fmt(sn.rsi, 1)}</td><td>${fmt(atrp, 3)}</td><td>${s.funding[sym] !== undefined ? fmt(s.funding[sym] * 100, 4) + '%' : '–'}</td></tr>`;
  }

  const pend = s.pending || [];
  $('#pendPanel').hidden = pend.length === 0;
  const pt0 = $('#tblPend tbody'); pt0.innerHTML = '';
  for (const o of pend) {
    pt0.innerHTML += `<tr><td>${o.symbol}</td><td class="${o.side === 'LONG' ? 'pos' : 'neg'}">${o.side}</td><td>${o.qty}</td><td>${px(o.limit_price)}</td><td>${o.mark_price ? px(o.mark_price) : '–'}</td><td>${px(o.stop)}</td><td>${px(o.take_profit)}</td><td>${ts(o.expires_at)}</td><td><button class="btn sm" onclick="closePos('${o.symbol}')">İptal</button></td></tr>`;
  }

  const pt = $('#tblPos tbody'); pt.innerHTML = '';
  $('#posEmpty').hidden = s.positions.length > 0;
  for (const p of s.positions) {
    pt.innerHTML += `<tr><td>${p.symbol}</td><td class="${p.side === 'LONG' ? 'pos' : 'neg'}">${p.side}</td><td>${p.qty}</td><td>${px(p.entry)}</td><td>${p.mark_price ? px(p.mark_price) : '–'}</td><td>${px(p.stop)}</td><td>${px(p.take_profit)}</td><td>${p.unrealized_pnl !== undefined ? pn(p.unrealized_pnl) : '–'}</td><td>${p.unrealized_pct !== undefined ? pn(p.unrealized_pct) + '%' : '–'}</td><td>${p.partial_done ? '✓' : ''}</td><td>${p.trail_active ? '✓' : ''}</td><td><button class="btn sm red" onclick="closePos('${p.symbol}')">Kapat</button></td></tr>`;
  }

  const trades = await api('/api/trades?mode=' + s.mode);
  const tt = $('#tblTrades tbody'); tt.innerHTML = '';
  $('#trEmpty').hidden = trades.length > 0;
  for (const t of trades) {
    tt.innerHTML += `<tr><td>${ts(t.closed_at)}</td><td>${t.symbol}</td><td class="${t.side === 'LONG' ? 'pos' : 'neg'}">${t.side}</td><td>${t.qty}</td><td>${px(t.entry)}</td><td>${px(t.exit)}</td><td>${pn(t.gross_pnl)}</td><td class="neg">-${fmt(t.fees)}</td><td>${fmt(-t.funding)}</td><td>${pn(t.net_pnl)}</td><td>${t.reason}</td></tr>`;
  }

  const logs = await api('/api/logs');
  const lb = $('#logBox'); const atBottom = lb.scrollTop + lb.clientHeight >= lb.scrollHeight - 20;
  lb.textContent = logs.join('\n'); if (atBottom) lb.scrollTop = lb.scrollHeight;
}

async function closePos(sym) { if (!confirm(sym + ' pozisyonu kapatılsın / emri iptal edilsin mi?')) return; const r = await api('/api/close/' + sym, 'POST'); if (!r.ok) alert(r.msg); refresh(); }

$('#btnStart').onclick = async () => {
  if (currentMode === 'live' && !confirm('GERÇEK PARA ile işlem başlatılacak. Emin misiniz?')) return;
  const r = await api('/api/start', 'POST'); if (!r.ok) alert(r.msg); refresh();
};
$('#btnStop').onclick = async () => { await api('/api/stop', 'POST'); refresh(); };
$('#btnReset').onclick = async () => { if (!confirm('Demo bakiye ve demo işlem geçmişi silinecek. Onaylıyor musunuz?')) return; const r = await api('/api/reset_demo', 'POST'); if (!r.ok) alert(r.msg); refresh(); };

// ---- ayarlar ----
const getPath = (o, p) => p.split('.').reduce((a, k) => a && a[k], o);
const setPath = (o, p, v) => { const ks = p.split('.'); let c = o; ks.slice(0, -1).forEach(k => c = c[k] = c[k] || {}); c[ks.at(-1)] = v; };
async function loadConfig() {
  const c = await api('/api/config');
  for (const el of $('#cfgForm').elements) {
    if (!el.name) continue;
    const v = getPath(c, el.name);
    if (el.type === 'checkbox') el.checked = !!v;
    else if (el.name === 'symbols') el.value = (v || []).join(',');
    else if (!el.name.includes('api')) el.value = v ?? '';
  }
  $('#keyInfo').textContent = `Kayıtlı anahtar: gerçek ${c.api_key_set ? '✓' : '✗'} · testnet ${c.testnet_api_key_set ? '✓' : '✗'}`;
  $('#btInterval').value = c.interval;
  try { const st = await api('/api/status'); $('#btSymbols').value = (st.symbols || c.symbols).join(','); } catch (e) { $('#btSymbols').value = c.symbols.join(','); }
}
$('#btnSave').onclick = async () => {
  const out = {};
  for (const el of $('#cfgForm').elements) {
    if (!el.name) continue;
    let v;
    if (el.type === 'checkbox') v = el.checked;
    else if (el.name === 'symbols') v = el.value.split(',').map(s => s.trim().toUpperCase()).filter(Boolean);
    else if (el.type === 'number') v = Number(el.value);
    else v = el.value;
    setPath(out, el.name, v);
  }
  if (out.mode === 'live' && !out.live_trading_confirmed) { alert('Gerçek mod için onay kutusunu işaretleyin.'); return; }
  const r = await api('/api/config', 'POST', out);
  $('#saveMsg').textContent = r.ok ? 'Kaydedildi ✓' : 'Hata: ' + r.msg;
  setTimeout(() => $('#saveMsg').textContent = '', 3000);
  loadConfig(); refresh();
};

// ---- backtest ----
$('#btnBt').onclick = async () => {
  const btn = $('#btnBt'); btn.disabled = true; btn.textContent = 'Çalışıyor…';
  $('#btResult').innerHTML = 'Veri indiriliyor ve simülasyon yapılıyor…';
  try {
    const r = await api('/api/backtest', 'POST', {symbols: $('#btSymbols').value.split(','), interval: $('#btInterval').value, days: Number($('#btDays').value)});
    if (!r.ok) { $('#btResult').textContent = 'Hata: ' + r.msg; return; }
    const x = r.result;
    $('#btResult').innerHTML = `
      <div>Aralık: <b>${x.from} → ${x.to}</b></div><div>Periyot: <b>${x.interval}</b></div>
      <div>Başlangıç: <b>${fmt(x.start_balance)}</b></div><div>Bitiş: <b>${fmt(x.end_balance)}</b> (${pn(x.net_pct)}%)</div>
      <div>Brüt PnL: ${pn(x.gross_pnl)}</div><div>Net PnL: ${pn(x.net_pnl)}</div>
      <div>Komisyon: <span class="neg">-${fmt(x.total_fees)}</span></div><div>Fonlama (tahmini): <span class="neg">${fmt(-x.total_funding)}</span></div>
      <div>İşlem: <b>${x.trades}</b> (${x.wins}K / ${x.losses}Z)</div><div>Kazanma: <b>%${x.win_rate}</b></div>
      <div>Profit factor: <b>${x.profit_factor}</b></div><div>Maks. düşüş: <b class="neg">%${x.max_drawdown_pct}</b></div>
      <div>Ort. net/işlem: ${pn(x.avg_net_per_trade)}</div><div>Sinyal: ${x.signals} · limit dolum: ${x.limit_fills}</div>
      <div style="grid-column:1/3">Sembol bazında: ${Object.entries(x.per_symbol || {}).sort((a,b)=>b[1].net-a[1].net).map(([k,v])=>`${k} ${v.net>=0?'+':''}${fmt(v.net)} (${v.trades})`).join(' · ')}</div>`;
    const c = $('#btChart'); c.hidden = false; drawLine(c, x.equity_curve, x.start_balance);
  } finally { btn.disabled = false; btn.textContent = 'Çalıştır'; }
};

loadConfig(); refresh(); setInterval(refresh, 4000);
