/**
 * 22B Strategy Engine Dashboard — Real-time WebSocket client
 *
 * Responsibilities:
 *  - Connect to /ws/live and maintain connection with auto-reconnect
 *  - Handle incoming events: snapshot, ticker, regime, candle, funding, system_mode, etc.
 *  - Update DOM elements in-place without full page refresh
 *  - Show toast notifications for regime changes
 */

"use strict";

// ============================================================
// Config
// ============================================================
function getWsUrl() {
  const configured = window.__WS_URL__ || "";
  // If configured URL has localhost/127.0.0.1 but we're on a different host, use current host
  if (configured.includes("localhost") || configured.includes("127.0.0.1")) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}/ws/live`;
  }
  return configured || `ws://${location.host}/ws/live`;
}
const WS_URL       = getWsUrl();
const RECONNECT_MS = 3000;
const API_INDICATOR_REFRESH_MS  = 30000;   // re-fetch indicators every 30s
const API_STRATEGY_REFRESH_MS   = 30000;   // re-fetch strategy board every 30s
const API_POSITIONS_REFRESH_MS  = 15000;   // re-fetch live positions every 15s
const API_TRADE_LOG_REFRESH_MS  = 60000;   // re-fetch trade log every 60s

// ============================================================
// State
// ============================================================
const state = {
  ws:              null,
  reconnectTimer:  null,
  connected:       false,
  lastRegime:      null,
  indicators:      {},    // symbol → { indicators, price, change_pct, funding_rate, open_interest }
  tickers:         {},    // symbol → ticker dict
  signals:         [],    // last 50 signals (newest first)
  openPositions:   [],    // open paper positions
  livePositions:   [],    // live positions from Binance
  tradeLog:        [],    // closed trades
  killSwitchActive: window.__KILL_SWITCH_ACTIVE__ || false,
  tradeLogMode:    '',    // '' | 'LIVE' | 'PAPER'
  reconcileStatus: null,
};

// ============================================================
// DOM helpers
// ============================================================
function $(id)      { return document.getElementById(id); }
function $q(sel)    { return document.querySelector(sel); }
function $qa(sel)   { return document.querySelectorAll(sel); }

function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

function setHTML(id, html) {
  const el = $(id);
  if (el) el.innerHTML = html;
}

function setClass(id, cls) {
  const el = $(id);
  if (el) el.className = cls;
}

function addFlash(el, cls = 'flash') {
  if (!el) return;
  el.classList.add(cls);
  setTimeout(() => el.classList.remove(cls), 600);
}

// ============================================================
// Time formatting (KST = UTC+9)
// ============================================================
const _KST_OPTS_TIME = { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false };
const _KST_OPTS_FULL = { timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false };

function fmtKST(val) {
  if (!val) return '—';
  const d = (val instanceof Date) ? val : new Date(val);
  if (isNaN(d)) return '—';
  return d.toLocaleString('ko-KR', _KST_OPTS_TIME);
}

function fmtKSTFull(val) {
  if (!val) return '—';
  const d = (val instanceof Date) ? val : new Date(val);
  if (isNaN(d)) return '—';
  return d.toLocaleString('ko-KR', _KST_OPTS_FULL);
}

// ============================================================
// Number formatting
// ============================================================
function fmtPrice(v, sym) {
  if (v == null) return '—';
  if (typeof v !== 'number') v = parseFloat(v);
  if (isNaN(v)) return '—';
  if (sym === 'BTCUSDT') return v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (v >= 1000) return v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (v >= 1)    return v.toFixed(4);
  return v.toFixed(6);
}

function fmtPct(v) {
  if (v == null) return '—';
  const n = parseFloat(v);
  if (isNaN(n)) return '—';
  const sign = n >= 0 ? '+' : '';
  return `${sign}${n.toFixed(2)}%`;
}

function fmtNum(v, decimals = 2) {
  if (v == null) return '—';
  const n = parseFloat(v);
  if (isNaN(n)) return '—';
  return n.toFixed(decimals);
}

function fmtVol(v) {
  if (v == null) return '—';
  const n = parseFloat(v);
  if (isNaN(n)) return '—';
  if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(2)}`;
}

function pctClass(v) {
  if (v == null) return 'neutral';
  return parseFloat(v) >= 0 ? 'positive' : 'negative';
}

function colorClass(v) {
  if (v == null) return '';
  const n = parseFloat(v);
  if (n > 0) return 'highlight-green';
  if (n < 0) return 'highlight-red';
  return '';
}

// ============================================================
// WebSocket
// ============================================================
function connect() {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) return;

  state.ws = new WebSocket(WS_URL);

  state.ws.onopen = () => {
    console.log('[WS] Connected');
    state.connected = true;
    updateWsStatus('connected');
    clearTimeout(state.reconnectTimer);
  };

  state.ws.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      handleMessage(msg);
    } catch (e) {
      console.error('[WS] Parse error:', e);
    }
  };

  state.ws.onclose = (evt) => {
    console.warn('[WS] Closed', evt.code);
    state.connected = false;
    updateWsStatus('reconnecting');
    state.reconnectTimer = setTimeout(connect, RECONNECT_MS);
  };

  state.ws.onerror = (err) => {
    console.error('[WS] Error', err);
    state.connected = false;
    updateWsStatus('disconnected');
  };
}

function updateWsStatus(status) {
  const dot  = $q('.ws-dot');
  const text = $q('.ws-status-text');
  if (!dot) return;
  dot.className = `ws-dot ${status}`;
  if (text) {
    const labels = { connected: 'Live', disconnected: 'Disconnected', reconnecting: 'Reconnecting…' };
    text.textContent = labels[status] || status;
  }
}

// ============================================================
// Message dispatcher
// ============================================================
function handleMessage(msg) {
  switch (msg.type) {
    case 'snapshot':               handleSnapshot(msg.data);           break;
    case 'ticker':                 handleTicker(msg.data);              break;
    case 'regime':                 handleRegime(msg.data);              break;
    case 'candle':                 handleCandle(msg.data);              break;
    case 'funding':                handleFunding(msg.data);             break;
    case 'open_interest':          handleOI(msg.data);                  break;
    case 'system_mode':            handleSystemMode(msg.data);          break;
    case 'exchange_status':        handleExchangeStatus(msg.data);      break;
    case 'signal':                 handleSignal(msg.data);              break;
    case 'paper_position_opened':  handlePositionOpened(msg.data);      break;
    case 'paper_position_closed':  handlePositionClosed(msg.data);      break;
    // Phase 3 — Execution
    case 'kill_switch':            handleKillSwitchUpdate(msg.data);    break;
    case 'order_update':           handleOrderUpdate(msg.data);         break;
    case 'reconcile':              handleReconcileUpdate(msg.data);     break;
    case 'account_balance':        handleBalanceUpdate(msg.data);       break;
    // Phase 4 — Panel 6
    case 'regime_interpretation':  handleRegimeInterpretation(msg.data); break;
    case 'weekly_review':          handleWeeklyReview(msg.data);         break;
    case 'daily_review':           handleDailyReview(msg.data);          break;
    case 'daily_alert_count':      handleDailyAlertCount(msg.data);      break;
    case 'recommendation_decided': handleRecommendationDecided(msg.data); break;
    // User Control events
    case 'regime_override':        _handleRegimeOverrideEvent(msg.data); break;
    case 'exchange_mode':          loadExchangeMode();                   break;
    case 'ping':                   /* keepalive — ignore */              break;
    default:
      console.debug('[WS] Unknown message type:', msg.type);
  }
}

// ============================================================
// Snapshot (initial state)
// ============================================================
function handleSnapshot(data) {
  if (!data) return;

  if (data.system_mode)  updateSystemMode(data.system_mode);
  if (data.exchange_ok != null) updateExchangeStatus(data.exchange_ok);
  updatePnL(data.daily_pnl, data.daily_pnl_pct);
  updateExposure(data.exposure_pct);
  if (data.account_balance != null || data.available_balance != null) {
    handleBalanceUpdate({
      balance: data.account_balance,
      available_balance: data.available_balance,
      margin_balance: data.margin_balance,
    });
  }

  if (data.tickers) {
    Object.entries(data.tickers).forEach(([sym, t]) => {
      state.tickers[sym] = t;
    });
  }
  if (data.funding) {
    Object.entries(data.funding).forEach(([sym, rate]) => {
      state.indicators[sym] = state.indicators[sym] || {};
      state.indicators[sym].funding_rate = rate;
    });
  }
  if (data.regime) handleRegime(data.regime);

  // Re-render indicator cards
  renderAllSymbolCards();
}

// ============================================================
// Ticker update
// ============================================================
function handleTicker(data) {
  const { symbol, ticker } = data;
  if (!symbol || !ticker) return;
  state.tickers[symbol] = ticker;

  // Update card
  updateSymbolCard(symbol);
}

// ============================================================
// Regime
// ============================================================
function handleRegime(data) {
  if (!data) return;
  const regime = data.regime;

  const old = state.lastRegime;
  state.lastRegime = regime;

  updateRegimeDisplay(data);

  if (old && old !== regime) {
    showToast('regime-change', '⚡ Regime Changed', `${old} → ${regime}`);
  }
}

function updateRegimeDisplay(data) {
  const regime = data.regime || 'UNKNOWN';

  // Header regime
  const headerRegime = $('header-regime');
  if (headerRegime) {
    headerRegime.innerHTML = `<span class="regime-badge regime-${regime}">${regime}</span>`;
  }

  // Header entry allowed
  const entryEl = $('header-entry');
  if (entryEl) {
    const allowed = data.new_entry_allowed;
    entryEl.innerHTML = allowed
      ? `<span class="text-green">✓ Allowed</span>`
      : `<span class="text-red">✗ Blocked</span>`;
  }

  // Regime updated time
  const ts = fmtKST(data.ts);
  setText('regime-updated', ts);

  // Regime panel
  const regimeName = $('regime-name');
  if (regimeName) {
    regimeName.textContent = regime;
    regimeName.className = `regime-name regime-${regime}`;
  }

  // Allowed strategies
  const allowedEl = $('regime-allowed');
  if (allowedEl && data.allowed_strategies) {
    allowedEl.innerHTML = data.allowed_strategies.length
      ? data.allowed_strategies.map(s => `<span>${s}</span>`).join('')
      : '<span style="color:var(--red)">NONE</span>';
  }

  // Regime indicators
  renderRegimeIndicators(data);
}

function renderRegimeIndicators(data) {
  const container = $('regime-indicators');
  if (!container) return;

  const fields = [
    { key: 'btc_price',    label: 'BTC Price',   fmt: v => fmtPrice(v, 'BTCUSDT') },
    { key: 'btc_ema50',    label: 'EMA 50 (4H)', fmt: v => fmtPrice(v, 'BTCUSDT') },
    { key: 'btc_atr_pct',  label: 'ATR %',        fmt: v => fmtNum(v, 2) + '%' },
    { key: 'btc_ret_24h',  label: 'BTC 24H Ret', fmt: v => fmtPct(v), color: true },
    { key: 'btc_ret_1h',   label: 'BTC 1H Ret',  fmt: v => fmtPct(v), color: true },
    { key: 'btc_rsi',      label: 'RSI (4H)',     fmt: v => fmtNum(v, 1) },
    { key: 'funding',      label: 'Funding Rate', fmt: v => fmtNum(v, 4) + '%' },
    { key: 'btc_bb_bw',    label: 'BB Bandwidth', fmt: v => fmtNum(v, 4) },
  ];

  container.innerHTML = fields.map(f => {
    const val = data[f.key];
    const fmtd = val != null ? f.fmt(val) : '—';
    const extra = f.color ? colorClass(val) : '';
    return `
      <div class="regime-indicator-item">
        <div class="metric-label">${f.label}</div>
        <div class="metric-value ${extra}">${fmtd}</div>
      </div>`;
  }).join('');
}

// ============================================================
// Candle (no-op for now — used for future charting)
// ============================================================
function handleCandle(data) {
  // Phase 1: just log
  // Phase 2: update mini chart
}

// ============================================================
// Panel 2 — Signals
// ============================================================

/**
 * Called when the WebSocket pushes a new "signal" event.
 * Prepends to the in-memory list and re-renders the table.
 */
function handleSignal(data) {
  if (!data) return;
  // Prepend new signal (newest first)
  state.signals.unshift(data);
  if (state.signals.length > 50) state.signals.length = 50;

  renderSignalsTable();

  // Flash the live indicator dot
  flashLiveDot('signals-live-dot');

  // Toast for BUY/SELL
  if (data.action === 'BUY' || data.action === 'SELL') {
    const actionLabel = data.action === 'BUY' ? '🟢 BUY' : '🔴 SELL';
    showToast(
      data.action === 'BUY' ? 'success' : 'error',
      `${actionLabel} Signal — ${data.symbol}`,
      `${data.strategy} | conf ${(data.confidence * 100).toFixed(0)}% | ${data.regime}`,
      4000,
    );
  }
}

function renderSignalsTable() {
  const tbody = $('signals-tbody');
  if (!tbody) return;

  const countEl = $('signals-count');
  if (countEl) countEl.textContent = `${state.signals.length} signal${state.signals.length !== 1 ? 's' : ''}`;

  if (state.signals.length === 0) {
    tbody.innerHTML = '<tr class="signals-empty-row"><td colspan="8">Waiting for signals…</td></tr>';
    return;
  }

  tbody.innerHTML = state.signals.map(sig => {
    const time = fmtKST(sig.ts);
    const actionCls = sig.action === 'BUY' ? 'action-buy' : sig.action === 'SELL' ? 'action-sell' : 'action-skip';
    const confPct = sig.confidence != null ? Math.round(sig.confidence * 100) : 0;
    const confBar = `
      <div class="conf-bar-wrap" title="${confPct}%">
        <div class="conf-bar-fill" style="width:${confPct}%;background:${confBarColor(confPct)}"></div>
        <span class="conf-bar-label">${confPct}%</span>
      </div>`;
    const reason = sig.reason
      ? `<span class="signal-reason" title="${escapeHtml(sig.reason)}">${escapeHtml(truncate(sig.reason, 48))}</span>`
      : '—';
    return `
      <tr class="signal-row ${actionCls}-row">
        <td class="text-mono" style="white-space:nowrap;font-size:11px">${time}</td>
        <td class="text-mono fw-bold">${sig.symbol || '—'}</td>
        <td><span class="signal-action-badge ${actionCls}">${sig.action || '—'}</span></td>
        <td class="text-mono" style="font-size:11px">${sig.strategy || '—'}</td>
        <td><span class="mode-badge mode-paper">PAPER</span></td>
        <td>${confBar}</td>
        <td><span class="regime-badge regime-${sig.regime || 'UNKNOWN'}" style="font-size:10px">${sig.regime || '—'}</span></td>
        <td>${reason}</td>
      </tr>`;
  }).join('');
}

function confBarColor(pct) {
  if (pct >= 80) return 'var(--green)';
  if (pct >= 60) return 'var(--accent-cyan)';
  if (pct >= 40) return 'var(--yellow)';
  return 'var(--text-muted)';
}

// ============================================================
// Panel 3 — Open Paper Positions
// ============================================================

function handlePositionOpened(data) {
  if (!data) return;
  state.openPositions.unshift(data);
  renderPositionsTable();
  showToast('success', `Position Opened — ${data.symbol}`,
    `${data.strategy} | ${data.side} @ ${fmtPrice(data.entry_price, data.symbol)}`, 3500);
}

function handlePositionClosed(data) {
  if (!data) return;
  // Remove from open list
  state.openPositions = state.openPositions.filter(p => p.id !== data.id);
  renderPositionsTable();

  const pnl = data.pnl_pct != null ? data.pnl_pct.toFixed(2) : '?';
  const win = data.pnl_pct != null && data.pnl_pct > 0;
  showToast(
    win ? 'success' : 'error',
    `Position Closed — ${data.symbol} (${win ? 'WIN' : 'LOSS'})`,
    `${data.strategy} | PnL ${win ? '+' : ''}${pnl}% | ${data.close_reason || ''}`,
    4000,
  );
}

function renderPositionsTable() {
  const tbody = $('positions-tbody');
  if (!tbody) return;

  const countEl = $('positions-count');
  if (countEl) countEl.textContent = `${state.openPositions.length} open`;

  if (state.openPositions.length === 0) {
    tbody.innerHTML = '<tr class="signals-empty-row"><td colspan="8">No open positions</td></tr>';
    return;
  }

  tbody.innerHTML = state.openPositions.map(pos => {
    const opened = fmtKST(pos.opened_at);
    const sideCls = pos.side === 'LONG' ? 'action-buy' : 'action-sell';
    return `
      <tr>
        <td class="text-mono fw-bold">${pos.symbol || '—'}</td>
        <td class="text-mono" style="font-size:11px">${pos.strategy || '—'}</td>
        <td><span class="signal-action-badge ${sideCls}">${pos.side || '—'}</span></td>
        <td class="text-mono">${fmtPrice(pos.entry_price, pos.symbol)}</td>
        <td class="text-mono text-green">${pos.tp != null ? fmtPrice(pos.tp, pos.symbol) : '—'}</td>
        <td class="text-mono text-red">${pos.sl != null ? fmtPrice(pos.sl, pos.symbol) : '—'}</td>
        <td><span class="regime-badge regime-${pos.regime || 'UNKNOWN'}" style="font-size:10px">${pos.regime || '—'}</span></td>
        <td class="text-mono" style="font-size:11px">${opened}</td>
      </tr>`;
  }).join('');
}

// ============================================================
// Panel 4 — Strategy Board
// ============================================================

async function refreshStrategyBoard() {
  try {
    const resp = await fetch('/api/strategies');
    if (!resp.ok) return;
    const strategies = await resp.json();
    renderStrategyBoard(strategies);

    const updEl = $('strategy-board-updated');
    if (updEl) updEl.textContent = `Updated ${fmtKST(new Date())} KST`;
  } catch (e) {
    console.debug('Strategy board refresh failed:', e);
  }
}

function renderStrategyBoard(strategies) {
  const grid = $('strategy-board-grid');
  if (!grid) return;

  if (!strategies || strategies.length === 0) {
    grid.innerHTML = '<div class="text-muted" style="padding:20px;font-size:13px">No strategies loaded</div>';
    return;
  }

  grid.innerHTML = strategies.map(s => {
    const st = s.stats || {};
    const mode = s.mode || 'PAPER';
    const modeCls = mode === 'PAUSED' ? 'mode-badge mode-paused' : 'mode-badge mode-paper';

    const wr = st.win_rate != null ? (st.win_rate * 100).toFixed(1) + '%' : '—';
    const pf = st.profit_factor != null ? st.profit_factor.toFixed(2) : '—';
    const tc = st.trade_count != null ? st.trade_count : 0;
    const exp = st.expectancy != null ? (st.expectancy >= 0 ? '+' : '') + st.expectancy.toFixed(2) + '%' : '—';
    const mdd = st.mdd != null ? st.mdd.toFixed(2) + '%' : '—';
    const openCnt = st.open_count || 0;

    const lastTs = fmtKST(s.last_signal_ts);

    const wrColor = st.win_rate != null
      ? (st.win_rate >= 0.55 ? 'text-green' : st.win_rate >= 0.45 ? 'text-yellow' : 'text-red')
      : '';

    const regimeTags = (s.regime_filter || [])
      .map(r => `<span class="regime-badge regime-${r}" style="font-size:9px;padding:1px 5px">${r}</span>`)
      .join(' ');

    return `
      <div class="strategy-card">
        <div class="strategy-card-header">
          <div>
            <div class="strategy-name">${s.name}</div>
            <div class="strategy-category">${s.category || ''}</div>
          </div>
          <span class="${modeCls}">${mode}</span>
        </div>

        <div class="strategy-regime-tags">${regimeTags || '<span class="text-muted" style="font-size:10px">No regime filter</span>'}</div>

        <div class="strategy-stats-grid">
          <div class="strategy-stat">
            <div class="strategy-stat-label">Win Rate</div>
            <div class="strategy-stat-value ${wrColor}">${wr}</div>
          </div>
          <div class="strategy-stat">
            <div class="strategy-stat-label">Profit Factor</div>
            <div class="strategy-stat-value">${pf}</div>
          </div>
          <div class="strategy-stat">
            <div class="strategy-stat-label">Trades</div>
            <div class="strategy-stat-value">${tc}</div>
          </div>
          <div class="strategy-stat">
            <div class="strategy-stat-label">Open</div>
            <div class="strategy-stat-value">${openCnt}</div>
          </div>
          <div class="strategy-stat">
            <div class="strategy-stat-label">Expectancy</div>
            <div class="strategy-stat-value">${exp}</div>
          </div>
          <div class="strategy-stat">
            <div class="strategy-stat-label">Max DD</div>
            <div class="strategy-stat-value text-red">${mdd}</div>
          </div>
        </div>

        <div class="strategy-card-footer">
          <span class="strategy-last-signal">Last signal: ${lastTs}</span>
        </div>
      </div>`;
  }).join('');
}

// ============================================================
// Initial data fetch for Phase 2 panels
// ============================================================

async function refreshSignalsPanel() {
  try {
    const resp = await fetch('/api/signals?limit=50');
    if (!resp.ok) return;
    const signals = await resp.json();
    state.signals = signals;
    renderSignalsTable();
  } catch (e) {
    console.debug('Signals panel refresh failed:', e);
  }
}

async function refreshOpenPositions() {
  try {
    const resp = await fetch('/api/open-positions');
    if (!resp.ok) return;
    const positions = await resp.json();
    state.openPositions = positions;
    renderPositionsTable();
  } catch (e) {
    console.debug('Open positions refresh failed:', e);
  }
}

// ============================================================
// Funding rate
// ============================================================
function handleFunding(data) {
  const { symbol, rate } = data;
  if (!symbol) return;
  state.indicators[symbol] = state.indicators[symbol] || {};
  state.indicators[symbol].funding_rate = rate;
  updateFundingInCard(symbol, rate);
}

function updateFundingInCard(symbol, rate) {
  const el = $(`funding-${symbol}`);
  if (el) {
    el.textContent = rate != null ? fmtNum(rate, 4) + '%' : '—';
    el.className = `metric-value ${parseFloat(rate) > 0 ? 'highlight-yellow' : ''}`;
  }
}

// ============================================================
// Open Interest
// ============================================================
function handleOI(data) {
  const { symbol, oi } = data;
  if (!symbol) return;
  state.indicators[symbol] = state.indicators[symbol] || {};
  state.indicators[symbol].open_interest = oi;
  const el = $(`oi-${symbol}`);
  if (el) el.textContent = fmtVol(oi);
}

// ============================================================
// System mode
// ============================================================
function handleSystemMode(data) {
  updateSystemMode(data.mode);
}

function updateSystemMode(mode) {
  const el = $('header-mode');
  if (!el) return;
  el.innerHTML = `<span class="badge mode-${mode}"><span class="badge-dot" style="background:currentColor"></span>${mode}</span>`;
}

// ============================================================
// Exchange status
// ============================================================
function handleExchangeStatus(data) {
  updateExchangeStatus(data.ok);
}

function updateExchangeStatus(ok) {
  const el = $('header-exchange');
  if (!el) return;
  el.innerHTML = ok
    ? `<span class="conn-ok">● Binance OK</span>`
    : `<span class="conn-fail">● Binance FAIL</span>`;
}

// ============================================================
// P&L + Exposure
// ============================================================
function updatePnL(pnl, pct) {
  const el = $('header-pnl');
  if (!el) return;
  if (pnl == null) { el.textContent = '—'; return; }
  const sign = pnl >= 0 ? '+' : '';
  const cls  = pnl >= 0 ? 'pnl-positive' : 'pnl-negative';
  el.innerHTML = `<span class="${cls}">${sign}$${parseFloat(pnl).toFixed(2)} (${sign}${parseFloat(pct || 0).toFixed(2)}%)</span>`;
}

function updateExposure(pct) {
  const el = $('header-exposure');
  if (!el) return;
  el.textContent = pct != null ? `${parseFloat(pct).toFixed(1)}%` : '—';
}

// ============================================================
// Symbol cards
// ============================================================
function renderAllSymbolCards() {
  const grid = $('indicators-grid');
  if (!grid) return;

  const symbols = Array.from($qa('.symbol-card')).map(c => c.dataset.symbol);
  symbols.forEach(sym => updateSymbolCard(sym));
}

function updateSymbolCard(symbol) {
  const card = $(`card-${symbol}`);
  if (!card) return;

  const ticker = state.tickers[symbol] || {};
  const ind    = state.indicators[symbol] || {};

  const price     = ticker.price;
  const changePct = ticker.change_pct;
  const vol24h    = ticker.volume_24h;
  const funding   = ind.funding_rate;
  const oi        = ind.open_interest;
  const indicators = ind.indicators || {};

  // Price
  const priceEl = $(`price-${symbol}`);
  if (priceEl) {
    const newText = fmtPrice(price, symbol);
    if (priceEl.textContent !== newText) {
      priceEl.textContent = newText;
      addFlash(priceEl);
    }
  }

  // Change pct
  const changeEl = $(`change-${symbol}`);
  if (changeEl) {
    changeEl.textContent = fmtPct(changePct);
    changeEl.className = `symbol-change ${pctClass(changePct)}`;
  }

  // Card accent
  if (changePct != null) {
    card.className = `symbol-card ${parseFloat(changePct) >= 0 ? 'positive' : 'negative'}`;
  }

  // Volume
  const volEl = $(`vol-${symbol}`);
  if (volEl) volEl.textContent = fmtVol(vol24h);

  // Funding
  const fundEl = $(`funding-${symbol}`);
  if (fundEl) {
    fundEl.textContent = funding != null ? fmtNum(funding, 4) + '%' : '—';
    fundEl.className = `metric-value ${parseFloat(funding) > 0 ? 'highlight-yellow' : ''}`;
  }

  // OI
  const oiEl = $(`oi-${symbol}`);
  if (oiEl) oiEl.textContent = fmtVol(oi);

  // EMA / RSI / ATR / VWAP — prefer 4h then 1h
  const indData = indicators['4h'] || indicators['1h'] || {};

  const setMetric = (id, val, extra = '') => {
    const el = $(id);
    if (el) {
      el.textContent = val;
      if (extra) el.className = `metric-value ${extra}`;
    }
  };

  setMetric(`ema50-${symbol}`,  fmtPrice(indData.ema50, symbol));
  setMetric(`rsi-${symbol}`,    indData.rsi != null ? fmtNum(indData.rsi, 1) : '—',
    indData.rsi > 70 ? 'highlight-red' : indData.rsi < 30 ? 'highlight-green' : '');
  setMetric(`atr-${symbol}`,    indData.atr_pct != null ? fmtNum(indData.atr_pct, 2) + '%' : '—');
  setMetric(`vwap-${symbol}`,   fmtPrice(indData.vwap, symbol));
}

// ============================================================
// Periodic indicator refresh from REST
// ============================================================
async function refreshIndicators() {
  try {
    const resp = await fetch('/api/indicators');
    if (!resp.ok) return;
    const data = await resp.json();
    Object.entries(data).forEach(([sym, info]) => {
      state.indicators[sym] = info;
    });
    renderAllSymbolCards();
  } catch (e) {
    console.debug('Indicator refresh failed:', e);
  }
}

// ============================================================
// Toast notifications
// ============================================================
function showToast(type, title, msg, duration = 5000) {
  const container = $q('.toast-container');
  if (!container) return;

  const icons = {
    'regime-change': '⚡',
    'error':         '❌',
    'success':       '✅',
  };

  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `
    <div class="toast-icon">${icons[type] || 'ℹ️'}</div>
    <div class="toast-body">
      <div class="toast-title">${title}</div>
      <div class="toast-msg">${msg}</div>
    </div>`;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(120%)';
    toast.style.transition = 'all 0.3s ease';
    setTimeout(() => toast.remove(), 350);
  }, duration);
}

// ============================================================
// Flash animation (CSS class added/removed)
// ============================================================
(function injectFlashStyle() {
  const style = document.createElement('style');
  style.textContent = `
    .flash {
      animation: flashBg 0.6s ease;
    }
    @keyframes flashBg {
      0%   { background-color: rgba(59,130,246,0.3); }
      100% { background-color: transparent; }
    }
  `;
  document.head.appendChild(style);
})();

// ============================================================
// Clock (header)
// ============================================================
function startClock() {
  function tick() {
    const el = $('header-clock');
    if (el) {
      const now = new Date();
      const kst = now.toLocaleString('ko-KR', _KST_OPTS_TIME);
      const utc = now.toUTCString().slice(17, 25);
      el.textContent = kst + ' KST / ' + utc + ' UTC';
    }
  }
  tick();
  setInterval(tick, 1000);
}

// ============================================================
// Live dot flash helper
// ============================================================
function flashLiveDot(id) {
  const el = $(id);
  if (!el) return;
  el.classList.add('live-dot-flash');
  setTimeout(() => el.classList.remove('live-dot-flash'), 600);
}

// ============================================================
// Phase 3 — Kill Switch UI
// ============================================================

function handleKillSwitch() {
  if (state.killSwitchActive) {
    // Already active — show reset dialog
    const modal = $('kill-switch-modal');
    if (!modal) return;
    $('ks-modal-title').textContent = 'Reset Kill Switch';
    $('ks-modal-body').innerHTML =
      'The kill switch is currently <strong>ACTIVE</strong>.<br/><br/>' +
      'Reset will allow new entries again. System mode will return to OBSERVE.<br/><br/>' +
      'Are you sure you want to reset?';
    const confirmBtn = $('ks-modal-confirm-btn');
    if (confirmBtn) {
      confirmBtn.textContent = 'Reset Kill Switch';
      confirmBtn.className = 'modal-btn modal-btn-reset';
      confirmBtn.onclick = confirmKillSwitchReset;
    }
    modal.style.display = 'flex';
  } else {
    // Not active — show activation dialog
    const modal = $('kill-switch-modal');
    if (!modal) return;
    $('ks-modal-title').textContent = 'Confirm Kill Switch';
    $('ks-modal-body').innerHTML =
      'This will <strong>immediately block all new entries</strong> ' +
      'and cancel all open orders.<br/><br/>' +
      'Existing positions will be kept and protected by their SL/TP orders.<br/><br/>' +
      'Are you sure?';
    const confirmBtn = $('ks-modal-confirm-btn');
    if (confirmBtn) {
      confirmBtn.textContent = 'Confirm Kill Switch';
      confirmBtn.className = 'modal-btn modal-btn-confirm';
      confirmBtn.onclick = confirmKillSwitch;
    }
    modal.style.display = 'flex';
  }
}

function closeKillSwitchModal() {
  const modal = $('kill-switch-modal');
  if (modal) modal.style.display = 'none';
}

async function confirmKillSwitch() {
  closeKillSwitchModal();
  try {
    const resp = await fetch('/api/kill-switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        reason: 'Manual trigger from dashboard',
        authorized_by: 'dashboard_operator',
      }),
    });
    if (resp.ok) {
      showToast('error', '⚠ Kill Switch Activated', 'All new entries blocked. Orders cancelled.', 6000);
      updateKillSwitchButton(true);
    } else {
      const err = await resp.json();
      showToast('error', 'Kill Switch Failed', err.detail || 'Unknown error', 5000);
    }
  } catch (e) {
    showToast('error', 'Kill Switch Error', String(e), 5000);
  }
}

async function confirmKillSwitchReset() {
  closeKillSwitchModal();
  try {
    const resp = await fetch('/api/kill-switch/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ authorized_by: 'dashboard_operator' }),
    });
    if (resp.ok) {
      showToast('success', 'Kill Switch Reset', 'System mode → OBSERVE. New entries now allowed.', 5000);
      updateKillSwitchButton(false);
    } else {
      const err = await resp.json();
      showToast('error', 'Reset Failed', err.detail || 'Unknown error', 5000);
    }
  } catch (e) {
    showToast('error', 'Reset Error', String(e), 5000);
  }
}

function updateKillSwitchButton(isActive) {
  state.killSwitchActive = isActive;
  const btn = $('btn-kill-switch');
  const label = $('kill-switch-label');
  if (!btn) return;
  if (isActive) {
    btn.classList.add('kill-switch-btn-active');
    if (label) label.textContent = 'KILL SWITCH ON';
  } else {
    btn.classList.remove('kill-switch-btn-active');
    if (label) label.textContent = 'KILL SWITCH';
  }
}

function handleKillSwitchUpdate(data) {
  if (!data) return;
  const isActive = !!data.active;
  updateKillSwitchButton(isActive);

  if (isActive) {
    showToast(
      'error',
      '⚠ Kill Switch ACTIVE',
      `Reason: ${data.reason || '?'} | By: ${data.triggered_by || '?'}`,
      8000,
    );
  } else {
    showToast(
      'success',
      'Kill Switch Reset',
      `Reset by: ${data.reset_by || '?'}`,
      5000,
    );
  }
}

// ============================================================
// Phase 3 — Live Positions (Panel 3)
// ============================================================

async function refreshLivePositions() {
  try {
    const resp = await fetch('/api/live-positions');
    if (!resp.ok) return;
    const data = await resp.json();

    state.livePositions = data.live || [];
    state.openPositions = data.paper || [];

    renderLivePositionsTable();
    renderPositionsTable();
  } catch (e) {
    console.debug('Live positions refresh failed:', e);
  }
}

function renderLivePositionsTable() {
  const tbody = $('live-positions-tbody');
  if (!tbody) return;

  const countEl = $('live-positions-count');
  const totalEl = $('positions-count');
  const total   = state.livePositions.length + state.openPositions.length;

  if (countEl) countEl.textContent = `${state.livePositions.length} position${state.livePositions.length !== 1 ? 's' : ''}`;
  if (totalEl) totalEl.textContent = `${total} open`;

  if (state.livePositions.length === 0) {
    tbody.innerHTML = '<tr class="signals-empty-row"><td colspan="9">No live positions</td></tr>';
    return;
  }

  tbody.innerHTML = state.livePositions.map(pos => {
    const side     = pos.side || 'LONG';
    const sideCls  = side === 'LONG' ? 'action-buy' : 'action-sell';
    const pnl      = pos.pnl_pct != null ? parseFloat(pos.pnl_pct) : null;
    const pnlCls   = pnl == null ? '' : pnl >= 0 ? 'highlight-green' : 'highlight-red';
    const pnlStr   = pnl != null ? `${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}% (${pos.unrealised_pnl != null ? (parseFloat(pos.unrealised_pnl) >= 0 ? '+' : '') + parseFloat(pos.unrealised_pnl).toFixed(2) : '?'} USDT)` : '—';
    const posId    = pos.id || '';

    return `
      <tr class="live-position-row">
        <td class="text-mono fw-bold">${pos.symbol || '—'}</td>
        <td><span class="signal-action-badge ${sideCls}">${side}</span></td>
        <td class="text-mono">${fmtNum(pos.qty, 4)}</td>
        <td class="text-mono">${fmtPrice(pos.entry_price, pos.symbol)}</td>
        <td class="text-mono">${fmtPrice(pos.current_price, pos.symbol)}</td>
        <td class="text-mono ${pnlCls}">${pnlStr}</td>
        <td class="text-mono text-red">${pos.sl != null ? fmtPrice(pos.sl, pos.symbol) : '—'}</td>
        <td class="text-mono text-green">${pos.tp != null ? fmtPrice(pos.tp, pos.symbol) : '—'}</td>
        <td>
          ${posId ? `<button class="btn-sm btn-danger" onclick="closePosition('${escapeHtml(posId)}', '${escapeHtml(pos.symbol || '')}')">Close</button>` : '—'}
        </td>
      </tr>`;
  }).join('');
}

async function closePosition(positionId, symbol) {
  if (!confirm(`Close position ${symbol}? This will submit a reduce-only MARKET order.`)) return;
  try {
    const resp = await fetch(`/api/order/close/${positionId}`, { method: 'POST' });
    if (resp.ok) {
      showToast('success', `Close order submitted for ${symbol}`, 'Reduce-only MARKET order placed.', 4000);
      setTimeout(refreshLivePositions, 2000);
    } else {
      const err = await resp.json();
      showToast('error', `Close failed: ${symbol}`, err.detail || 'Unknown error', 5000);
    }
  } catch (e) {
    showToast('error', 'Close position error', String(e), 5000);
  }
}

function togglePaperPositions() {
  const container = $('paper-positions-container');
  const icon = $('paper-collapse-icon');
  if (!container) return;
  const isHidden = container.style.display === 'none';
  container.style.display = isHidden ? 'block' : 'none';
  if (icon) icon.textContent = isHidden ? '▲ hide' : '▼ show';
}

function handleOrderUpdate(data) {
  if (!data) return;
  // Refresh positions after any order update
  refreshLivePositions();
}

// ============================================================
// Phase 3 — Reconcile status
// ============================================================

function handleReconcileUpdate(data) {
  if (!data) return;
  state.reconcileStatus = data;
  updateReconcileStatusBadge(data);

  if (data.has_discrepancies) {
    showToast(
      'error',
      '⚠ Reconciliation Discrepancy',
      `DB-only: ${data.in_db_not_exchange?.length || 0} | Exchange-only: ${data.in_exchange_not_db?.length || 0}`,
      7000,
    );
  }
}

function updateReconcileStatusBadge(data) {
  const dot   = $('reconcile-dot');
  const label = $('reconcile-label');
  if (!label) return;

  if (!data) {
    label.textContent = 'Reconcile: never';
    if (dot) dot.className = 'reconcile-dot reconcile-unknown';
    return;
  }

  const ageSec = data.age_sec != null ? data.age_sec : null;
  const ageStr = ageSec != null ? `${Math.round(ageSec / 60)}min ago` : '—';

  if (data.has_discrepancies) {
    label.textContent = `Reconcile: DISCREPANCY (${ageStr})`;
    if (dot) dot.className = 'reconcile-dot reconcile-error';
  } else {
    label.textContent = `Last reconciled: ${ageStr}`;
    if (dot) dot.className = 'reconcile-dot reconcile-ok';
  }
}

async function refreshReconcileStatus() {
  try {
    const resp = await fetch('/api/reconcile-status');
    if (!resp.ok) return;
    const data = await resp.json();
    state.reconcileStatus = data;
    updateReconcileStatusBadge(data);
  } catch (e) {
    console.debug('Reconcile status refresh failed:', e);
  }
}

// ============================================================
// Phase 3 — Account balance
// ============================================================

function handleBalanceUpdate(data) {
  if (!data) return;
  const balanceEl = $('header-balance');
  const availableEl = $('header-balance-sub');
  if (balanceEl) {
    balanceEl.textContent = data.balance != null ? `$${parseFloat(data.balance).toFixed(2)}` : '—';
    addFlash(balanceEl);
  }
  if (availableEl) {
    const availableText = data.available_balance != null
      ? `$${parseFloat(data.available_balance).toFixed(2)}`
      : '—';
    const marginText = data.margin_balance != null
      ? `$${parseFloat(data.margin_balance).toFixed(2)}`
      : '—';
    availableEl.textContent = `Avail ${availableText} · Margin ${marginText}`;
    addFlash(availableEl);
  }
}

// ============================================================
// Phase 3 — Trade Log (Panel 5)
// ============================================================

async function refreshTradeLog() {
  const strategy = ($('tl-filter-strategy')?.value || '').trim();
  const period   = $('tl-filter-period')?.value || '';
  const mode     = state.tradeLogMode;

  const params = new URLSearchParams({ limit: '100' });
  if (strategy) params.set('strategy', strategy);
  if (period)   params.set('period', period);
  if (mode)     params.set('mode', mode);

  try {
    const resp = await fetch(`/api/trade-log?${params}`);
    if (!resp.ok) return;
    const trades = await resp.json();
    state.tradeLog = trades;
    renderTradeLogTable();

    // Populate strategy dropdown from trade data
    populateStrategyFilter(trades);
  } catch (e) {
    console.debug('Trade log refresh failed:', e);
  }
}

function setTradeLogMode(mode) {
  state.tradeLogMode = mode;
  // Update button states
  ['', 'LIVE', 'PAPER'].forEach(m => {
    const btn = $(`tl-mode-${m === '' ? 'all' : m.toLowerCase()}`);
    if (btn) btn.className = 'mode-toggle-btn' + (m === mode ? ' active' : '');
  });
  refreshTradeLog();
}

function populateStrategyFilter(trades) {
  const select = $('tl-filter-strategy');
  if (!select) return;
  const strategies = [...new Set(trades.map(t => t.strategy).filter(Boolean))];
  const currentVal = select.value;
  select.innerHTML = '<option value="">All Strategies</option>' +
    strategies.map(s => `<option value="${escapeHtml(s)}" ${s === currentVal ? 'selected' : ''}>${escapeHtml(s)}</option>`).join('');
}

function renderTradeLogTable() {
  const tbody = $('trade-log-tbody');
  if (!tbody) return;

  if (!state.tradeLog || state.tradeLog.length === 0) {
    tbody.innerHTML = '<tr class="signals-empty-row"><td colspan="10">No closed trades yet</td></tr>';
    return;
  }

  tbody.innerHTML = state.tradeLog.map(trade => {
    const openTime   = fmtKSTFull(trade.opened_at);
    const side       = trade.side || '—';
    const sideCls    = side === 'LONG' ? 'action-buy' : (side === 'SHORT' ? 'action-sell' : 'action-skip');
    const pnl        = trade.pnl_pct != null ? parseFloat(trade.pnl_pct) : null;
    const pnlCls     = pnl == null ? '' : pnl > 0 ? 'highlight-green' : (pnl < 0 ? 'highlight-red' : '');
    const pnlStr     = pnl != null ? `${pnl > 0 ? '+' : ''}${pnl.toFixed(2)}%` : '—';
    const mode       = trade.mode || 'PAPER';
    const modeCls    = mode === 'LIVE' ? 'mode-live' : 'mode-paper';
    const tradeId    = trade.id || trade.signal_id || '';

    // Duration
    let duration = '—';
    if (trade.opened_at && trade.closed_at) {
      const diffMs = trade.closed_at - trade.opened_at;
      const mins   = Math.floor(diffMs / 60000);
      if (mins < 60) duration = `${mins}m`;
      else if (mins < 1440) duration = `${Math.floor(mins / 60)}h ${mins % 60}m`;
      else duration = `${Math.floor(mins / 1440)}d`;
    }

    const clickAttr = tradeId ? `onclick="showAuditTrail('${escapeHtml(tradeId)}')" style="cursor:pointer"` : '';

    return `
      <tr class="trade-log-row" ${clickAttr}>
        <td class="text-mono" style="font-size:11px;white-space:nowrap">${openTime}</td>
        <td class="text-mono fw-bold">${trade.symbol || '—'}</td>
        <td><span class="signal-action-badge ${sideCls}">${side}</span></td>
        <td class="text-mono" style="font-size:11px">${trade.strategy || '—'}</td>
        <td><span class="mode-badge ${modeCls}">${mode}</span></td>
        <td class="text-mono">${fmtPrice(trade.entry_price, trade.symbol)}</td>
        <td class="text-mono">${fmtPrice(trade.exit_price, trade.symbol)}</td>
        <td class="text-mono ${pnlCls}">${pnlStr}</td>
        <td class="text-mono" style="font-size:11px">${duration}</td>
        <td style="font-size:11px;color:var(--text-muted)">${escapeHtml(truncate(trade.close_reason || '—', 20))}</td>
      </tr>`;
  }).join('');
}

// ============================================================
// Phase 3 — Audit Trail Modal
// ============================================================

async function showAuditTrail(tradeId) {
  const modal = $('audit-trail-modal');
  const content = $('audit-trail-content');
  if (!modal || !content) return;

  content.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-muted)">Loading…</div>';
  modal.style.display = 'flex';

  try {
    const resp = await fetch(`/api/trade-log/${tradeId}/audit`);
    if (!resp.ok) {
      content.innerHTML = '<div style="color:var(--red);padding:20px">Failed to load audit trail.</div>';
      return;
    }
    const trail = await resp.json();

    if (!trail || trail.length === 0) {
      content.innerHTML = '<div style="color:var(--text-muted);padding:20px;font-style:italic">No audit trail records found.</div>';
      return;
    }

    content.innerHTML = trail.map((entry, idx) => {
      const ts = fmtKSTFull(entry.ts);
      let riskCheck = {};
      try { riskCheck = JSON.parse(entry.risk_check || '{}'); } catch(e) {}
      const fromStatus = riskCheck.from_status || '';
      const toStatus   = riskCheck.to_status   || '';
      const transArrow = fromStatus ? `${fromStatus} → ${toStatus}` : toStatus;

      return `
        <div class="audit-entry">
          <div class="audit-entry-header">
            <span class="audit-step">#${idx + 1}</span>
            <span class="audit-transition">${escapeHtml(transArrow)}</span>
            <span class="audit-ts">${ts}</span>
          </div>
          <div class="audit-entry-body">
            <div class="audit-reason">${escapeHtml(entry.decision_reason || '—')}</div>
            ${entry.strategy ? `<div class="audit-meta">Strategy: <code>${escapeHtml(entry.strategy)}</code></div>` : ''}
          </div>
        </div>`;
    }).join('');
  } catch (e) {
    content.innerHTML = `<div style="color:var(--red);padding:20px">Error: ${escapeHtml(String(e))}</div>`;
  }
}

function closeAuditModal() {
  const modal = $('audit-trail-modal');
  if (modal) modal.style.display = 'none';
}

// Close modals on overlay click
document.addEventListener('click', (e) => {
  if (e.target.classList.contains('modal-overlay')) {
    e.target.style.display = 'none';
  }
});

// ============================================================
// String helpers
// ============================================================
function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function truncate(str, maxLen) {
  if (!str) return '';
  return str.length > maxLen ? str.slice(0, maxLen) + '…' : str;
}

// ============================================================
// Panel 6 — Regime Interpretation
// ============================================================

function handleRegimeInterpretation(data) {
  if (!data) return;
  renderRegimeInterpretation(data);
  const updEl = $('ai-panel-updated');
  if (updEl) updEl.textContent = `AI updated ${fmtKST(new Date())} KST`;
  showToast('regime-change', '🤖 AI Regime Analysis',
    `Regime ${data.regime || ''} interpreted`, 4000);
}

function renderRegimeInterpretation(data) {
  const nameEl = $('interp-regime-name');
  if (nameEl) {
    nameEl.innerHTML = `<span class="regime-badge regime-${data.regime || 'UNKNOWN'}">${data.regime || 'UNKNOWN'}</span>`;
  }
  const factorsEl = $('interp-why-factors');
  if (factorsEl) {
    const factors = data.why_factors || [];
    factorsEl.innerHTML = factors.length
      ? factors.map(f => `<li class="interp-factor-item">${escapeHtml(String(f))}</li>`).join('')
      : '<li class="text-muted" style="font-size:12px">No factors available</li>';
  }
  const transEl = $('interp-transition');
  if (transEl) transEl.textContent = data.transition_signals || '—';
  const stratEl = $('interp-strategy-rec');
  if (stratEl) stratEl.textContent = data.strategy_recommendations || '—';
  const tsEl = $('interp-ts');
  if (tsEl) {
    const ts = fmtKST(data.timestamp);
    const aiOk = data.ai_available !== false;
    tsEl.textContent = `${aiOk ? 'AI' : 'Offline'} · ${ts}`;
    tsEl.style.color = aiOk ? 'var(--accent-cyan)' : 'var(--text-muted)';
  }
  const badge = $('ai-badge');
  if (badge) {
    const aiOk = data.ai_available !== false;
    badge.className = aiOk ? 'panel-badge-ai' : 'panel-badge-ai ai-offline';
    badge.textContent = aiOk ? 'AI' : 'AI OFFLINE';
  }
}

async function refreshRegimeInterpretation() {
  try {
    const resp = await fetch('/api/regime-interpretation');
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.available) renderRegimeInterpretation(data);
  } catch (e) {
    console.debug('Regime interpretation refresh failed:', e);
  }
}

// ============================================================
// Panel 6 — Recommendations
// ============================================================

function handleWeeklyReview(data) {
  if (!data) return;
  refreshRecommendations();
  renderWeeklyReviewSummary(data);
  showToast('success', '📈 Weekly Review Complete',
    `${(data.recommendations || []).length} recommendation(s) generated`, 5000);
}

function handleDailyReview(data) {
  if (!data) return;
  if ((data.alert_count || 0) > 0) {
    showToast('error', `📊 Daily Review — ${data.date || ''}`,
      `${data.alert_count} warning(s) detected`, 6000);
  } else {
    showToast('success', `📊 Daily Review — ${data.date || ''}`, 'No warnings', 4000);
  }
  handleDailyAlertCount({ count: data.alert_count || 0 });
}

function handleDailyAlertCount(data) {
  const badge = $('daily-alert-badge');
  if (!badge) return;
  const count = data.count || 0;
  badge.textContent = count;
  badge.style.display = count > 0 ? 'inline-flex' : 'none';
}

function handleRecommendationDecided(data) {
  if (!data) return;
  refreshRecommendations();
}

async function refreshRecommendations() {
  try {
    const [pendResp, histResp] = await Promise.all([
      fetch('/api/recommendations'),
      fetch('/api/recommendations/history?limit=10'),
    ]);
    if (pendResp.ok) renderRecommendations(await pendResp.json());
    if (histResp.ok) renderRecommendationHistory(await histResp.json());
  } catch (e) {
    console.debug('Recommendations refresh failed:', e);
  }
}

function renderRecommendations(recommendations) {
  const list = $('recommendations-list');
  if (!list) return;
  const countEl = $('rec-pending-count');
  if (countEl) countEl.textContent = `${recommendations.length} pending`;
  if (!recommendations || recommendations.length === 0) {
    list.innerHTML = `<div class="text-muted" style="font-size:12px;padding:16px;text-align:center;font-style:italic">No pending recommendations</div>`;
    return;
  }
  list.innerHTML = recommendations.map(rec => buildRecommendationCard(rec, true)).join('');
}

function buildRecommendationCard(rec, showActions) {
  const typeColors = { PROMOTE: 'var(--green)', DEMOTE: 'var(--red)', MODIFY: 'var(--yellow)', RETIRE: 'var(--text-muted)' };
  const color = typeColors[rec.type] || 'var(--text-primary)';
  const sd = rec.supporting_data || {};
  const wr  = sd.win_rate  != null ? (sd.win_rate  * 100).toFixed(1) + '%' : '—';
  const pf  = sd.profit_factor != null ? sd.profit_factor.toFixed(2) : '—';
  const tc  = sd.trade_count != null ? sd.trade_count : '—';
  const exp = sd.expectancy  != null ? (sd.expectancy >= 0 ? '+' : '') + sd.expectancy.toFixed(2) + '%' : '—';
  const counterArgs = (rec.counter_arguments || [])
    .map(a => `<li style="font-size:11px;color:var(--text-secondary)">${escapeHtml(a)}</li>`)
    .join('');
  const actionsHtml = showActions && rec.status === 'PENDING' ? `
    <div class="rec-actions">
      <input type="text" class="rec-reason-input" placeholder="Decision reason (required)…" id="reason-${escapeHtml(rec.id)}" />
      <div class="rec-action-buttons">
        <button class="rec-btn rec-btn-approve" onclick="decideRecommendation('${escapeHtml(rec.id)}','APPROVED')">✓ Approve</button>
        <button class="rec-btn rec-btn-reject"  onclick="decideRecommendation('${escapeHtml(rec.id)}','REJECTED')">✗ Reject</button>
        <button class="rec-btn rec-btn-defer"   onclick="decideRecommendation('${escapeHtml(rec.id)}','DEFERRED')">⏸ Defer</button>
      </div>
    </div>` : '';
  const decisionInfo = rec.status !== 'PENDING' && rec.decided_by ? `
    <div class="rec-decision-info">
      ${buildStatusBadge(rec.status)} by <span style="color:var(--text-primary)">${escapeHtml(rec.decided_by)}</span>
      ${rec.decision_reason ? `— "${escapeHtml(truncate(rec.decision_reason, 80))}"` : ''}
    </div>` : '';
  return `
    <div class="rec-card${rec.status !== 'PENDING' ? ' rec-card-decided' : ''}">
      <div class="rec-card-header">
        <div class="rec-type-badge" style="color:${color}">${rec.type}</div>
        <div class="rec-strategy-name">${escapeHtml(rec.strategy)}</div>
        <div class="rec-mode-arrow">
          <span class="mode-badge mode-paper">${rec.current_mode || '—'}</span>
          <span style="color:var(--text-muted);font-size:11px;margin:0 4px">→</span>
          <span class="mode-badge mode-paper">${rec.proposed_mode || '—'}</span>
        </div>
        ${rec.status !== 'PENDING' ? buildStatusBadge(rec.status) : ''}
      </div>
      <div class="rec-metrics">
        <div class="rec-metric"><div class="rec-metric-label">Win Rate</div><div class="rec-metric-value">${wr}</div></div>
        <div class="rec-metric"><div class="rec-metric-label">Profit Factor</div><div class="rec-metric-value">${pf}</div></div>
        <div class="rec-metric"><div class="rec-metric-label">Trades</div><div class="rec-metric-value">${tc}</div></div>
        <div class="rec-metric"><div class="rec-metric-label">Expectancy</div><div class="rec-metric-value">${exp}</div></div>
      </div>
      ${counterArgs ? `<div style="margin-top:8px"><div class="rec-label">Counter-arguments:</div><ul style="margin:4px 0 0 16px">${counterArgs}</ul></div>` : ''}
      ${rec.rollback_condition ? `<div class="rec-rollback">Rollback: ${escapeHtml(rec.rollback_condition)}</div>` : ''}
      ${decisionInfo}
      ${actionsHtml}
    </div>`;
}

function buildStatusBadge(status) {
  const map   = { PENDING: 'status-pending', APPROVED: 'status-approved', REJECTED: 'status-rejected', DEFERRED: 'status-deferred' };
  const icons = { PENDING: '⏳', APPROVED: '✓', REJECTED: '✗', DEFERRED: '⏸' };
  return `<span class="rec-status-badge ${map[status] || ''}">${icons[status] || ''} ${status}</span>`;
}

function renderRecommendationHistory(history) {
  const list = $('rec-history-list');
  if (!list) return;
  if (!history || history.length === 0) {
    list.innerHTML = '<div class="text-muted" style="font-size:11px;padding:8px;font-style:italic">No past decisions yet</div>';
    return;
  }
  list.innerHTML = history.map(rec => buildRecommendationCard(rec, false)).join('');
}

async function decideRecommendation(recId, decision) {
  const reasonInput = $(`reason-${recId}`);
  const reason = reasonInput ? reasonInput.value.trim() : '';
  if (!reason) {
    if (reasonInput) reasonInput.focus();
    showToast('error', 'Reason Required', 'Provide a decision reason before submitting', 3000);
    return;
  }
  try {
    const resp = await fetch(`/api/recommendations/${recId}/decide`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, reason, decided_by: '22B_Operator' }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      showToast('error', 'Decision Failed', err.detail || 'Unknown error', 4000);
      return;
    }
    const labels = { APPROVED: '✓ Approved', REJECTED: '✗ Rejected', DEFERRED: '⏸ Deferred' };
    showToast(decision === 'APPROVED' ? 'success' : 'error', labels[decision] || decision, 'Recommendation decided', 3500);
    await refreshRecommendations();
  } catch (e) {
    showToast('error', 'Network Error', String(e), 4000);
  }
}

// ============================================================
// Panel 6 — Weekly Review Summary
// ============================================================

function renderWeeklyReviewSummary(data) {
  const container = $('ai-weekly-summary');
  const content   = $('weekly-review-content');
  if (!container || !content) return;

  const stats = data.strategy_stats || {};
  const statRows = Object.entries(stats).map(([name, s]) => {
    const pf = s.profit_factor != null ? s.profit_factor.toFixed(2) : '—';
    const wr = s.win_rate != null ? (s.win_rate * 100).toFixed(1) + '%' : '—';
    return `<tr>
      <td class="text-mono" style="font-size:11px">${escapeHtml(name)}</td>
      <td class="text-mono">${pf}</td>
      <td class="text-mono">${wr}</td>
      <td class="text-mono">${s.trade_count || 0}</td>
      <td><span class="mode-badge mode-paper">${s.mode || 'PAPER'}</span></td>
    </tr>`;
  }).join('');

  const aiHtml = data.ai_analysis ? `
    <div style="margin-top:14px">
      <div class="ai-section-subtitle">AI Analysis</div>
      <div class="ai-analysis-text">${escapeHtml(data.ai_analysis)}</div>
    </div>` : '';

  content.innerHTML = `
    <div style="font-size:11px;color:var(--text-muted);font-family:var(--font-mono);margin-bottom:10px">
      ${escapeHtml(data.week_label || '')} · ${escapeHtml(data.period_start || '')} → ${escapeHtml(data.period_end || '')}
    </div>
    ${statRows ? `<div class="signals-table-wrapper" style="margin-bottom:12px"><table class="signals-table">
      <thead><tr><th>Strategy</th><th>PF</th><th>WR</th><th>Trades</th><th>Mode</th></tr></thead>
      <tbody>${statRows}</tbody></table></div>` : ''}
    ${aiHtml}`;

  container.style.display = 'block';
}

async function refreshWeeklyReview() {
  try {
    const resp = await fetch('/api/weekly-review');
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.available) {
      renderWeeklyReviewSummary(data);
      if (data.recommendations) {
        renderRecommendations((data.recommendations || []).filter(r => r.status === 'PENDING'));
      }
    }
  } catch (e) {
    console.debug('Weekly review refresh failed:', e);
  }
}

async function refreshDailyAlertCount() {
  try {
    const resp = await fetch('/api/daily-alert-count');
    if (!resp.ok) return;
    handleDailyAlertCount(await resp.json());
  } catch (e) {
    console.debug('Daily alert count refresh failed:', e);
  }
}

// ============================================================
// Init
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
  connect();
  startClock();

  // Panel 1: Indicators
  refreshIndicators();
  setInterval(refreshIndicators, API_INDICATOR_REFRESH_MS);

  // Panel 2: Signals (initial load from REST, then live via WS)
  refreshSignalsPanel();

  // Panel 3: Live + Paper positions
  refreshLivePositions();
  setInterval(refreshLivePositions, API_POSITIONS_REFRESH_MS);

  // Panel 4: Strategy board
  refreshStrategyBoard();
  setInterval(refreshStrategyBoard, API_STRATEGY_REFRESH_MS);

  // Panel 5: Trade log
  refreshTradeLog();
  setInterval(refreshTradeLog, API_TRADE_LOG_REFRESH_MS);

  // Reconcile status
  refreshReconcileStatus();
  setInterval(refreshReconcileStatus, 60000);

  // Kill switch button initial state
  updateKillSwitchButton(state.killSwitchActive);

  // Panel 6: AI analysis (initial load)
  refreshRegimeInterpretation();
  refreshRecommendations();
  refreshWeeklyReview();
  refreshDailyAlertCount();

  // Refresh Panel 6 every 2 minutes
  setInterval(() => {
    refreshRegimeInterpretation();
    refreshRecommendations();
    refreshDailyAlertCount();
  }, 120000);

  // Handle manual refresh button if present
  const refreshBtn = $('btn-refresh');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', () => {
      refreshIndicators();
      refreshBtn.textContent = '↻ Refreshing…';
      setTimeout(() => { refreshBtn.textContent = '↻ Refresh'; }, 1500);
    });
  }

  // System Health panel
  loadSystemHealth();
  setInterval(loadSystemHealth, 10000);
});

// ============================================================
// System Health Panel
// ============================================================
function fmtUptime(sec) {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60) % 60;
  const h = Math.floor(sec / 3600);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function setHealthItem(iconId, valId, ok, label) {
  const icon = $(iconId);
  const val  = $(valId);
  if (!icon || !val) return;
  icon.className = 'health-icon ' + (ok ? 'ok' : 'fail');
  icon.textContent = ok ? '●' : '●';
  val.textContent = label;
  val.style.color = ok ? 'var(--green)' : 'var(--red)';
}

async function loadSystemHealth() {
  try {
    const r = await fetch('/api/system-health');
    if (!r.ok) return;
    const d = await r.json();

    // Uptime
    const up = $('health-uptime');
    if (up) up.textContent = `Uptime: ${fmtUptime(d.uptime_sec || 0)}`;

    // Bot
    setHealthItem('hicon-bot', 'hval-bot', true, 'RUNNING');

    // Binance WS
    setHealthItem('hicon-ws', 'hval-ws', d.binance_ws, d.binance_ws ? 'Connected' : 'Disconnected');

    // OpenClaw
    setHealthItem('hicon-ai', 'hval-ai', d.openclaw, d.openclaw ? 'Connected' : 'Offline');

    // Telegram
    setHealthItem('hicon-tg', 'hval-tg', d.telegram, d.telegram ? 'Active' : 'Disabled');

    // Mode
    const modeEl = $('hval-mode');
    if (modeEl) {
      modeEl.textContent = d.system_mode || '—';
      modeEl.style.color = d.kill_switch ? 'var(--red)' : 'var(--text-primary)';
    }

  } catch (e) {
    // Dashboard temporarily unreachable — don't spam errors
  }
}

// ============================================================
// Settings Panel
// ============================================================

let _currentSymbols = [];

async function loadSettings() {
  try {
    const r = await fetch('/api/settings');
    if (!r.ok) return;
    const d = await r.json();

    // Mode buttons
    ['OBSERVE','LIMITED','ACTIVE','BLOCKED'].forEach(m => {
      const btn = document.getElementById('mode-btn-' + m);
      if (!btn) return;
      btn.classList.toggle('mode-btn-active', d.system_mode === m);
    });

    // Kill switch status
    const ksLine = document.getElementById('ks-status-line');
    if (ksLine) {
      ksLine.textContent = d.kill_switch ? '🔴 현재 활성 — 신규 진입 차단 중' : '🟢 해제 상태';
      ksLine.style.color = d.kill_switch ? 'var(--red)' : 'var(--green)';
    }

    // AI toggle
    const aiBox = document.getElementById('toggle-ai');
    const aiTxt = document.getElementById('toggle-ai-text');
    if (aiBox) aiBox.checked = d.ai_enabled;
    if (aiTxt) aiTxt.textContent = d.ai_enabled ? 'ON' : 'OFF';

    // Symbols
    _currentSymbols = d.tracked_symbols || [];
    renderSymbolTags();
  } catch (e) {}
}

function renderSymbolTags() {
  const wrap = document.getElementById('symbol-tags');
  if (!wrap) return;
  wrap.innerHTML = '';
  _currentSymbols.forEach(sym => {
    const tag = document.createElement('span');
    tag.className = 'symbol-tag';
    tag.textContent = sym;
    tag.title = '클릭하여 제거';
    tag.onclick = () => {
      _currentSymbols = _currentSymbols.filter(s => s !== sym);
      renderSymbolTags();
    };
    wrap.appendChild(tag);
  });
}

function addSymbol() {
  const inp = document.getElementById('symbol-input');
  if (!inp) return;
  const sym = inp.value.trim().toUpperCase();
  if (!sym) return;
  if (!_currentSymbols.includes(sym)) {
    _currentSymbols.push(sym);
    renderSymbolTags();
  }
  inp.value = '';
}

async function saveSymbols() {
  await applySetting({ tracked_symbols: _currentSymbols }, '심볼 저장됨');
}

async function setMode(mode) {
  await applySetting({ system_mode: mode }, `모드 변경: ${mode}`);
  await loadSettings();
}

async function triggerKillSwitch(active) {
  const msg = active ? '🚨 Kill Switch를 활성화합니까?' : '✅ Kill Switch를 해제합니까?';
  if (!confirm(msg)) return;
  await applySetting({ kill_switch: active }, active ? 'Kill Switch 활성화' : 'Kill Switch 해제');
  await loadSettings();
}

async function setAI(enabled) {
  const aiTxt = document.getElementById('toggle-ai-text');
  if (aiTxt) aiTxt.textContent = enabled ? 'ON' : 'OFF';
  await applySetting({ ai_enabled: enabled }, `AI 분석 ${enabled ? '활성화' : '비활성화'}`);
}

async function applySetting(body, successMsg) {
  const statusEl = document.getElementById('settings-save-status');
  try {
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (r.ok) {
      showToast('success', '설정 저장', successMsg, 2500);
      if (statusEl) { statusEl.textContent = '✓ 저장됨'; statusEl.style.color = 'var(--green)'; }
      setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 3000);
    } else {
      const err = await r.json().catch(() => ({}));
      showToast('error', '설정 오류', err.detail || '저장 실패', 3000);
    }
  } catch (e) {
    showToast('error', '네트워크 오류', e.message, 3000);
  }
}

// Settings 초기 로드 및 주기적 갱신
loadSettings();
setInterval(loadSettings, 15000);

// ============================================================
// Strategy Params Panel
// ============================================================

// 전략별 파라미터 메타데이터 (label, type, step, min, max)
const STRATEGY_PARAM_META = {
  overreaction_reversal: {
    enabled:          { label: '활성화', type: 'bool' },
    tp_pct:           { label: 'TP (%)', type: 'pct', step: 0.001, min: 0.001, max: 0.2 },
    sl_pct:           { label: 'SL (%)', type: 'pct', step: 0.001, min: 0.001, max: 0.1 },
    rsi_oversold:     { label: 'RSI 과매도', type: 'num', step: 0.5, min: 10, max: 40 },
    rsi_overbought:   { label: 'RSI 과매수', type: 'num', step: 0.5, min: 60, max: 90 },
    overreaction_pct: { label: '급등락 임계 (%)', type: 'pct', step: 0.005, min: 0.01, max: 0.1 },
    lookback_bars:    { label: 'Lookback 봉', type: 'int', step: 1, min: 1, max: 10 },
    ema_long:         { label: 'EMA 장기', type: 'int', step: 1, min: 50, max: 500 },
  },
  volatility_expansion_breakout: {
    enabled:       { label: '활성화', type: 'bool' },
    tp_ratio:      { label: 'TP Ratio', type: 'num', step: 0.05, min: 0.1, max: 2.0 },
    sl_offset:     { label: 'SL Offset (%)', type: 'pct', step: 0.001, min: 0.001, max: 0.05 },
    vol_mult:      { label: '거래량 배수', type: 'num', step: 0.1, min: 1.0, max: 5.0 },
    squeeze_ratio: { label: 'Squeeze 비율', type: 'num', step: 0.01, min: 1.0, max: 1.5 },
    expand_ratio:  { label: 'Expand 비율', type: 'num', step: 0.01, min: 1.0, max: 2.0 },
    bb_period:     { label: 'BB 기간', type: 'int', step: 1, min: 5, max: 50 },
  },
  early_trend_capture: {
    enabled:   { label: '활성화', type: 'bool' },
    tp_pct:    { label: 'TP (%)', type: 'pct', step: 0.001, min: 0.001, max: 0.2 },
    sl_pct:    { label: 'SL (%)', type: 'pct', step: 0.001, min: 0.001, max: 0.1 },
    rsi_low:   { label: 'RSI 하한', type: 'num', step: 1, min: 20, max: 50 },
    rsi_high:  { label: 'RSI 상한', type: 'num', step: 1, min: 50, max: 80 },
    vol_mult:  { label: '거래량 배수', type: 'num', step: 0.1, min: 1.0, max: 5.0 },
    ema_fast:  { label: 'EMA 단기', type: 'int', step: 1, min: 5, max: 50 },
    ema_slow:  { label: 'EMA 장기', type: 'int', step: 1, min: 20, max: 200 },
    atr_period:{ label: 'ATR 기간', type: 'int', step: 1, min: 5, max: 30 },
    atr_mult:  { label: 'ATR 배수', type: 'num', step: 0.05, min: 1.0, max: 3.0 },
  },
};

const STRATEGY_DISPLAY_NAMES = {
  overreaction_reversal:          'Overreaction Reversal',
  volatility_expansion_breakout:  'Volatility Breakout',
  early_trend_capture:            'Early Trend',
};

let _strategyParams = {};
let _activeStrategyTab = null;

async function loadStrategyParams() {
  try {
    const r = await fetch('/api/strategy-params');
    if (!r.ok) return;
    _strategyParams = await r.json();

    // 글로벌 파라미터 필드 채우기
    const g = _strategyParams.global || {};
    for (const [key, val] of Object.entries(g)) {
      const el = document.getElementById('gp-' + key);
      if (el) el.value = val;
    }

    // 탭 렌더링
    renderStrategyTabs();
    if (_activeStrategyTab) renderStrategyParamForm(_activeStrategyTab);
    else if (Object.keys(STRATEGY_PARAM_META).length > 0) {
      _activeStrategyTab = Object.keys(STRATEGY_PARAM_META)[0];
      renderStrategyParamForm(_activeStrategyTab);
    }
  } catch (e) {}
}

function renderStrategyTabs() {
  const container = document.getElementById('strategy-tabs');
  if (!container) return;
  container.innerHTML = '';
  for (const stratName of Object.keys(STRATEGY_PARAM_META)) {
    const params = (_strategyParams.strategies || {})[stratName] || {};
    const enabled = params.enabled !== false;
    const btn = document.createElement('button');
    btn.className = 'strategy-tab' + (stratName === _activeStrategyTab ? ' active' : '') + (!enabled ? ' paused' : '');
    btn.textContent = STRATEGY_DISPLAY_NAMES[stratName] || stratName;
    if (!enabled) btn.title = '비활성화됨';
    btn.onclick = () => {
      _activeStrategyTab = stratName;
      renderStrategyTabs();
      renderStrategyParamForm(stratName);
    };
    container.appendChild(btn);
  }
}

function renderStrategyParamForm(stratName) {
  const grid = document.getElementById('strategy-param-grid');
  if (!grid) return;
  grid.innerHTML = '';

  const meta = STRATEGY_PARAM_META[stratName] || {};
  const current = Object.assign(
    {},
    (_strategyParams.strategies || {})[stratName] || {}
  );

  for (const [key, m] of Object.entries(meta)) {
    const val = current[key] !== undefined ? current[key] : '';
    const item = document.createElement('div');
    item.className = 'param-item';

    const label = document.createElement('label');
    label.className = 'param-label';
    label.textContent = m.label;

    let input;
    if (m.type === 'bool') {
      // 활성화 토글: select로 구현
      input = document.createElement('select');
      input.className = 'param-input';
      input.id = 'sp-' + key;
      const optOn  = new Option('활성화', 'true');
      const optOff = new Option('비활성화', 'false');
      input.append(optOn, optOff);
      input.value = String(val !== '' ? val : true);
    } else {
      input = document.createElement('input');
      input.className = 'param-input';
      input.id = 'sp-' + key;
      input.type = 'number';
      if (m.step)  input.step = m.step;
      if (m.min !== undefined) input.min = m.min;
      if (m.max !== undefined) input.max = m.max;
      // pct 타입: 내부는 소수, 표시도 소수 (0.025 = 2.5%)
      input.value = val !== '' ? val : '';
      input.placeholder = val !== '' ? '' : '(기본값 사용)';
    }

    item.append(label, input);
    grid.appendChild(item);
  }
}

async function saveGlobalParams() {
  const g = {};
  const fields = {
    default_tp_pct:       parseFloat,
    default_sl_pct:       parseFloat,
    min_score_execute:    parseInt,
    max_active_positions: parseInt,
  };
  for (const [key, parser] of Object.entries(fields)) {
    const el = document.getElementById('gp-' + key);
    if (el && el.value !== '') g[key] = parser(el.value);
  }
  if (Object.keys(g).length === 0) return;

  const statusEl = document.getElementById('settings-save-status');
  try {
    const r = await fetch('/api/strategy-params/global', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(g),
    });
    if (r.ok) {
      showToast('success', '글로벌 파라미터', '저장됨', 2500);
      if (statusEl) { statusEl.textContent = '✓ 저장됨'; statusEl.style.color = 'var(--green)'; setTimeout(() => { statusEl.textContent = ''; }, 3000); }
      await loadStrategyParams();
    } else {
      const err = await r.json().catch(() => ({}));
      showToast('error', '저장 오류', err.detail || '실패', 3000);
    }
  } catch (e) {
    showToast('error', '네트워크 오류', e.message, 3000);
  }
}

async function saveStrategyParams() {
  if (!_activeStrategyTab) return;
  const meta = STRATEGY_PARAM_META[_activeStrategyTab] || {};
  const updates = {};

  for (const [key, m] of Object.entries(meta)) {
    const el = document.getElementById('sp-' + key);
    if (!el || el.value === '') continue;
    if (m.type === 'bool')    updates[key] = el.value === 'true';
    else if (m.type === 'int') updates[key] = parseInt(el.value);
    else                       updates[key] = parseFloat(el.value);
  }
  if (Object.keys(updates).length === 0) return;

  const statusEl = document.getElementById('settings-save-status');
  try {
    const r = await fetch('/api/strategy-params/' + _activeStrategyTab, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates),
    });
    if (r.ok) {
      showToast('success', '전략 파라미터', `${STRATEGY_DISPLAY_NAMES[_activeStrategyTab] || _activeStrategyTab} 저장됨`, 2500);
      if (statusEl) { statusEl.textContent = '✓ 저장됨'; statusEl.style.color = 'var(--green)'; setTimeout(() => { statusEl.textContent = ''; }, 3000); }
      await loadStrategyParams();
    } else {
      const err = await r.json().catch(() => ({}));
      showToast('error', '저장 오류', err.detail || '실패', 3000);
    }
  } catch (e) {
    showToast('error', '네트워크 오류', e.message, 3000);
  }
}

async function resetStrategyParams() {
  if (!_activeStrategyTab) return;
  const name = STRATEGY_DISPLAY_NAMES[_activeStrategyTab] || _activeStrategyTab;
  if (!confirm(`'${name}' 파라미터를 기본값으로 초기화합니까?`)) return;

  try {
    const r = await fetch('/api/strategy-params/' + _activeStrategyTab + '/reset', { method: 'POST' });
    if (r.ok) {
      showToast('success', '초기화', `${name} 기본값 복원됨`, 2500);
      await loadStrategyParams();
    } else {
      const err = await r.json().catch(() => ({}));
      showToast('error', '오류', err.detail || '실패', 3000);
    }
  } catch (e) {
    showToast('error', '네트워크 오류', e.message, 3000);
  }
}

// 초기 로드
loadStrategyParams();
setInterval(loadStrategyParams, 30000);

async function confirmRestart() {
  document.getElementById('restart-modal').style.display = 'none';
  showToast('success', 'Restarting', 'Bot process will restart in ~2s…', 3000);
  try {
    await fetch('/api/restart', { method: 'POST' });
  } catch (e) {
    // connection dropped is expected on restart
  }
  showToast('success', 'Dashboard Reloading', 'Reconnecting in 6 seconds…', 6000);
  setTimeout(() => location.reload(), 6000);
}

// ============================================================
// Image Pattern Strategy Panel
// ============================================================
const _ipt = {
  file:     null,
  analysis: null,
  imgB64:   null,
  contentType: null,
};

function iptDragOver(e) {
  e.preventDefault();
  $('ipt-dropzone').classList.add('drag-over');
}
function iptDragLeave(e) {
  $('ipt-dropzone').classList.remove('drag-over');
}
function iptDrop(e) {
  e.preventDefault();
  $('ipt-dropzone').classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file) iptSetFile(file);
}
function iptFileSelected(e) {
  const file = e.target.files[0];
  if (file) iptSetFile(file);
}

function iptSetFile(file) {
  if (!file.type.startsWith('image/')) {
    showToast('error', '오류', '이미지 파일만 업로드 가능합니다', 3000);
    return;
  }
  _ipt.file = file;
  _ipt.contentType = file.type;

  const reader = new FileReader();
  reader.onload = (e) => {
    const img = $('ipt-preview-img');
    img.src = e.target.result;
    $('ipt-preview-wrap').style.display = 'block';
    $('ipt-dropzone-hint').style.display = 'none';
    $('ipt-analyze-btn').disabled = false;
  };
  reader.readAsDataURL(file);
}

async function iptAnalyze() {
  if (!_ipt.file) return;
  const btn = $('ipt-analyze-btn');
  btn.disabled = true;
  btn.textContent = '⏳ AI 분석 중…';

  const formData = new FormData();
  formData.append('file',        _ipt.file);
  formData.append('symbol',      $('ipt-symbol').value);
  formData.append('timeframe',   $('ipt-timeframe').value);
  formData.append('description', $('ipt-description').value);

  try {
    const resp = await fetch('/api/image-pattern/analyze', {
      method: 'POST',
      body:   formData,
    });
    const data = await resp.json();

    if (!resp.ok || !data.ok) {
      showToast('error', 'AI 분석 실패', data.detail || data.error || '오류 발생', 5000);
      return;
    }
    _ipt.analysis    = data.analysis;
    _ipt.imgB64      = data.image_b64;
    _ipt.contentType = data.content_type;
    iptRenderResult(data.analysis);
    showToast('success', 'AI 분석 완료', data.analysis.pattern_name, 3000);
  } catch (e) {
    showToast('error', '네트워크 오류', e.message, 4000);
  } finally {
    btn.disabled = false;
    btn.textContent = '🤖 AI 분석 시작';
  }
}

function iptRenderResult(a) {
  const sec = $('ipt-result-section');
  if (!sec) return;
  sec.style.display = 'block';

  // 요약 카드
  const dirColor = a.direction === 'LONG' ? 'var(--green)' : 'var(--red)';
  $('ipt-result-grid').innerHTML = `
    <div class="ipt-result-card">
      <span class="ipt-label">패턴명</span>
      <div class="ipt-val" style="font-size:13px">${escapeHtml(a.pattern_name || '—')}</div>
    </div>
    <div class="ipt-result-card">
      <span class="ipt-label">방향</span>
      <div class="ipt-val" style="color:${dirColor}">${a.direction || '—'}</div>
    </div>
    <div class="ipt-result-card">
      <span class="ipt-label">TP %</span>
      <div class="ipt-val highlight-green">+${(a.tp_pct || 3).toFixed(1)}%</div>
    </div>
    <div class="ipt-result-card">
      <span class="ipt-label">SL %</span>
      <div class="ipt-val highlight-red">-${(a.sl_pct || 1.5).toFixed(1)}%</div>
    </div>
    <div class="ipt-result-card">
      <span class="ipt-label">쿨다운</span>
      <div class="ipt-val">${(a.cooldown_hours || 4)}h</div>
    </div>
    <div class="ipt-result-card">
      <span class="ipt-label">신뢰도</span>
      <div class="ipt-val">${((a.min_confidence || 0.65) * 100).toFixed(0)}%</div>
    </div>`;

  // 로직 배지
  const logicBadge = $('ipt-logic-badge');
  if (logicBadge) logicBadge.textContent = a.conditions_logic || 'AND';

  // 조건 목록
  const condList = $('ipt-conditions-list');
  if (condList) {
    const conds = a.conditions || [];
    if (conds.length === 0) {
      condList.innerHTML = '<div class="text-muted" style="font-size:12px">조건 없음</div>';
    } else {
      condList.innerHTML = conds.map(c => {
        const params = Object.entries(c)
          .filter(([k]) => k !== 'type')
          .map(([k, v]) => `${k}=${v}`)
          .join(', ');
        return `<div class="ipt-condition-item">
          <span class="ipt-condition-type">${escapeHtml(c.type)}</span>
          <span class="ipt-condition-params">${escapeHtml(params)}</span>
        </div>`;
      }).join('');
    }
  }

  // 경고
  const warns = a.warnings || [];
  const warnSec = $('ipt-warnings-section');
  if (warnSec) {
    warnSec.style.display = warns.length > 0 ? 'block' : 'none';
    $('ipt-warnings-list').innerHTML = warns.map(w => `<li>${escapeHtml(w)}</li>`).join('');
  }
}

async function iptSave() {
  if (!_ipt.analysis) return;
  const btn = $('ipt-save-btn');
  btn.disabled = true;
  btn.textContent = '저장 중…';

  try {
    const resp = await fetch('/api/image-pattern/save', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        analysis:     _ipt.analysis,
        symbol:       $('ipt-symbol').value,
        interval:     $('ipt-timeframe').value,
        image_b64:    _ipt.imgB64,
        content_type: _ipt.contentType,
      }),
    });
    const data = await resp.json();
    if (data.ok) {
      showToast('success', '저장 완료', `전략 ID: ${data.id.slice(0,8)}…`, 3000);
      iptReset();
      iptLoadPatterns();
    } else {
      showToast('error', '저장 실패', data.detail || '오류', 3000);
    }
  } catch (e) {
    showToast('error', '네트워크 오류', e.message, 3000);
  } finally {
    btn.disabled = false;
    btn.textContent = '✅ 전략으로 저장';
  }
}

function iptReset() {
  _ipt.file = null; _ipt.analysis = null; _ipt.imgB64 = null;
  $('ipt-preview-wrap').style.display  = 'none';
  $('ipt-dropzone-hint').style.display = 'block';
  $('ipt-result-section').style.display = 'none';
  $('ipt-analyze-btn').disabled = true;
  $('ipt-file-input').value = '';
  $('ipt-description').value = '';
}

async function iptLoadPatterns() {
  try {
    const resp = await fetch('/api/image-patterns');
    if (!resp.ok) return;
    const patterns = await resp.json();
    iptRenderPatterns(patterns);
  } catch (e) {
    console.debug('iptLoadPatterns error:', e);
  }
}

function iptRenderPatterns(patterns) {
  const container = $('ipt-patterns-list');
  if (!container) return;
  if (!patterns || patterns.length === 0) {
    container.innerHTML = '<div class="text-muted" style="font-size:12px;padding:16px;text-align:center">저장된 패턴이 없습니다</div>';
    return;
  }
  container.innerHTML = patterns.map(p => {
    const enabled   = p.enabled === 1 || p.enabled === true;
    const dirColor  = p.direction === 'LONG' ? 'var(--green)' : 'var(--red)';
    const rfJson    = p.regime_filter_json;
    let rfText = '전체 레짐';
    if (rfJson) { try { const rf = JSON.parse(rfJson); rfText = rf.join(', ') || '전체'; } catch(e){} }
    const conds = (() => { try { return JSON.parse(p.conditions_json || '[]'); } catch(e){ return []; } })();
    const ts    = p.created_at ? new Date(p.created_at).toLocaleDateString('ko-KR') : '—';
    return `
      <div class="ipt-pattern-card${enabled ? '' : ' disabled'}">
        <div class="ipt-pattern-info">
          <div class="ipt-pattern-name">${escapeHtml(p.pattern_name)}</div>
          <div class="ipt-pattern-desc">${escapeHtml(p.description || '—')}</div>
          <div class="ipt-pattern-meta">
            <span style="color:${dirColor}">${p.direction}</span>
            <span>TP +${(p.tp_pct||3).toFixed(1)}%</span>
            <span>SL -${(p.sl_pct||1.5).toFixed(1)}%</span>
            <span>${p.symbol} · ${p.interval}</span>
            <span>${conds.length}개 조건</span>
            <span>${rfText}</span>
            <span>생성: ${ts}</span>
          </div>
        </div>
        <div class="ipt-pattern-actions">
          <button class="btn-sm ${enabled ? 'btn-danger' : ''}"
            onclick="iptToggle('${escapeHtml(p.id)}')"
            title="${enabled ? '비활성화' : '활성화'}">
            ${enabled ? '⏸ 끄기' : '▶ 켜기'}
          </button>
          <button class="btn-sm"
            onclick="iptDelete('${escapeHtml(p.id)}', '${escapeHtml(p.pattern_name)}')">
            🗑 삭제
          </button>
        </div>
      </div>`;
  }).join('');
}

async function iptToggle(id) {
  try {
    const resp = await fetch(`/api/image-pattern/${id}/toggle`, { method: 'PATCH' });
    const data = await resp.json();
    showToast('success', data.enabled ? '활성화' : '비활성화', '패턴 전략 상태 변경', 2000);
    iptLoadPatterns();
  } catch (e) {
    showToast('error', '오류', e.message, 3000);
  }
}

async function iptDelete(id, name) {
  if (!confirm(`'${name}' 패턴을 삭제하시겠습니까?`)) return;
  try {
    const resp = await fetch(`/api/image-pattern/${id}`, { method: 'DELETE' });
    const data = await resp.json();
    if (data.ok) {
      showToast('success', '삭제됨', name, 2000);
      iptLoadPatterns();
    }
  } catch (e) {
    showToast('error', '오류', e.message, 3000);
  }
}

// 초기 로드
iptLoadPatterns();

// ============================================================
// Paper Performance Panel
// ============================================================
async function refreshPerformance() {
  const inp = $('perf-capital-input');
  const capital = inp ? (parseFloat(inp.value) || 1000) : 1000;
  try {
    const resp = await fetch(`/api/paper-performance?capital=${capital}`);
    if (!resp.ok) return;
    const d = await resp.json();
    renderPerformance(d);
  } catch (e) {
    // silently ignore — panel shows dashes on error
  }
}

function renderPerformance(d) {
  const capital = d.capital ?? 1000;
  const total   = d.total_trades ?? 0;

  // Win Rate
  const wrEl = $('perf-win-rate');
  if (wrEl) {
    if (total === 0) {
      wrEl.textContent = '—';
      wrEl.className = 'perf-value';
    } else {
      wrEl.textContent = (d.win_rate * 100).toFixed(1) + '%';
      wrEl.className   = 'perf-value ' + (d.win_rate >= 0.5 ? 'highlight-green' : 'highlight-red');
    }
  }
  setText('perf-win-loss', total === 0 ? '— 승 / — 패' : `${d.wins} 승 / ${d.losses} 패`);

  // Total trades
  setText('perf-total-trades', total === 0 ? '—' : total);
  setText('perf-trade-sub', total === 0 ? 'paper trades' : `거래당 $${(d.per_trade_alloc ?? 0).toFixed(0)} 배분`);

  // Net P&L USD
  const pnlEl = $('perf-net-pnl-usd');
  if (pnlEl) {
    if (total === 0) {
      pnlEl.textContent = '—';
      pnlEl.className = 'perf-value';
    } else {
      const pnl = d.net_pnl_usd ?? 0;
      pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(2);
      pnlEl.className   = 'perf-value ' + (pnl >= 0 ? 'highlight-green' : 'highlight-red');
    }
  }
  const pnlPct = d.net_pnl_pct ?? 0;
  setText('perf-net-pnl-pct', total === 0 ? '—' : `${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%`);

  // Final balance
  const fbEl = $('perf-final-balance');
  if (fbEl) {
    if (total === 0) {
      fbEl.textContent = '$' + capital.toFixed(2);
      fbEl.className = 'perf-value';
    } else {
      const fb = d.final_balance ?? capital;
      fbEl.textContent = '$' + fb.toFixed(2);
      fbEl.className   = 'perf-value ' + (fb >= capital ? 'highlight-green' : 'highlight-red');
    }
  }
  setText('perf-capital-sub', `$${capital.toLocaleString(undefined, {maximumFractionDigits:0})} 기준`);

  // Profit Factor
  const pfEl = $('perf-profit-factor');
  if (pfEl) {
    if (d.profit_factor == null) {
      pfEl.textContent = '—';
      pfEl.className = 'perf-value';
    } else {
      pfEl.textContent = d.profit_factor.toFixed(2);
      pfEl.className   = 'perf-value ' + (d.profit_factor >= 1.5 ? 'highlight-green' : d.profit_factor >= 1 ? 'highlight-yellow' : 'highlight-red');
    }
  }

  // Max Drawdown
  const ddEl = $('perf-max-dd');
  if (ddEl) {
    const dd = d.max_drawdown_usd ?? 0;
    ddEl.textContent = total === 0 ? '—' : '$' + dd.toFixed(2);
    ddEl.className   = total === 0 ? 'perf-value' : 'perf-value ' + (dd < 0 ? 'highlight-red' : '');
  }
  const expVal = (d.expectancy_usd == null || total === 0)
    ? '기대값: —'
    : `기대값: ${d.expectancy_usd >= 0 ? '+' : ''}$${d.expectancy_usd.toFixed(2)} / 거래`;
  setText('perf-expectancy', expVal);

  // Per-strategy table
  const tbody = $('perf-strategy-tbody');
  if (!tbody) return;
  const strategies = d.per_strategy ?? {};
  const names = Object.keys(strategies);

  if (names.length === 0) {
    tbody.innerHTML = '<tr class="signals-empty-row"><td colspan="7">거래 기록 없음 — 전략이 포지션을 닫으면 표시됩니다</td></tr>';
    return;
  }

  names.sort((a, b) => (strategies[b].net_pnl_usd ?? 0) - (strategies[a].net_pnl_usd ?? 0));

  tbody.innerHTML = names.map(name => {
    const s = strategies[name];
    const wr = (s.win_rate * 100).toFixed(1) + '%';
    const wrCls = s.win_rate >= 0.5 ? 'highlight-green' : 'highlight-red';
    const pnlCls = s.net_pnl_usd >= 0 ? 'highlight-green' : 'highlight-red';
    const pnlSign = s.net_pnl_usd >= 0 ? '+' : '';
    const label = name.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    return `
      <tr>
        <td><span class="text-mono" style="font-size:11px">${label}</span></td>
        <td style="text-align:center">${s.trades}</td>
        <td style="text-align:center">${s.wins}W / ${s.losses}L</td>
        <td style="text-align:center"><span class="${wrCls}">${wr}</span></td>
        <td style="text-align:right"><span class="highlight-green">+${s.avg_win_pct.toFixed(2)}%</span></td>
        <td style="text-align:right"><span class="highlight-red">${s.avg_loss_pct.toFixed(2)}%</span></td>
        <td style="text-align:right"><span class="${pnlCls}">${pnlSign}$${Math.abs(s.net_pnl_usd).toFixed(2)}</span></td>
      </tr>`;
  }).join('');
}

// 초기 로드 및 60초마다 갱신
refreshPerformance();
setInterval(refreshPerformance, 60000);

// ============================================================
// USER CONTROL — Regime Override
// ============================================================

let _currentRegimeOverride = null;  // null = auto

async function loadRegimeOverride() {
  try {
    const res = await fetch('/api/regime/override');
    if (!res.ok) return;
    const data = await res.json();
    _currentRegimeOverride = data.override;
    _updateRegimeOverrideUI(data);
  } catch(e) {}
}

function _updateRegimeOverrideUI(data) {
  const override = data.override;
  const botRegime = data.bot_regime || '—';

  // 봇 판단 표시
  const botEl = document.getElementById('regime-bot-judge');
  if (botEl) botEl.textContent = botRegime;

  // 오버라이드 배지
  const badge = document.getElementById('regime-override-badge');
  if (badge) badge.style.display = override ? 'inline' : 'none';

  // 노트 텍스트
  const note = document.getElementById('regime-override-note');
  if (note) {
    note.textContent = override
      ? `수동 오버라이드 중: ${override} (봇 판단: ${botRegime})`
      : `현재: 자동 (봇 판단 사용 중 → ${botRegime})`;
    note.style.color = override ? 'var(--yellow)' : 'var(--text-muted)';
  }

  // 버튼 active 상태
  document.querySelectorAll('.regime-sel-btn').forEach(btn => {
    const r = btn.dataset.regime;
    const isActive = override ? r === override : r === 'AUTO';
    btn.classList.toggle('active', isActive);
  });
}

async function setRegimeOverride(regime) {
  try {
    let res;
    if (regime === 'AUTO') {
      res = await fetch('/api/regime/override', { method: 'DELETE' });
    } else {
      res = await fetch('/api/regime/override', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ regime }),
      });
    }
    if (!res.ok) {
      const err = await res.json();
      alert(`레짐 오버라이드 실패: ${err.detail || JSON.stringify(err)}`);
      return;
    }
    await loadRegimeOverride();
  } catch(e) {
    alert(`오류: ${e.message}`);
  }
}

// WebSocket에서 regime_override 이벤트 처리
function _handleRegimeOverrideEvent(data) {
  _currentRegimeOverride = data.override;
  loadRegimeOverride();
}

// ============================================================
// USER CONTROL — Exchange Mode
// ============================================================

async function loadExchangeMode() {
  try {
    const res = await fetch('/api/exchange-mode');
    if (!res.ok) return;
    const data = await res.json();
    _applyExchangeModeUI(data.exchange_mode, data.hyperliquid_available);
  } catch(e) {}
}

function _applyExchangeModeUI(mode, hlAvailable) {
  // 라디오 버튼 선택
  document.querySelectorAll('input[name="exchange_mode"]').forEach(radio => {
    radio.checked = radio.value === mode;
  });

  // 선택된 옵션 하이라이트
  document.querySelectorAll('.exchange-option').forEach(el => {
    const val = el.querySelector('input')?.value;
    el.classList.toggle('selected', val === mode);
  });

  // Hyperliquid 미사용 경고
  const warn = document.getElementById('exchange-hl-warn');
  if (warn) {
    if (!hlAvailable && (mode === 'BOTH' || mode === 'HYPERLIQUID_ONLY')) {
      warn.textContent = '⚠ Hyperliquid가 초기화되지 않았습니다. config에서 HYPERLIQUID_ENABLED=true 설정 필요.';
      warn.style.display = 'block';
    } else {
      warn.style.display = 'none';
    }
  }

  const note = document.getElementById('exchange-mode-note');
  const labels = { BOTH: 'Binance + Hyperliquid', BINANCE_ONLY: 'Binance 전용', HYPERLIQUID_ONLY: 'Hyperliquid 전용' };
  if (note) note.textContent = `현재: ${labels[mode] || mode}`;
}

async function setExchangeMode(mode) {
  try {
    const res = await fetch('/api/exchange-mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    });
    if (!res.ok) {
      const err = await res.json();
      alert(`거래소 선택 실패: ${err.detail || JSON.stringify(err)}`);
      await loadExchangeMode();  // 원래 상태로 복원
      return;
    }
    await loadExchangeMode();
  } catch(e) {
    alert(`오류: ${e.message}`);
  }
}

// ============================================================
// USER CONTROL — Strategy Recommendations
// ============================================================

async function loadStrategyRecommendations() {
  try {
    const res = await fetch('/api/strategy-recommendations');
    if (!res.ok) return;
    const data = await res.json();
    _renderStrategyRecommendations(data.pending || []);
  } catch(e) {}
}

function _renderStrategyRecommendations(recs) {
  const container = document.getElementById('strategy-recommendations-container');
  if (!container) return;

  if (!recs.length) {
    container.innerHTML = `
      <div style="color:var(--text-muted);font-size:13px;text-align:center;padding:20px">
        현재 추천 사항이 없습니다. 거래 데이터가 쌓이면 자동으로 생성됩니다.
      </div>`;
    return;
  }

  const typeIcon = {
    ACTIVATE: '▶',
    PROMOTE:  '⬆',
    SUSPEND:  '⏸',
    ADJUST:   '⚙',
    FOCUS:    '🎯',
  };

  container.innerHTML = recs.map(rec => {
    const icon = typeIcon[rec.rec_type] || '●';
    const data = rec.supporting_data || {};
    const dataTags = Object.entries(data)
      .filter(([k]) => !['current_regime'].includes(k))
      .map(([k, v]) => {
        if (typeof v === 'number') v = v.toFixed ? v.toFixed(3) : v;
        return `<span class="rec-data-item">${k}: ${v}</span>`;
      }).join('');

    return `
      <div class="rec-card" id="rec-${rec.id}">
        <div class="rec-card-header">
          <span class="rec-type-badge rec-type-${rec.rec_type}">${icon} ${rec.rec_type}</span>
          <span class="rec-card-title">${escHtml(rec.title)}</span>
        </div>
        <div class="rec-card-reasoning">${escHtml(rec.reasoning)}</div>
        ${dataTags ? `<div class="rec-card-data">${dataTags}</div>` : ''}
        <div class="rec-card-actions">
          <button class="rec-btn-approve" onclick="decideRecommendation('${rec.id}', true)">
            ✓ 적용
          </button>
          <button class="rec-btn-reject" onclick="decideRecommendation('${rec.id}', false)">
            ✗ 무시
          </button>
        </div>
      </div>`;
  }).join('');
}

async function decideRecommendation(recId, approved) {
  const reason = approved
    ? '대시보드에서 승인'
    : prompt('거절 이유 (선택사항)', '') ?? '대시보드에서 거절';

  try {
    const res = await fetch(`/api/strategy-recommendations/${recId}/decide`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        approved,
        decided_by: 'dashboard_operator',
        reason,
      }),
    });
    if (!res.ok) {
      const err = await res.json();
      alert(`오류: ${err.detail}`);
      return;
    }
    // 카드 제거 애니메이션
    const card = document.getElementById(`rec-${recId}`);
    if (card) {
      card.style.opacity = '0';
      card.style.transition = 'opacity .3s';
      setTimeout(() => loadStrategyRecommendations(), 350);
    }
  } catch(e) {
    alert(`오류: ${e.message}`);
  }
}

async function refreshStrategyRecommendations() {
  try {
    await fetch('/api/strategy-recommendations/refresh', { method: 'POST' });
    setTimeout(loadStrategyRecommendations, 500);
  } catch(e) {}
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ============================================================
// 초기 로드 및 주기 갱신
// ============================================================
loadRegimeOverride();
loadExchangeMode();
loadStrategyRecommendations();
setInterval(loadStrategyRecommendations, 5 * 60 * 1000);  // 5분마다 갱신
