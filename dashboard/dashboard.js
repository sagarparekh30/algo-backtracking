const API = window.location.origin;
let prevPx = {};

// ══════════════════════════════════════════════════════
// AUTH
// ══════════════════════════════════════════════════════

const AUTH_KEY = 'apex_token';

// ── Symbol Autocomplete ──────────────────────────────────────────────────────
// Usage: attachSymbolSearch('input-id', 'list-id', onSelectCallback)
function attachSymbolSearch(inputId, listId, onSelect) {
  const inp  = document.getElementById(inputId);
  const list = document.getElementById(listId);
  if (!inp || !list) return;

  let activeIdx = -1;
  let debounceT = null;

  function closeDrop() {
    list.classList.remove('open');
    activeIdx = -1;
  }

  function renderItems(symbols) {
    if (!symbols.length) { closeDrop(); return; }
    list.innerHTML = symbols.map((s, i) =>
      `<div class="ac-item" data-sym="${s}" data-idx="${i}">${s}</div>`
    ).join('');
    list.classList.add('open');
    activeIdx = -1;

    list.querySelectorAll('.ac-item').forEach(el => {
      el.addEventListener('mousedown', e => {
        e.preventDefault();
        inp.value = el.dataset.sym;
        closeDrop();
        if (onSelect) onSelect(el.dataset.sym);
      });
    });
  }

  async function fetchSuggestions(q) {
    if (!q) { closeDrop(); return; }
    try {
      const d = await (await fetch(`${API}/api/symbols/search?q=${encodeURIComponent(q)}`)).json();
      renderItems(d.symbols || []);
    } catch(_) { closeDrop(); }
  }

  inp.addEventListener('input', () => {
    clearTimeout(debounceT);
    const q = inp.value.trim();
    if (!q) { closeDrop(); return; }
    debounceT = setTimeout(() => fetchSuggestions(q), 180);
  });

  inp.addEventListener('keydown', e => {
    const items = list.querySelectorAll('.ac-item');
    if (!list.classList.contains('open') || !items.length) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      activeIdx = Math.min(activeIdx + 1, items.length - 1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      activeIdx = Math.max(activeIdx - 1, 0);
    } else if (e.key === 'Enter' && activeIdx >= 0) {
      e.preventDefault();
      inp.value = items[activeIdx].dataset.sym;
      closeDrop();
      if (onSelect) onSelect(inp.value);
      return;
    } else if (e.key === 'Escape') {
      closeDrop(); return;
    }
    items.forEach((el, i) => el.classList.toggle('ac-active', i === activeIdx));
  });

  document.addEventListener('click', e => {
    if (!inp.contains(e.target) && !list.contains(e.target)) closeDrop();
  });
}

// Wire up autocomplete on both inputs (called after DOM ready)
function initSymbolSearch() {
  attachSymbolSearch('bt-sym',   'bt-sym-list',   null);
  attachSymbolSearch('pa-sym',   'pa-sym-list',   null);
  attachSymbolSearch('wl-input', 'wl-input-list', sym => { addSym(sym); });
}
// ────────────────────────────────────────────────────────────────────────────

function getToken() { return localStorage.getItem(AUTH_KEY); }
function setToken(t) { localStorage.setItem(AUTH_KEY, t); }
function clearToken() { localStorage.removeItem(AUTH_KEY); localStorage.removeItem(AUTH_KEY + '_role'); localStorage.removeItem('apex_username'); }
function getRole()  { return localStorage.getItem(AUTH_KEY + '_role') || 'user'; }
function setRole(r) { localStorage.setItem(AUTH_KEY + '_role', r); }

function authHeaders() {
  const t = getToken();
  return t ? { 'Authorization': 'Bearer ' + t } : {};
}

/** Fetch wrapper that auto-includes auth header. Returns parsed JSON. */
async function jAuth(url, opts = {}) {
  opts.headers = { ...(opts.headers || {}), ...authHeaders() };
  const r = await fetch(url, opts);
  if (r.status === 401 || r.status === 403) {
    clearToken();
    showLogin();
    showLoginBtn();
    throw new Error('Session expired — please log in again.');
  }
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { const b = await r.json(); msg = b.detail || b.message || msg; } catch(_) {}
    throw new Error(msg);
  }
  return r.json();
}

function showLogin(msg) {
  const overlay = document.getElementById('login-overlay');
  overlay.style.display = 'flex';
  if (msg) {
    const err = document.getElementById('login-error');
    err.textContent = msg;
    err.style.display = 'block';
  }
  setTimeout(() => document.getElementById('login-user').focus(), 50);
}

function hideLogin() {
  document.getElementById('login-overlay').style.display = 'none';
  document.getElementById('login-error').style.display = 'none';
  const signinBtn = document.getElementById('signin-btn');
  if (signinBtn) signinBtn.style.display = 'none';
}

function showLoginBtn() {
  const signinBtn = document.getElementById('signin-btn');
  if (signinBtn) signinBtn.style.display = 'inline-block';
  const tokenChip = document.getElementById('token-chip');
  if (tokenChip) { tokenChip.querySelector('.chip-dot').style.background = 'var(--red)'; }
}

async function doLogin() {
  const btn  = document.getElementById('login-btn');
  const user = document.getElementById('login-user').value.trim();
  const pass = document.getElementById('login-pass').value;
  const err  = document.getElementById('login-error');
  if (!user || !pass) { err.textContent='Enter username and password'; err.style.display='block'; return; }

  btn.disabled = true;
  btn.textContent = 'Signing in…';
  try {
    const res = await fetch(`${API}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: user, password: pass }),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.detail || `Error ${res.status}`);
    }
    const d = await res.json();
    setToken(d.access_token);
    setRole(d.role || 'user');
    localStorage.setItem('apex_username', d.username || user);
    document.getElementById('login-pass').value = '';
    hideLogin();
    _showAdminBadge(d.username || user);
    _applyRoleUI(d.role || 'user', d.plan_type, d.plan_expiry);
    loadHome();
    _syncHomePicks();
  } catch(e) {
    err.textContent = e.message;
    err.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Sign In';
  }
}

function doLogout() {
  const token = getToken();
  // Revoke server-side in background — don't await, UI responds instantly
  if (token) {
    fetch(`${API}/api/auth/logout`, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + token },
    }).catch(() => {});
  }
  clearToken();
  showLogin();
  _showAdminBadge(null);
  _applyRoleUI('user');
}

function _showAdminBadge(username) {
  const el = document.getElementById('admin-badge');
  if (!el) return;
  if (username) {
    el.textContent = '👤 ' + username;
    el.style.display = 'inline-block';
  } else {
    el.style.display = 'none';
  }
  // Populate More sheet user info
  const nameEl = document.getElementById('more-sheet-username');
  if (nameEl) nameEl.textContent = username || '';
}

// Tabs restricted to admin only
const ADMIN_ONLY_TABS = ['execute', 'ml', 'data', 'admin'];

function _applyRoleUI(role, planType, planExpiry) {
  const isAdmin = role === 'admin';

  // ── Nav bar buttons ──────────────────────────────────────────────────
  const show = (id, visible) => {
    const el = document.getElementById(id);
    if (el) el.style.display = visible ? 'flex' : 'none';
  };
  show('nav-execute-btn',   isAdmin);
  show('nav-ml-btn',        isAdmin);
  show('nav-admin-btn',     isAdmin);

  // ── More sheet buttons ───────────────────────────────────────────────
  const showBlock = (id, visible) => {
    const el = document.getElementById(id);
    if (el) el.style.display = visible ? 'flex' : 'none';
  };
  showBlock('more-data-btn',      isAdmin);
  showBlock('more-portfolio-btn', isAdmin);

  // ── Home: "Today's Best Trades" Run Scan button (Execute shortcut) ──
  const homeRunScan = document.querySelector('[onclick*="runExecute"]');
  if (homeRunScan) homeRunScan.style.display = isAdmin ? '' : 'none';

  // ── Home: "Setups" quick action card ────────────────────────────────
  const setupsCard = document.querySelector('.home-shortcut[onclick*="execute"]');
  if (setupsCard) setupsCard.style.display = isAdmin ? 'flex' : 'none';

  // ── Plan info chip (header) ──────────────────────────────────────────
  const planChip = document.getElementById('plan-info-chip');
  if (planChip) {
    if (isAdmin) {
      planChip.style.display = 'none';
    } else if (planType && planExpiry) {
      const expDate  = new Date(planExpiry);
      const daysLeft = Math.ceil((expDate - new Date()) / 864e5);
      const expired  = daysLeft <= 0;
      planChip.textContent = expired ? 'Plan expired' : `${planType.toUpperCase()} · ${daysLeft}d left`;
      planChip.style.cssText = `font-size:10px;font-weight:700;padding:2px 8px;border-radius:5px;background:${expired ? 'var(--red-d)' : 'var(--green-d)'};color:${expired ? 'var(--red)' : 'var(--green)'};border:1px solid ${expired ? 'rgba(248,113,113,.25)' : 'rgba(52,211,153,.25)'};display:inline-block;`;
    } else {
      planChip.style.display = 'none';
    }
  }

  // ── More sheet plan line ─────────────────────────────────────────────
  const planEl = document.getElementById('more-sheet-plan');
  if (planEl) {
    if (isAdmin) {
      planEl.textContent = 'Administrator · Full access';
    } else if (planType && planExpiry) {
      const expDate  = new Date(planExpiry);
      const daysLeft = Math.ceil((expDate - new Date()) / 864e5);
      const expired  = daysLeft <= 0;
      planEl.textContent = expired
        ? `${planType.toUpperCase()} plan — expired`
        : `${planType.toUpperCase()} plan · ${daysLeft} days remaining`;
      planEl.style.color = expired ? 'var(--red)' : 'var(--txt3)';
    } else {
      planEl.textContent = '';
    }
  }
}

// Check existing token on load
(async function _initAuth() {
  const t = getToken();
  if (!t) { showLoginBtn(); return; }  // no token — show Sign In button in header
  // Token exists — validate silently in background; hide overlay optimistically
  // so returning users land instantly on dashboard
  hideLogin();
  _showAdminBadge(localStorage.getItem('apex_username') || '');
  _applyRoleUI(getRole(), null, null);
  try {
    const res = await fetch(`${API}/api/auth/me`, { headers: { 'Authorization': 'Bearer ' + t } });
    if (!res.ok) { clearToken(); showLogin(); return; }
    const info = await res.json();
    _showAdminBadge(info.username);
    _applyRoleUI(info.role || getRole(), info.plan_type, info.plan_expiry);
    if (info.role) setRole(info.role);
  } catch(e) {
    // network error — keep user on dashboard, token still local
  }
})();

const STRAT_DESC = {
  // Trend Following
  golden_rsi:          "Looks for stocks in a strong long-term uptrend (above 200-day EMA) pulling back (below 20-day EMA) with RSI below 40. Classic buy-the-dip setup.",
  sma_cross:           "Triggers when the 20-day moving average freshly crosses above the 50-day average — a classic momentum signal indicating short-term strength.",
  golden_cross:        "Triggers when SMA50 freshly crosses above SMA200 — the iconic 'Golden Cross'. One of the strongest long-term buy signals in technical analysis.",
  triple_ma:           "All three moving averages (SMA20 > SMA50 > SMA200) aligned bullishly and price bouncing off the 20-day average — full trend confirmation.",
  ema_ribbon:          "Flags fresh EMA alignment — EMA8 > EMA21 > EMA55 all stacked bullishly with price above all three. Indicates momentum across all timeframes.",
  ma_price_action:     "Price pulls back to the 50-day SMA in an uptrend (above SMA200), then closes back above it with a bullish candle — clean trend continuation.",
  supertrend:          "Fires when price crosses above the ATR-based Supertrend line, flipping the trend from bearish to bullish. Clean, objective trend signal.",
  supertrend_ema:      "Supertrend bullish flip confirmed by price trading above EMA50 — adds trend filter to reduce false signals.",
  supertrend_volume:   "Supertrend flip with a volume surge (1.5× average) on the trigger candle — institutional participation confirmed.",
  supertrend_adx:      "Supertrend bullish with ADX > 20 confirming trend strength. Filters out weak, choppy markets.",
  supertrend_adx_vol:  "Triple confirmation: Supertrend bullish + ADX strong trend + volume surge. Highest-confidence Supertrend setup.",
  adx_trend:           "Identifies stocks in a confirmed strong uptrend: ADX above 25 (trend strength), +DI above -DI (bulls in control), price above SMA50.",
  adx_breakout:        "ADX rising above 20 with price breaking to a new 20-day high and volume confirmation — trend just starting to accelerate.",
  // MACD
  macd_cross:          "Fires when the MACD line crosses above the signal line and histogram turns positive — a shift from bearish to bullish momentum.",
  macd_histogram:      "MACD histogram turns positive for the first time (zero cross) while below zero. Early momentum shift — often precedes price move.",
  macd_trend:          "MACD line above zero AND above signal line, price above SMA50 — momentum confirmed with trend filter.",
  macd_zero_line:      "MACD line freshly crosses above zero from below — confirming bullish momentum transition on the primary trend.",
  macd_divergence:     "Price makes a lower low but MACD makes a higher low — bullish divergence indicating hidden buying strength.",
  // RSI
  rsi_support:         "RSI dips into 30–45 zone (pullback without panic), then turns up while price is above SMA200. High probability bounce in uptrend.",
  rsi_50_trend:        "RSI crosses above 50 (neutral to bullish shift) while price is above SMA50 — momentum turning positive.",
  rsi_swing_reject:    "RSI dips below 40, then crosses back above 40 while price holds above SMA200. Classic swing low rejection signal.",
  rsi_divergence:      "Price makes lower low but RSI makes higher low — classic bullish divergence indicating buyer absorption at lows.",
  rsi_macd:            "RSI above 50 (bullish momentum) AND MACD histogram positive — dual-indicator momentum confirmation.",
  rsi_fibonacci:       "Price near a key Fibonacci retracement level (38.2%, 50%, or 61.8%) with RSI bouncing from oversold. Confluence reversal zone.",
  // Ichimoku
  ichimoku_cloud:      "Price above both Senkou A and B (above the cloud), Tenkan above Kijun — full Ichimoku bullish structure confirmed.",
  ichimoku_volume:     "Ichimoku bullish structure (above cloud, Tenkan > Kijun) confirmed with volume surge — institutional buying in trend.",
  tenkan_kijun_cross:  "Tenkan (9-period) freshly crosses above Kijun (26-period) while price is above the cloud — Ichimoku 'TK Cross' buy signal.",
  kumo_twist:          "Senkou A crosses above Senkou B (cloud twists bullish) — the cloud itself becomes supportive, indicating future bullish bias.",
  // Fibonacci
  fib_retracement:     "Price pulls back to a key Fibonacci level (38.2%, 50%, or 61.8%) from a recent swing high, then shows a bullish bounce signal.",
  fib_trend:           "Fibonacci retracement bounce combined with ADX trend strength confirmation — high-quality trend continuation setup.",
  // Mean Reversion
  bollinger_bounce:    "Identifies stocks touching the lower Bollinger Band (oversold) while above the 200-day SMA. Mean-reversion long in an uptrend.",
  double_bb:           "Price breaks above the middle Bollinger Band (20-day SMA) after being below it — shift from lower to upper band territory.",
  bb_rsi:              "Price near lower Bollinger Band with RSI below 35 — both indicators confirm oversold condition in an uptrend.",
  stochastic:          "Fires when Stochastic %K crosses above %D from the oversold zone (below 20) while the stock is above its 200-day SMA. Oversold bounce signal.",
  stoch_trend:         "Stochastic pullback to 40–50 (not fully oversold) with %K above %D — trend continuation pullback buy in strong uptrend.",
  stoch_divergence:    "Price makes lower low but Stochastic makes higher low — bullish divergence from oversold zone indicating buyer support.",
  cci_bounce:          "Triggers when CCI (Commodity Channel Index) crosses upward from below -100 while price is above SMA50. CCI oversold recovery signal.",
  williams_r_bounce:   "Williams %R crosses above -80 from extreme oversold territory — a fast oscillator signalling a sharp reversal in motion.",
  pivot_bounce:        "Price dips to a key pivot support level (S1 or S2) and bounces with a bullish candle — classic pivot point reversal.",
  // Breakout
  breakout:            "Detects stocks closing above their 20-day high with at least 1.5× average volume — confirming institutional breakout participation.",
  high_52w:            "Fires when price breaks above its 52-week high with volume confirmation (1.5× average). The strongest breakout signal — momentum tends to continue.",
  squeeze:             "TTM Squeeze: Bollinger Bands were compressed inside Keltner Channels (low volatility). When they break out bullishly, it's a high-probability signal.",
  donchian_breakout:   "Price closes above the 20-period Donchian Channel upper band — a Turtle Trader-style trend breakout with volume confirmation.",
  parabolic_sar:       "Parabolic SAR dots flip from above price to below — the SAR trend reversal signal confirming a new uptrend beginning.",
  // Volume & OBV
  volume_surge:        "Flags 2× average volume on a bullish candle (close > open) above the 50-day SMA — a sign of institutional accumulation.",
  obv_trend:           "On-Balance Volume rising (OBV above its 20-day SMA) with price also above SMA50 — volume confirming the bullish price trend.",
  acc_dist:            "Accumulation/Distribution Line rising above its 20-day SMA — money flow confirms institutional accumulation in the stock.",
  vwap_bounce:         "Price dips below VWAP then closes back above it with a bullish candle — classic intraday mean-reversion signal at VWAP support.",
  vwap_price_action:   "Price crosses above VWAP with a bullish candle and RSI above 50 — VWAP reclaim with momentum confirmation.",
  ema_rsi_volume:      "EMA20 > EMA50 (trend up), RSI between 45–65 (healthy momentum), volume above average — three-factor trend continuation.",
  // Candle Patterns
  hammer:              "Identifies a hammer candle (long lower wick ≥ 2× body, tiny upper wick) appearing after a downtrend. Classic single-candle reversal pattern.",
  bullish_engulfing:   "Two-candle reversal: today's bullish candle completely engulfs yesterday's bearish candle body above SMA50. Strong reversal confirmation.",
};

// ── More sheet ─────────────────────────────────────────
function openMoreSheet() {
  document.getElementById('more-sheet').style.display = 'block';
}
function closeMoreSheet() {
  document.getElementById('more-sheet').style.display = 'none';
}

// ── Navigation ─────────────────────────────────────────
function go(id, el) {
  // Block access to admin-only tabs for regular users
  if (ADMIN_ONLY_TABS.includes(id) && getRole() !== 'admin') {
    _showAccessDenied();
    return;
  }
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(n => n.classList.remove('active'));
  document.getElementById('tab-' + id).classList.add('active');
  if (el) el.classList.add('active');
  document.querySelector('.scroll').scrollTo(0, 0);
  if (id === 'home')      { loadHome(); }
  if (id === 'live')      { loadWatchlist(); refreshLive(); }
  if (id === 'execute')   { loadExecuteRegime(); _updateExecuteBadge(0); _execPrevCount = 0; }
  if (id === 'screener')  { scrGo('sector', document.querySelector('.scr-tab-btn[data-scr="sector"]')); loadSectors(); }
  if (id === 'admin')     { loadAdminStats(); loadAdminUsers(); }
  if (id === 'ml')        { checkMLStatus(); }
}

function _showAccessDenied() {
  const toast = document.createElement('div');
  toast.textContent = '🔒 Admin access required';
  toast.style.cssText = 'position:fixed;bottom:90px;left:50%;transform:translateX(-50%);background:var(--s2);color:var(--txt);border:1px solid var(--border);padding:10px 20px;border-radius:10px;font-size:13px;font-weight:500;z-index:9999;opacity:1;transition:opacity .4s;';
  document.body.appendChild(toast);
  setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 400); }, 2000);
}

// ── Clock ──────────────────────────────────────────────
function tickClock() {
  const ist = new Date().toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour:'2-digit', minute:'2-digit', second:'2-digit' });
  document.getElementById('hdr-clock').textContent = ist;
}
setInterval(tickClock, 1000);
tickClock();

// ── Market hours ───────────────────────────────────────
function checkMarket() {
  const ist = new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
  const day = ist.getDay();
  const mins = ist.getHours() * 60 + ist.getMinutes();
  const open = day >= 1 && day <= 5 && mins >= 555 && mins <= 930;

  document.getElementById('mkt-dot').className = 'mkt-dot ' + (open ? 'open' : 'closed');
  document.getElementById('mkt-label').textContent = open ? 'NSE — Market Open' : 'NSE — Market Closed';
  document.getElementById('live-banner-sub').textContent = open ? 'Live streaming active' : 'Market closed · last prices shown';

  const chip = document.getElementById('mkt-chip');
  const dot  = document.getElementById('mkt-chip-dot');
  chip.className = 'chip ' + (open ? 'chip-green' : 'chip-gray');
  dot.style.background = open ? 'var(--green)' : 'var(--txt3)';
  document.getElementById('mkt-chip-txt').textContent = open ? 'Open' : 'Closed';
}
setInterval(checkMarket, 30000);
checkMarket();

// ── Status refresh (Data tab) ──────────────────────────
async function refreshStatus() {
  try {
    const d = await j(`${API}/api/status`);
    // token chip
    const tc = document.getElementById('token-chip');
    tc.className = 'chip ' + (d.token_valid ? 'chip-green' : 'chip-red');
    tc.innerHTML = `<span class="chip-dot" style="background:${d.token_valid?'var(--green)':'var(--red)'}"></span><span>${d.token_valid?'Auth':'Expired'}</span>`;

    set('hdr-rows',  fmt(d.total_db_rows));
    set('t-new',     fmt(d.total_candles));
    set('t-syms',    fmt(d.unique_symbols));
    set('t-size',    d.db_size_mb + ' MB');
    set('t-last',    d.last_run && d.last_run !== 'Never' ? d.last_run.slice(11,16) : '—');
    set('t-expiry',  'Expires ' + (d.token_expiry || '—'));
    set('tbl-name',  d.table_name || '—');
    set('tok-exp',   d.token_expiry || '—');
    set('data-range', d.min_date && d.max_date ? d.min_date + ' → ' + d.max_date : '—');

    // Fyers chip
    const fc = document.getElementById('fyers-chip');
    const fl = document.getElementById('fyers-chip-label');
    if (fc && fl) {
      if (d.token_valid) {
        fc.className = 'chip chip-green';
        fc.querySelector('.chip-dot').style.background = 'var(--green)';
        fl.textContent = 'Connected';
      } else {
        fc.className = 'chip chip-red';
        fc.querySelector('.chip-dot').style.background = 'var(--red)';
        fl.textContent = 'Disconnected';
      }
    }

    const pct = d.total_symbols > 0 ? Math.min(100, Math.round((d.processed / d.total_symbols) * 100)) : 0;
    set('prog-pct', pct + '%');
    set('prog-counts', d.processed + ' / ' + (d.total_symbols || '—'));
    set('prog-label', d.is_running ? 'Sync in progress' : pct === 100 ? 'Sync complete ✓' : 'Ready to sync');
    const bar = document.getElementById('prog-bar');
    bar.style.width = pct + '%';
    bar.classList.toggle('shimmer', d.is_running);

    document.getElementById('run-banner').style.display = d.is_running ? 'flex' : 'none';
    if (d.is_running) set('cur-sym', d.current_symbol || '...');

    const sb = document.getElementById('sync-btn');
    sb.disabled = d.is_running;
    sb.innerHTML = d.is_running
      ? '<div class="spin" style="color:#fff"></div> Syncing...'
      : '<svg width="15" height="15" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg> Sync Data';

    if (getToken()) loadSnapshot();
  } catch(e) {}
}

async function loadSnapshot() {
  try {
    const rows = await jAuth(`${API}/api/latest_snapshot`);
    const tb = document.getElementById('snap-body');
    if (!rows.length) return;
    tb.innerHTML = rows.map(r => `
      <tr>
        <td style="font-weight:700;">${r.symbol}</td>
        <td class="mono" style="color:var(--data);font-weight:600;">₹${Math.round(r.close).toLocaleString('en-IN')}</td>
        <td style="color:var(--txt3);font-size:12px;">${(r.volume/1e6).toFixed(1)}M</td>
        <td style="color:var(--txt3);font-size:11px;">${r.date}</td>
      </tr>`).join('');
  } catch(e) {}
}

async function startSync() {
  document.getElementById('sync-btn').disabled = true;
  try { await jAuth(`${API}/api/start_backfill`, { method:'POST' }); await refreshStatus(); }
  catch(e) {}
}

async function triggerDailyRun() {
  const b = document.getElementById('daily-btn');
  b.disabled = true; b.textContent = 'Running...';
  try { await fetch(`${API}/api/daily_run`, { method:'POST' }); }
  catch(e) {}
  setTimeout(() => { b.disabled = false; b.textContent = '✦ Daily Run'; }, 5000);
}

// ── Strategy scanner ───────────────────────────────────
function onStratChange() {
  const v = document.getElementById('strat-sel').value;
  document.getElementById('strat-desc').textContent = STRAT_DESC[v] || '';
}

async function runScan() {
  const strat = document.getElementById('strat-sel').value;
  const btn = document.getElementById('scan-btn');
  const body = document.getElementById('sig-body');
  const cnt = document.getElementById('sig-count');

  btn.disabled = true;
  btn.innerHTML = '<div class="spin" style="color:var(--sig)"></div> Scanning...';
  body.innerHTML = '<div class="empty"><div class="empty-title" style="color:var(--txt2)">Scanning all 100 symbols…</div></div>';
  cnt.textContent = '...';

  try {
    const sigs = await jAuth(`${API}/api/signals?strategy=${strat}`);
    cnt.textContent = sigs.length + ' signals';

    if (!sigs.length) {
      body.innerHTML = `<div class="empty">
        <div class="empty-icon" style="background:var(--sig-d)">🔍</div>
        <div class="empty-title">No signals today</div>
        <div class="empty-sub">Market conditions don't match this strategy right now. Try again tomorrow.</div>
      </div>`;
    } else {
      body.innerHTML = sigs.map(s => `
        <div class="signal-item">
          <div style="flex:1;min-width:0;">
            <div class="sig-name">${s.symbol}</div>
            <div class="sig-meta">${s.trend}${s.rsi != null ? ' · RSI ' + s.rsi : ''}</div>
            <div class="sig-tags">
              <span class="badge badge-sig">${s.strategy}</span>
              ${s.volume_ratio != null ? `<span class="badge badge-gray">Vol ${s.volume_ratio}×</span>` : ''}
            </div>
          </div>
          <div style="flex-shrink:0;text-align:right;">
            <div class="sig-price">₹${fmtP(s.price)}</div>
            <div class="sig-levels">SL ₹${fmtP(s.stop_loss)}</div>
            <div class="sig-levels">T &nbsp;₹${fmtP(s.target)}</div>
            <button onclick='openChart("${s.symbol}",${JSON.stringify({stop_loss:s.stop_loss,target:s.target})})' class="btn btn-ghost btn-xs" style="margin-top:7px;">📈 Chart</button>
          </div>
        </div>`).join('');
    }
  } catch(e) {
    body.innerHTML = `<div class="empty"><div class="empty-title" style="color:var(--red)">Scan failed</div><div class="empty-sub">Make sure the server is running</div></div>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="15" height="15" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/></svg> Scan Universe';
  }
}

// ── Price Action ───────────────────────────────────────
async function loadPriceAction() {
  const sym  = document.getElementById('pa-sym').value.trim().toUpperCase();
  const days = document.getElementById('pa-days').value;
  const btn  = document.getElementById('pa-btn');
  if (!sym) { alert('Enter a symbol first'); return; }

  btn.disabled = true;
  btn.textContent = '…';

  try {
    const d = await jAuth(`${API}/api/price-action?symbol=${encodeURIComponent(sym)}&days=${days}`);
    if (d.error) { alert(d.error); return; }

    const s = d.summary;
    const fmtP = v => v != null ? '₹' + Number(v).toLocaleString('en-IN', {maximumFractionDigits:2}) : '—';
    const fmtN = (v, suffix='') => v != null ? v + suffix : '—';

    // Summary
    document.getElementById('pa-close').textContent  = fmtP(s.last_close);
    const chgEl = document.getElementById('pa-change');
    chgEl.textContent = s.change_pct != null ? (s.change_pct >= 0 ? '+' : '') + s.change_pct + '%' : '—';
    chgEl.style.color = (s.change_pct || 0) >= 0 ? 'var(--green)' : 'var(--red)';

    document.getElementById('pa-52h').textContent     = fmtP(s.high_52w);
    document.getElementById('pa-52h-pct').textContent = fmtN(s.pct_from_52w_high, '% from 52W High');
    document.getElementById('pa-52h-pct').style.color = (s.pct_from_52w_high || 0) >= -5 ? 'var(--green)' : 'var(--red)';

    document.getElementById('pa-52l').textContent     = fmtP(s.low_52w);
    document.getElementById('pa-52l-pct').textContent = fmtN(s.pct_from_52w_low, '% above 52W Low');

    const trendEl = document.getElementById('pa-trend');
    trendEl.textContent = s.trend;
    trendEl.style.color = s.trend.includes('Above') ? 'var(--green)' : 'var(--red)';
    document.getElementById('pa-sma').textContent = `SMA20 ₹${s.sma20}${s.sma50 ? ' · SMA50 ₹' + s.sma50 : ''}`;

    document.getElementById('pa-range').textContent   = fmtN(s.avg_range_pct, '%');
    const streakEl = document.getElementById('pa-streak');
    streakEl.textContent = `${s.streak} ${s.streak_dir} candles`;
    streakEl.style.color = s.streak_dir === 'green' ? 'var(--green)' : 'var(--red)';
    document.getElementById('pa-bull-bear').textContent = `${s.bull_candles}↑ ${s.bear_candles}↓ of ${s.total_candles} bars`;

    document.getElementById('pa-summary').style.display = 'block';

    // Candle table
    const patternColor = p => {
      if (!p) return 'var(--txt3)';
      const bull = ['Bullish','Hammer','Inverted Hammer','Bullish Marubozu','Morning Star'];
      const bear = ['Bearish','Shooting Star','Hanging Man','Bearish Marubozu','Evening Star'];
      if (bull.some(b => p.includes(b))) return 'var(--green)';
      if (bear.some(b => p.includes(b))) return 'var(--red)';
      return 'var(--live)';
    };

    document.getElementById('pa-count').textContent = `${d.candles.length} candles`;
    document.getElementById('pa-rows').innerHTML = d.candles.map(c => {
      const chg = c.change_pct;
      const chgColor = chg == null ? 'var(--txt3)' : chg >= 0 ? 'var(--green)' : 'var(--red)';
      const volColor = (c.vol_ratio || 0) >= 2 ? 'var(--data)' : (c.vol_ratio || 0) >= 1.5 ? 'var(--live)' : 'var(--txt2)';
      const rowBg    = (c.body_pct || 0) >= 0 ? 'background:rgba(52,211,153,0.04)' : 'background:rgba(248,113,113,0.04)';
      return `<tr style="${rowBg}">
        <td style="font-size:11px;font-weight:500;">${c.trade_date}</td>
        <td class="mono" style="font-size:11px;">₹${Number(c.open).toFixed(1)}</td>
        <td class="mono" style="font-size:11px;color:var(--green);">₹${Number(c.high).toFixed(1)}</td>
        <td class="mono" style="font-size:11px;color:var(--red);">₹${Number(c.low).toFixed(1)}</td>
        <td class="mono" style="font-size:11px;font-weight:700;">₹${Number(c.close).toFixed(1)}</td>
        <td style="font-weight:600;color:${chgColor};">${chg != null ? (chg >= 0 ? '+' : '') + chg + '%' : '—'}</td>
        <td style="font-size:11px;color:var(--txt2);">${c.range_pct != null ? c.range_pct + '%' : '—'}</td>
        <td style="font-size:11px;font-weight:600;color:${volColor};">${c.vol_ratio != null ? c.vol_ratio + 'x' : '—'}</td>
        <td style="font-size:11px;font-weight:500;color:${patternColor(c.pattern)};">${c.pattern || '—'}</td>
      </tr>`;
    }).join('');

    document.getElementById('pa-table').style.display = 'block';
  } catch(e) {
    alert('Error: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Analyse';
  }
}

// ── Backtest ───────────────────────────────────────────
let _btDays = 730;  // default 2Y = 730 days

function setBtPeriod(days, label, el) {
  _btDays = days;
  document.querySelectorAll('.bt-yr-pill').forEach(p => p.classList.remove('active'));
  if (el) el.classList.add('active');

  const rangeEl = document.getElementById('bt-date-range');
  const subEl   = document.getElementById('bt-banner-sub');
  const fmtD    = d => d.toLocaleDateString('en-IN', {day:'numeric', month:'short', year:'numeric'});

  if (days === 0) {
    rangeEl.textContent = 'All available history in the database';
    if (subEl) subEl.textContent = 'Simulate on full available history';
  } else {
    const end   = new Date();
    const start = new Date();
    start.setDate(start.getDate() - days);
    rangeEl.textContent = `${fmtD(start)} → ${fmtD(end)}`;
    const periodMap = {
      7:'1 week', 15:'15 days', 30:'1 month', 91:'3 months',
      183:'6 months', 365:'1 year', 730:'2 years', 1095:'3 years',
      1825:'5 years', 3650:'10 years'
    };
    if (subEl) subEl.textContent = `Simulate on ${periodMap[days] || label} of history`;
  }
}

// Initialise the date range label on load
(function() {
  const el = document.querySelector('.bt-yr-pill.active');
  if (el) setBtPeriod(parseInt(el.dataset.days) || 730, el.textContent, el);
})();

async function runBacktest() {
  const strat = document.getElementById('bt-strat').value;
  const sym = document.getElementById('bt-sym').value.trim().toUpperCase();
  const btn = document.getElementById('bt-btn');
  btn.disabled = true;
  btn.innerHTML = '<div class="spin" style="color:#fff"></div> Running…';
  document.getElementById('bt-metrics').style.display = 'none';
  document.getElementById('bt-log').style.display = 'none';

  // Build start date from selected period
  let startParam = '';
  if (_btDays > 0) {
    const startDate = new Date();
    startDate.setDate(startDate.getDate() - _btDays);
    startParam = '&start=' + startDate.toISOString().slice(0, 10);
  }

  try {
    const url = `${API}/api/backtest?strategy=${strat}${sym ? '&symbol='+sym : ''}${startParam}`;
    const d = await jAuth(url);

    if (d.error) {
      document.getElementById('bt-rows').innerHTML =
        `<tr><td colspan="5" style="text-align:center;color:var(--red);padding:20px;">${d.error}</td></tr>`;
      document.getElementById('bt-log').style.display = 'block';
      return;
    }

    const m = d.metrics || {};

    document.getElementById('bt-metrics').style.display = 'grid';
    const pos = (m.total_pnl || 0) >= 0;
    set('m-wr',  (m.win_rate ?? '—') + (m.win_rate != null ? '%' : ''));
    clr('m-wr',  (m.win_rate ?? 0) >= 50 ? 'green' : 'red');
    set('m-wl',  (m.win_count ?? '—') + ' W / ' + (m.loss_count ?? '—') + ' L');
    set('m-pnl', (pos ? '+' : '') + '₹' + fmt(Math.abs(m.total_pnl || 0)));
    clr('m-pnl', pos ? 'green' : 'red');
    set('m-pnlp', (pos?'+':'') + (m.total_pnl_pct ?? '—') + '% on capital');
    set('m-tot',  m.total_trades ?? '—');
    set('m-hold', 'avg ' + (m.avg_hold_days ?? '—') + ' days');
    set('m-dd',   '₹' + fmt(m.max_drawdown || 0));
    set('m-ddp',  (m.max_drawdown_pct ?? '—') + '% of capital');
    set('m-sh',   m.sharpe_ratio ?? '—');
    clr('m-sh',  (m.sharpe_ratio ?? 0) >= 1 ? 'green' : (m.sharpe_ratio ?? 0) >= 0 ? 'live' : 'red');
    set('m-pf',   m.profit_factor === 999 ? '∞' : (m.profit_factor ?? '—'));
    clr('m-pf',  (m.profit_factor ?? 0) >= 1.5 ? 'green' : (m.profit_factor ?? 0) >= 1 ? 'live' : 'red');

    if (d.trades && d.trades.length) {
      document.getElementById('bt-log').style.display = 'block';
      set('bt-cnt', d.trades.length + ' trades');
      document.getElementById('bt-rows').innerHTML = d.trades.slice(0,50).map(t => {
        const w = t.pnl > 0;
        const rb = t.exit_reason === 'Target Hit' ? 'badge-green' : t.exit_reason === 'Stop Loss' ? 'badge-red' : 'badge-gray';
        return `<tr class="${w?'tr-win':'tr-loss'}">
          <td style="font-weight:700;">${t.symbol}</td>
          <td style="font-size:12px;"><span class="mono" style="font-weight:600;">₹${fmtP(t.entry_price)}</span><br><span style="font-size:10px;color:var(--txt3);">${t.entry_date}</span></td>
          <td style="font-size:12px;"><span class="mono" style="font-weight:600;">₹${fmtP(t.exit_price)}</span><br><span style="font-size:10px;color:var(--txt3);">${t.exit_date}</span></td>
          <td><span class="mono" style="font-weight:700;color:${w?'var(--green)':'var(--red)'};">${w?'+':''}₹${fmt(Math.abs(t.pnl))}</span><br><span style="font-size:10px;color:var(--txt3);">${w?'+':''}${t.pnl_pct}%</span></td>
          <td><span class="badge ${rb}">${t.exit_reason}</span></td>
        </tr>`;
      }).join('');
    }
  } catch(e) { console.error(e); }
  finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="15" height="15" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/><path stroke-linecap="round" stroke-linejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg> Run Backtest';
  }
}

// ── Live feed ──────────────────────────────────────────
async function startFeed() {
  const mode = document.getElementById('feed-mode').value;
  document.getElementById('feed-start').disabled = true;
  try {
    await fetch(`${API}/api/live/start?mode=${mode}`, { method:'POST' });
    showFeedRunning(mode);
  } catch(e) { document.getElementById('feed-start').disabled = false; }
}

async function stopFeed() {
  try {
    await fetch(`${API}/api/live/stop`, { method:'POST' });
    showFeedStopped();
  } catch(e) {}
}

function showFeedRunning(mode) {
  document.getElementById('feed-start').style.display = 'none';
  document.getElementById('feed-stop').style.display = 'flex';
  const chip = document.getElementById('feed-chip');
  chip.className = 'chip chip-green';
  chip.innerHTML = `<span class="chip-dot" style="background:var(--green)"></span><span>${mode === 'websocket' ? 'WS Live' : 'Polling'}</span>`;
}

function showFeedStopped() {
  document.getElementById('feed-stop').style.display = 'none';
  document.getElementById('feed-start').style.display = 'flex';
  document.getElementById('feed-start').disabled = false;
  document.getElementById('feed-chip').className = 'chip chip-gray';
  document.getElementById('feed-chip').innerHTML = '<span class="chip-dot" style="background:var(--txt3)"></span><span>Stopped</span>';
}

async function refreshLive() {
  try {
    const status = await jAuth(`${API}/api/live/status`);
    if (status.feed && status.feed.running) {
      showFeedRunning(status.feed.mode);
    } else {
      showFeedStopped();
    }
    const prices = await jAuth(`${API}/api/ltp`);
    renderPrices(prices);
  } catch(e) {}
}

function renderPrices(prices) {
  if (!prices || !prices.length) return;
  set('live-ts', new Date().toLocaleTimeString('en-IN', { timeZone:'Asia/Kolkata' }));
  document.getElementById('live-body').innerHTML = prices.map(p => {
    const sym = p.symbol;
    const prev = prevPx[sym];
    const up = (p.change_pct || 0) >= 0;
    let cls = '';
    if (prev) cls = p.ltp > prev.ltp ? 'fup' : p.ltp < prev.ltp ? 'fdown' : '';
    prevPx[sym] = p;
    const cp = (p.change_pct || 0).toFixed(2);
    return `<tr class="${cls}" id="pr-${sym}">
      <td style="font-weight:700;">${sym}</td>
      <td class="mono" style="font-weight:600;">₹${fmtP(p.ltp)}</td>
      <td style="font-weight:600;color:${up?'var(--green)':'var(--red)'};">${up?'▲':'▼'} ${Math.abs(cp)}%</td>
      <td class="mono" style="font-size:12px;color:var(--txt3);">₹${fmtP(p.high)}</td>
      <td class="mono" style="font-size:12px;color:var(--txt3);">₹${fmtP(p.low)}</td>
      <td style="font-size:11px;color:var(--txt3);">${(p.volume/1e6).toFixed(1)}M</td>
    </tr>`;
  }).join('');
}

async function loadWatchlist() {
  try {
    const d = await jAuth(`${API}/api/watchlist`);
    renderChips(d.symbols || []);
  } catch(e) {}
}

function renderChips(syms) {
  document.getElementById('wl-chips').innerHTML = syms.map(s => `
    <div class="chip-sym">
      ${s}
      <button onclick="removeSym('${s}')">×</button>
    </div>`).join('');
}

async function addSym(preselected) {
  const inp = document.getElementById('wl-input');
  const sym = (preselected || inp.value).trim().toUpperCase();
  if (!sym) return;
  try { const d = await (await fetch(`${API}/api/watchlist/${sym}`, {method:'POST'})).json(); renderChips(d.symbols||[]); inp.value = ''; }
  catch(e) {}
}

async function removeSym(sym) {
  try { const d = await (await fetch(`${API}/api/watchlist/${sym}`, {method:'DELETE'})).json(); renderChips(d.symbols||[]); }
  catch(e) {}
}

// ── Helpers ────────────────────────────────────────────
// ── In-app chart sheet ───────────────────────────────────────────────────

function openChart(sym, tradeData = {}) {
  const sheet = document.getElementById('chart-sheet');
  sheet.classList.add('open');

  // Header info
  document.getElementById('chart-sym-name').textContent = sym;
  const sector = (_SECTOR_MAP && _SECTOR_MAP[sym]) || 'Equity';
  document.getElementById('chart-sym-meta').textContent = sector + ' · NSE';

  // TradingView full-chart link
  const tvSym = sym.replace(/[&]/g, '_').replace(/-/g, '');
  document.getElementById('chart-tv-link').href =
    `https://www.tradingview.com/chart/?symbol=NSE:${tvSym}`;

  // Trade info bar
  const bar = document.getElementById('chart-trade-bar');
  const fmt  = v => v != null ? '₹' + Number(v).toLocaleString('en-IN', {maximumFractionDigits:0}) : '—';
  if (tradeData.stop_loss || tradeData.target || tradeData.price_target || tradeData.buy_probability != null) {
    bar.style.display = 'block';
    document.getElementById('chart-entry').textContent = fmt(tradeData.entry || tradeData.current_price);
    document.getElementById('chart-sl').textContent    = fmt(tradeData.stop_loss);
    document.getElementById('chart-tgt').textContent   = fmt(tradeData.target || tradeData.price_target);
    const prob = tradeData.buy_probability != null
      ? Math.round(tradeData.buy_probability * 100) + '%' : '—';
    const aiEl = document.getElementById('chart-ai');
    aiEl.textContent  = prob;
    aiEl.style.color  = _probColor(tradeData.buy_probability || 0);
  } else {
    bar.style.display = 'none';
  }

  // Push a history state so Android back button closes the sheet
  history.pushState({ chartOpen: true }, '', '');

  _renderTVChart(sym);
}

function closeChart() {
  const sheet = document.getElementById('chart-sheet');
  sheet.classList.remove('open');
  document.getElementById('tv-widget').innerHTML = '';  // stop any live feeds
}

function _renderTVChart(sym) {
  const tvSym     = 'NSE:' + sym.replace(/[&]/g, '_').replace(/-/g, '');
  const container = document.getElementById('tv-widget');
  container.innerHTML = '';

  function _doRender() {
    new TradingView.widget({
      autosize:            true,
      symbol:              tvSym,
      interval:            'D',
      timezone:            'Asia/Kolkata',
      theme:               'dark',
      style:               '1',
      locale:              'en',
      toolbar_bg:          '#070C16',
      backgroundColor:     '#070C16',
      gridColor:           'rgba(255,255,255,0.04)',
      enable_publishing:   false,
      hide_side_toolbar:   false,
      allow_symbol_change: true,
      container_id:        'tv-widget',
      save_image:          false,
      studies:             ['RSI@tv-basicstudies', 'MACD@tv-basicstudies'],
    });
  }

  if (window.TradingView) {
    _doRender();
  } else {
    // Library still loading — poll until ready (max 5 s)
    let tries = 0;
    const wait = setInterval(() => {
      if (window.TradingView) { clearInterval(wait); _doRender(); }
      if (++tries > 50)        { clearInterval(wait); }
    }, 100);
  }
}

// Close chart sheet on Android back-button press
window.addEventListener('popstate', e => {
  if (document.getElementById('chart-sheet').classList.contains('open')) {
    closeChart();
  }
});
const j = url => fetch(url).then(r => r.json());
function set(id, v) { const el = document.getElementById(id); if(el) el.textContent = v ?? '—'; }
function clr(id, c) {
  const el = document.getElementById(id); if(!el) return;
  const map = { green:'var(--green)', red:'var(--red)', live:'var(--live)', bt:'var(--bt)', sig:'var(--sig)', data:'var(--data)' };
  el.style.color = map[c] || 'var(--txt)';
}
function fmt(n) { return n == null || isNaN(n) ? '—' : Number(n).toLocaleString('en-IN'); }
function fmtP(n) { return n == null || isNaN(n) ? '—' : Number(n).toLocaleString('en-IN',{minimumFractionDigits:0,maximumFractionDigits:0}); }

// ── ML Predictor ───────────────────────────────────────
let mlAllPredictions = [];
let mlTrainingPoller = null;

async function checkMLStatus() {
  try {
    const d = await jAuth(`${API}/api/ml/train/status`);
    if (d.is_trained) {
      const chip = document.getElementById('ml-status-chip');
      chip.className = 'chip chip-green';
      chip.innerHTML = '<span class="chip-dot" style="background:var(--green)"></span><span>Trained</span>';
      document.getElementById('ml-predict-btn').style.display = 'flex';
      document.getElementById('ml-results-card').style.display = 'block';
      document.getElementById('ml-analytics-card').style.display = 'block';
      document.getElementById('ml-regime-card').style.display = 'block';
      document.getElementById('ml-data-card').style.display = 'block';
      if (d.meta && d.meta.auc_roc) renderMLMeta(d.meta);
      loadMLAnalytics();
      loadRegime();
      loadMLDataValidation();
    }
  } catch(e) {}
}

async function loadMLDataValidation() {
  try {
    const d = await jAuth(`${API}/api/ml/data-validation`);
    const db  = d.db_coverage  || {};
    const mm  = d.model_meta   || {};
    const sym = d.per_symbol   || [];

    // Summary tiles
    const trainSamples = mm.train_samples || db.total_rows || 0;
    document.getElementById('val-train-samples').textContent = trainSamples > 0 ? trainSamples.toLocaleString('en-IN') : '—';
    document.getElementById('val-symbols').textContent = mm.symbols_used || db.total_symbols || '—';
    document.getElementById('val-years').textContent   = db.years_span   != null ? db.years_span : '—';

    // Date range
    document.getElementById('val-oldest').textContent = db.oldest || '—';
    document.getElementById('val-latest').textContent = db.latest || '—';

    // Timeline bar — wider = more history
    const maxYears = 30;
    const pct = db.years_span ? Math.min((db.years_span / maxYears) * 100, 100) : 0;
    setTimeout(() => {
      const bar = document.getElementById('val-timeline-bar');
      if (bar) bar.style.width = pct + '%';
    }, 200);

    // Trained at
    const ta = mm.trained_at || '';
    document.getElementById('val-trained-at').innerHTML =
      ta ? `Last trained: <b>${new Date(ta).toLocaleString('en-IN')}</b> · AUC ${mm.auc_roc ? (mm.auc_roc*100).toFixed(1)+'%' : '—'} · ${mm.symbols_used||'—'} symbols · ${(mm.train_samples||0).toLocaleString('en-IN')} train rows` : 'Model not yet trained';

    // Per-symbol coverage
    const body = document.getElementById('val-symbols-body');
    if (sym.length) {
      const maxRows = Math.max(...sym.map(s => s.rows));
      body.innerHTML = sym.map(s => {
        const barW = maxRows > 0 ? Math.round((s.rows / maxRows) * 100) : 0;
        const yrs  = s.years != null ? s.years + 'y' : '—';
        const color = (s.years || 0) >= 20 ? 'var(--green)' : (s.years || 0) >= 5 ? 'var(--data)' : 'var(--live)';
        return `
          <div style="display:flex;align-items:center;gap:8px;">
            <div style="width:72px;font-size:11px;font-weight:700;flex-shrink:0;">${s.symbol}</div>
            <div style="flex:1;height:6px;background:var(--s3);border-radius:99px;overflow:hidden;">
              <div style="height:6px;background:${color};border-radius:99px;width:${barW}%;"></div>
            </div>
            <div style="width:32px;text-align:right;font-size:10px;color:var(--txt3);flex-shrink:0;">${yrs}</div>
            <div style="width:50px;text-align:right;font-size:10px;color:var(--txt3);flex-shrink:0;">${s.rows.toLocaleString('en-IN')}</div>
          </div>`;
      }).join('');
    } else {
      body.innerHTML = '<div style="font-size:12px;color:var(--txt3);">No data found — run a backfill first</div>';
    }
  } catch(e) {
    console.error('Data validation error', e);
  }
}

function renderMLMeta(meta) {
  const row = document.getElementById('ml-metrics-row');
  row.style.display = 'grid';
  document.getElementById('ml-auc').textContent = meta.auc_roc ? (meta.auc_roc * 100).toFixed(1) + '%' : '—';
  document.getElementById('ml-acc').textContent = meta.accuracy ? (meta.accuracy * 100).toFixed(1) + '%' : '—';
  document.getElementById('ml-f1').textContent  = meta.f1_buy   ? (meta.f1_buy   * 100).toFixed(1) + '%' : '—';

  const trained = meta.trained_at ? new Date(meta.trained_at).toLocaleDateString('en-IN') : '';
  const info = `Trained ${trained} · ${meta.symbols_used || 0} symbols · ${(meta.training_samples||0).toLocaleString('en-IN')} rows`;
  document.getElementById('ml-train-info').textContent = info;

  if (meta.top_features && meta.top_features.length) {
    const card = document.getElementById('ml-features-card');
    card.style.display = 'block';
    const maxImp = meta.top_features[0].importance;
    document.getElementById('ml-features-body').innerHTML = meta.top_features.map(f => {
      const pct = maxImp > 0 ? (f.importance / maxImp * 100).toFixed(0) : 0;
      return `<div>
        <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
          <span style="font-size:12px;font-weight:500;font-family:'JetBrains Mono',monospace;">${f.name}</span>
          <span style="font-size:11px;color:var(--txt3);">${(f.importance*100).toFixed(2)}%</span>
        </div>
        <div style="height:4px;background:var(--s3);border-radius:99px;overflow:hidden;">
          <div style="height:100%;width:${pct}%;background:linear-gradient(90deg,var(--data),var(--sig));border-radius:99px;"></div>
        </div>
      </div>`;
    }).join('');
  }
}

async function trainModel() {
  const btn = document.getElementById('ml-train-btn');
  btn.disabled = true;
  btn.innerHTML = '<div class="spin" style="color:#fff;"></div> Training…';

  try {
    const r = await jAuth(`${API}/api/ml/train`, {method:'POST'});
    if (r.status === 'busy') {
      btn.disabled = false;
      btn.innerHTML = '⚠ Already training';
      return;
    }

    // Poll until done
    mlTrainingPoller = setInterval(async () => {
      try {
        const st = await jAuth(`${API}/api/ml/train/status`);
        if (!st.is_training) {
          clearInterval(mlTrainingPoller);
          btn.disabled = false;
          btn.innerHTML = '↺ Re-Train';
          if (st.last_result && st.last_result.error) {
            btn.innerHTML = '❌ Failed — check deps';
          } else {
            checkMLStatus();
            loadMLAnalytics();
            loadRegime();
          }
        }
      } catch(e) {}
    }, 4000);
  } catch(e) {
    btn.disabled = false;
    btn.innerHTML = 'Train Model';
  }
}

async function loadRegime() {
  try {
    const r = await jAuth(`${API}/api/ml/regime`);
    document.getElementById('ml-regime-card').style.display = 'block';

    const colours = { Bull: 'var(--green)', Neutral: 'var(--live)', Bear: 'var(--red)' };
    const insights = {
      Bull: `${r.breadth_pct}% of Nifty100 stocks are above SMA50. Broad participation — BUY threshold lowered to ${(r.breadth_pct >= 60 ? 55 : 60)}% to capture more opportunities.`,
      Neutral: `Mixed market — ${r.breadth_pct}% above SMA50. Standard BUY threshold of 60% applied. Prefer strong setups with volume confirmation.`,
      Bear: `Only ${r.breadth_pct}% of stocks above SMA50. Broad weakness — BUY threshold raised to 65%. Only take the highest-confidence setups.`,
    };
    const thresholds = { Bull: 55, Neutral: 60, Bear: 65 };

    const regime = r.regime || 'Neutral';
    document.getElementById('regime-dot').style.background = colours[regime] || 'var(--txt3)';
    document.getElementById('regime-label').textContent = regime + ' Market';
    document.getElementById('regime-sub').textContent =
      `${r.above_sma50} above SMA50 · ${r.below_sma50} below · as of ${r.as_of_date}`;
    document.getElementById('regime-breadth').textContent = r.breadth_pct + '%';
    document.getElementById('regime-vol').textContent = r.volatility_label + ' Volatility';
    document.getElementById('regime-atr').textContent = `avg ATR ${r.avg_atr_pct}%`;
    document.getElementById('regime-thresh').textContent = (thresholds[regime] || 60) + '%';
    document.getElementById('regime-insight').textContent = insights[regime] || '';

    // Breadth bar marker position
    const pct = Math.min(Math.max(r.breadth_pct, 0), 100);
    document.getElementById('regime-bar-marker').style.left = pct + '%';
  } catch(e) {}
}

async function loadMLAnalytics() {
  try {
    const d = await jAuth(`${API}/api/ml/reliability`);
    document.getElementById('ml-analytics-card').style.display = 'block';

    if (d.regressor_mae_pct != null)
      document.getElementById('ml-reg-mae').textContent = Math.abs(d.regressor_mae_pct).toFixed(2) + '%';
    if (d.regressor_r2 != null)
      document.getElementById('ml-reg-r2').textContent = d.regressor_r2.toFixed(3);
    if (d.wf_test_ratio_pct != null)
      document.getElementById('wf-detail').textContent =
        `Walk-Forward: oldest ${100-d.wf_test_ratio_pct}% = train · most-recent ${d.wf_test_ratio_pct}% = test (per symbol)`;

    if (d.reliability_buckets && d.reliability_buckets.length) {
      document.getElementById('ml-calibration-bars').innerHTML =
        d.reliability_buckets.map(b => {
          const predW = b.predicted_pct;
          const actW  = b.actual_pct;
          const wellCalib = Math.abs(predW - actW) <= 8;
          return `<div>
            <div style="display:flex;justify-content:space-between;margin-bottom:3px;">
              <span style="font-size:11px;color:var(--txt2);">Predicted ~${predW}%</span>
              <span style="font-size:11px;font-weight:600;color:${wellCalib?'var(--green)':'var(--live)'};">
                Actual: ${actW}% <span style="font-size:10px;color:var(--txt3);">(n=${b.count})</span>
              </span>
            </div>
            <div style="height:16px;background:var(--s2);border-radius:6px;overflow:hidden;position:relative;display:flex;">
              <div style="height:100%;width:${predW}%;background:var(--data);border-radius:6px;opacity:.5;"></div>
              <div style="position:absolute;height:100%;width:${actW}%;background:var(--green);border-radius:6px;opacity:.7;"></div>
            </div>
          </div>`;
        }).join('');
    }
  } catch(e) {}
}

async function runPredictions() {
  const btn = document.getElementById('ml-predict-btn');
  btn.disabled = true;
  btn.innerHTML = '<div class="spin"></div> Scoring…';
  document.getElementById('ml-pred-body').innerHTML =
    '<div class="empty"><div class="spin" style="color:var(--data);margin:20px auto;"></div><div class="empty-sub" style="margin-top:8px;">Scoring all 99 symbols…</div></div>';

  try {
    const d = await jAuth(`${API}/api/ml/predict`);
    mlAllPredictions = d.results || [];
    mlFilter('all');
    const buyCount  = mlAllPredictions.filter(p => p.signal === 'BUY').length;
    const liveCount = d.live_count || 0;
    set('ml-buy-count', buyCount + ' BUY');
    set('ml-pred-count', mlAllPredictions.length + ' scored');
    const liveBadge = document.getElementById('ml-live-badge');
    if (liveCount > 0) {
      liveBadge.style.display = 'inline-flex';
      liveBadge.textContent = '● ' + liveCount + ' LIVE';
    } else {
      liveBadge.style.display = 'none';
    }
    if (d.regime) loadRegime();
  } catch(e) {
    document.getElementById('ml-pred-body').innerHTML =
      '<div class="empty"><div class="empty-title">Error</div><div class="empty-sub">Could not fetch predictions</div></div>';
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="15" height="15" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/></svg> Score All 99';
  }
}

function mlFilter(type) {
  // Update filter button styles
  ['all','buy','neutral','avoid'].forEach(t => {
    const el = document.getElementById('ml-filter-' + t);
    if (!el) return;
    el.style.background = t === type.toLowerCase() ? 'var(--data)' : 'var(--s3)';
    el.style.color       = t === type.toLowerCase() ? '#fff'    : 'var(--txt2)';
    el.style.border      = t === type.toLowerCase() ? 'none'    : '1px solid var(--border2)';
  });

  const filtered = type === 'all'
    ? mlAllPredictions
    : mlAllPredictions.filter(p => p.signal === type.toUpperCase());

  if (!filtered.length) {
    document.getElementById('ml-pred-body').innerHTML =
      '<div class="empty"><div class="empty-icon" style="background:rgba(99,102,241,.12);">🤖</div>' +
      '<div class="empty-title">No results</div><div class="empty-sub">No symbols match this filter</div></div>';
    return;
  }

  const sigColour = { BUY:'var(--green)', NEUTRAL:'var(--live)', AVOID:'var(--red)' };
  const sigBg     = { BUY:'var(--green-d)', NEUTRAL:'var(--live-d)', AVOID:'var(--red-d)' };

  document.getElementById('ml-pred-body').innerHTML = filtered.map(p => {
    const pct  = Math.round((p.buy_probability || 0) * 100);
    const bar  = `<div style="height:3px;background:var(--s3);border-radius:99px;margin-top:5px;overflow:hidden;">
      <div style="height:100%;width:${pct}%;background:${sigColour[p.signal]||'var(--data)'};border-radius:99px;transition:width .4s;"></div></div>`;
    const target = p.price_target ? `₹${fmtP(p.price_target)}` : '—';
    const retPct = p.expected_return_pct != null
      ? `<span style="color:${p.expected_return_pct>=0?'var(--green)':'var(--red)'};">${p.expected_return_pct>=0?'+':''}${p.expected_return_pct}%</span>`
      : '';
    const isLive = p.price_source === 'live';
    const priceLabel = isLive
      ? `<span style="font-size:9px;font-weight:700;color:var(--green);margin-left:3px;">●LIVE</span>`
      : `<span style="font-size:9px;color:var(--txt3);margin-left:3px;">(last close)</span>`;
    return `<div class="signal-item" style="cursor:pointer;" onclick='openChart("${p.symbol}",{stop_loss:${p.stop_loss||null},target:${p.target||null}})'>
      <div style="flex:1;min-width:0;">
        <div class="sig-name">${p.symbol}</div>
        <div class="sig-meta">₹${fmtP(p.price)}${priceLabel} · ${p.confidence || ''} · Target ${target} ${retPct}</div>
        ${bar}
      </div>
      <div style="text-align:right;flex-shrink:0;margin-left:8px;">
        <div style="font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:700;color:${sigColour[p.signal]||'var(--data)'};">${pct}%</div>
        <span style="display:inline-flex;align-items:center;font-size:10px;font-weight:700;padding:2px 8px;border-radius:5px;
          background:${sigBg[p.signal]||'rgba(99,102,241,.12)'};color:${sigColour[p.signal]||'var(--data)'};margin-top:3px;">${p.signal}</span>
      </div>
    </div>`;
  }).join('');
}

// ── Yahoo Finance backfill ─────────────────────────────
let yfPoller = null;

async function startYFinance() {
  const btn = document.getElementById('yf-btn');
  btn.disabled = true;
  btn.innerHTML = '<div class="spin" style="color:var(--live);"></div> Starting…';

  const selectedIndex = document.getElementById('yf-index-select')?.value || '';
  const url = selectedIndex
    ? `${API}/api/start_yfinance_backfill?index=${encodeURIComponent(selectedIndex)}`
    : `${API}/api/start_yfinance_backfill`;

  try {
    const r = await jAuth(url, {method:'POST'});
    if (r.status === 'busy') {
      btn.disabled = false;
      return;
    }
    document.getElementById('yf-progress-wrap').style.display = 'block';
    document.getElementById('yf-chip').className = 'chip chip-amber';
    document.getElementById('yf-chip').innerHTML =
      '<span class="chip-dot" style="background:var(--live)"></span><span>Running</span>';

    yfPoller = setInterval(pollYFinance, 3000);
  } catch(e) {
    btn.disabled = false;
    btn.innerHTML = 'Fetch Full History (Yahoo Finance)';
  }
}

async function pollYFinance() {
  try {
    const d = await jAuth(`${API}/api/yfinance/status`);
    set('yf-cur-sym', d.current_symbol || '…');
    set('yf-pct', (d.pct || 0) + '%');
    set('yf-proc', d.processed || 0);
    set('yf-tot', d.total || 0);
    set('yf-new', (d.total_new_candles || 0).toLocaleString('en-IN'));
    document.getElementById('yf-bar').style.width = (d.pct || 0) + '%';

    if (!d.is_running) {
      clearInterval(yfPoller);
      const btn = document.getElementById('yf-btn');
      btn.disabled = false;
      btn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg> Re-fetch (Yahoo Finance)';
      document.getElementById('yf-chip').className = 'chip chip-green';
      document.getElementById('yf-chip').innerHTML =
        '<span class="chip-dot" style="background:var(--green)"></span><span>Done</span>';
      loadDbSources();
      refreshStatus();   // update DB row count
    }
  } catch(e) {}
}

async function loadDbSources() {
  try {
    const rows = await jAuth(`${API}/api/db/sources`);
    if (!rows || rows.error || !rows.length) return;

    const sourceColours = { FYERS: 'var(--data)', YFINANCE: 'var(--live)' };
    document.getElementById('yf-sources').style.display = 'block';
    document.getElementById('yf-sources-body').innerHTML = rows.map(r => `
      <div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid var(--border);">
        <div style="display:flex;align-items:center;gap:8px;">
          <span style="width:8px;height:8px;border-radius:50%;background:${sourceColours[r.source]||'var(--bt)'};display:inline-block;flex-shrink:0;"></span>
          <span style="font-size:12px;font-weight:600;">${r.source}</span>
          <span style="font-size:10px;color:var(--txt3);">${r.from_date} → ${r.to_date}</span>
        </div>
        <div style="text-align:right;">
          <span style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:600;color:${sourceColours[r.source]||'var(--bt)'};">${r.rows.toLocaleString('en-IN')}</span>
          <span style="font-size:10px;color:var(--txt3);margin-left:4px;">rows · ${r.symbols} syms</span>
        </div>
      </div>`).join('');
  } catch(e) {}
}

// ── Scheduler status ───────────────────────────────────
async function refreshScheduler() {
  try {
    const d = await j(`${API}/api/scheduler/status`);

    // Status chip
    const chip = document.getElementById('sched-status-chip');
    if (d.running) {
      chip.className = 'chip chip-green';
      chip.innerHTML = '<span class="chip-dot" style="background:var(--green)"></span><span>Active</span>';
    } else {
      chip.className = 'chip chip-gray';
      chip.innerHTML = '<span class="chip-dot" style="background:var(--txt3)"></span><span>Off</span>';
    }

    // Data update job
    if (d.data_update) {
      const du = d.data_update;
      set('sched-data-schedule', du.schedule || '');
      set('sched-data-last', du.last_run && du.last_run !== 'Never' ? du.last_run : 'Never');
      if (du.last_result && du.last_result.total_candles != null)
        set('sched-data-candles', du.last_result.total_candles.toLocaleString('en-IN') + ' candles');
      document.getElementById('sched-data-running').style.display =
        du.currently_running ? 'flex' : 'none';
    }

    // ML retrain job
    if (d.ml_retrain) {
      const ml = d.ml_retrain;
      set('sched-ml-schedule', ml.schedule || '');
      set('sched-ml-last', ml.last_run && ml.last_run !== 'Never' ? ml.last_run : 'Never');
      if (ml.last_result && ml.last_result.auc_roc != null)
        set('sched-ml-auc', 'AUC ' + (ml.last_result.auc_roc * 100).toFixed(1) + '%');
      document.getElementById('sched-ml-running').style.display =
        ml.currently_running ? 'flex' : 'none';
    }

    // Next run times from jobs list
    if (d.jobs) {
      d.jobs.forEach(job => {
        if (job.id === 'daily_data_update')
          set('sched-data-next', job.next_run_ist);
        if (job.id === 'weekly_ml_retrain')
          set('sched-ml-next', job.next_run_ist);
      });
    }
  } catch(e) {}
}

// ══════════════════════════════════════════════════════
// EXECUTE TAB — Strategy + AI Combined
// ══════════════════════════════════════════════════════

async function loadExecuteRegime() {
  try {
    const d = await jAuth(`${API}/api/ml/regime`);
    const regime = d.regime || 'Unknown';
    const colors = { Bull:'var(--green)', Neutral:'var(--live)', Bear:'var(--red)' };
    const dot = document.getElementById('exec-regime-dot');
    dot.style.background  = colors[regime] || 'var(--txt3)';
    dot.style.boxShadow   = `0 0 10px ${colors[regime] || 'var(--txt3)'}`;
    set('exec-regime-label', regime + ' Market');
    set('exec-regime-sub', `${d.breadth_pct || 0}% stocks above SMA50 · ${d.volatility_label || ''} volatility · ${d.symbols_checked || 0} stocks analysed`);
    const thresh = { Bull:'55%', Neutral:'60%', Bear:'65%' };
    set('exec-threshold', thresh[regime] || '60%');
  } catch(e) {}
}

function _probColor(p) {
  if (p >= 0.70) return 'var(--green)';
  if (p >= 0.60) return 'var(--data)';
  if (p >= 0.50) return 'var(--live)';
  return 'var(--txt3)';
}

function _confBadgeStyle(conf) {
  const map = {
    'Very High': 'background:var(--green-d);color:var(--green);border:1px solid rgba(52,211,153,.25);',
    'High':      'background:var(--data-d);color:var(--data);border:1px solid rgba(99,102,241,.25);',
    'Moderate':  'background:var(--live-d);color:var(--live);border:1px solid rgba(251,191,36,.25);',
    'Low':       'background:var(--s3);color:var(--txt3);border:1px solid var(--border);',
    'Very Low':  'background:var(--s3);color:var(--txt3);border:1px solid var(--border);',
  };
  return map[conf] || 'background:var(--s3);color:var(--txt3);border:1px solid var(--border);';
}

function _hColor(p) {
  if (p >= 0.70) return {bg:'var(--green-d)',border:'rgba(52,211,153,.3)',color:'var(--green)'};
  if (p >= 0.60) return {bg:'var(--data-d)',border:'rgba(99,102,241,.3)',color:'var(--data)'};
  if (p >= 0.50) return {bg:'var(--live-d)',border:'rgba(251,191,36,.3)',color:'var(--live)'};
  return {bg:'var(--s3)',border:'var(--border)',color:'var(--txt3)'};
}

function _stratLabel(id) {
  const labels = {
    golden_rsi:'Golden RSI', sma_cross:'SMA Cross', macd_cross:'MACD Cross',
    bollinger_bounce:'BB Bounce', breakout:'Breakout', volume_surge:'Vol Surge',
    golden_cross:'Golden Cross', supertrend:'Supertrend', stochastic:'Stochastic',
    adx_trend:'ADX Trend', hammer:'Hammer', bullish_engulfing:'Engulfing',
    squeeze:'Squeeze', ema_ribbon:'EMA Ribbon', high_52w:'52W High', cci_bounce:'CCI Bounce',
  };
  return labels[id] || id;
}

let _execTrendPeriod = 'any';

function setTrendPeriod(period, el) {
  _execTrendPeriod = period;
  document.querySelectorAll('.trend-btn').forEach(b => b.classList.remove('active'));
  if (el) el.classList.add('active');

  const note = document.getElementById('trend-filter-note');
  const labels = { any: null, '1m': '1 Month (20 days)', '3m': '3 Months (63 days)', '6m': '6 Months (126 days)', '1y': '1 Year (252 days)' };
  if (period === 'any') {
    note.style.display = 'none';
  } else {
    note.style.display = 'block';
    note.textContent = `Only stocks with positive returns over the last ${labels[period]} will appear. Removes stocks in a downtrend at your chosen timeframe.`;
  }
}

function _buildSectorFilter(results) {
  const bar = document.getElementById('exec-sector-filter');
  if (!results || !results.length) { bar.style.display = 'none'; return; }

  // Count per sector, preserving result order
  const counts = {};
  const order  = [];
  results.forEach(r => {
    const s = _getSector(r.symbol);
    if (!counts[s]) { counts[s] = 0; order.push(s); }
    counts[s]++;
  });

  // Only show filter bar if there's more than one sector
  if (order.length <= 1) { bar.style.display = 'none'; return; }

  bar.style.display = 'block';
  bar.innerHTML = `<div class="exec-sector-bar">
    <button class="exec-sector-pill active" onclick="setSectorFilter('all',this)">
      All <span class="pill-count">${results.length}</span>
    </button>
    ${order.map(s => `
      <button class="exec-sector-pill" onclick="setSectorFilter('${s}',this)">
        ${s} <span class="pill-count">${counts[s]}</span>
      </button>`).join('')}
  </div>`;
}

function setSectorFilter(sector, el) {
  // Update pill active state
  document.querySelectorAll('.exec-sector-pill').forEach(p => p.classList.remove('active'));
  if (el) el.classList.add('active');

  // Show/hide cards
  document.querySelectorAll('#exec-body .comb-card').forEach(card => {
    const match = sector === 'all' || card.dataset.sector === sector;
    card.style.display = match ? '' : 'none';
  });

  // Update count label
  const visible = sector === 'all'
    ? document.querySelectorAll('#exec-body .comb-card').length
    : document.querySelectorAll(`#exec-body .comb-card[data-sector="${sector}"]`).length;
  const countEl = document.getElementById('exec-count');
  if (countEl) {
    countEl.textContent = sector === 'all'
      ? visible + ' trade ideas'
      : `${visible} in ${sector}`;
  }
}

async function runExecute() {
  const btn     = document.getElementById('exec-run-btn');
  const body    = document.getElementById('exec-body');
  const insight = document.getElementById('exec-ai-insight');
  btn.disabled = true;
  btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="animation:spin 1s linear infinite"><path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg> Scanning 100 stocks…';

  // Skeleton loading state
  body.innerHTML = [1,2,3].map(() => `
    <div style="padding:16px;border-bottom:1px solid var(--border);">
      <div style="display:flex;gap:10px;margin-bottom:12px;">
        <div class="sk sk-value" style="width:80px;height:22px;"></div>
        <div style="flex:1;"></div>
        <div class="sk" style="width:48px;height:22px;"></div>
      </div>
      <div class="sk sk-line sk-full"></div>
      <div class="sk sk-line sk-75" style="margin-top:6px;"></div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-top:10px;">
        <div class="sk" style="height:44px;border-radius:8px;"></div>
        <div class="sk" style="height:44px;border-radius:8px;"></div>
        <div class="sk" style="height:44px;border-radius:8px;"></div>
      </div>
    </div>`).join('');
  insight.style.display = 'none';
  document.getElementById('exec-sector-filter').style.display = 'none';

  const periodParam = _execTrendPeriod !== 'any' ? `?trend_period=${_execTrendPeriod}` : '';
  let _regime = 'Neutral';
  try {
    const d = await jAuth(`${API}/api/combined/signals${periodParam}`);

    // Stats
    set('exec-stat-hits',      d.total_strategy_signals || 0);
    set('exec-stat-symbols',   d.symbols_with_signals   || 0);
    set('exec-stat-confirmed', d.ml_confirmed           || 0);
    set('exec-count', (d.count || 0) + ' trade ideas');

    // Regime
    if (d.regime) {
      _regime = d.regime.regime || 'Neutral';
      const colors = { Bull:'var(--green)', Neutral:'var(--live)', Bear:'var(--red)' };
      const dot = document.getElementById('exec-regime-dot');
      dot.style.background = colors[_regime] || 'var(--txt3)';
      dot.style.boxShadow  = `0 0 10px ${colors[_regime] || 'var(--txt3)'}`;
      set('exec-regime-label', _regime + ' Market');
      set('exec-regime-sub', `${d.regime.breadth_pct||0}% stocks above SMA50 · ${d.regime.volatility_label||''} volatility · ${d.regime.symbols_checked||0} stocks analysed`);
      const thresh = { Bull:'55%', Neutral:'60%', Bear:'65%' };
      set('exec-threshold', thresh[_regime] || '60%');
    }

    // AI Insight
    const insightTexts = {
      Bull:    `<span class="ai-insight-key">Bull regime detected</span> — ${d.regime?.breadth_pct||0}% of Nifty100 stocks are above SMA50. Broad participation confirms bullish breadth. AI buy threshold is lowered to <span class="ai-insight-key">55%</span> to capture more opportunities. Focus on momentum and breakout setups. Trend-following strategies outperform in this environment.`,
      Neutral: `<span class="ai-insight-key">Neutral/consolidating market</span> — ${d.regime?.breadth_pct||0}% breadth indicates mixed conditions. AI buy threshold set at <span class="ai-insight-key">60%</span>. Be selective — favour stocks with multi-strategy confluence and stronger relative strength. Mean reversion setups may complement trend trades.`,
      Bear:    `<span class="ai-insight-key">Bear regime</span> — only ${d.regime?.breadth_pct||0}% stocks above SMA50. Elevated threshold of <span class="ai-insight-key">65%</span> ensures only the highest-conviction setups pass. Keep positions smaller. Prefer defensive sectors (FMCG, Pharma) and stocks showing relative strength against the index.`,
    };
    const insightText = insightTexts[_regime] || insightTexts['Neutral'];
    document.getElementById('exec-ai-insight-text').innerHTML = insightText;
    insight.style.display = 'block';

    if (!d.results || !d.results.length) {
      const pct = Math.round((d.buy_threshold || 0) * 100);
      body.innerHTML = `<div class="empty"><div class="empty-icon" style="background:var(--red-d);">🔍</div><div class="empty-title">No confirmed trades today</div><div class="empty-sub">Strategies found ${d.symbols_with_signals||0} setups but none cleared the ${pct}% AI threshold in the current ${_regime} market. Try again tomorrow or after new data loads.</div></div>`;
      return;
    }

    // Cache for HOME tab
    _homePicksCache = d.results;
    renderHomePicks(d.results);

    body.innerHTML = d.results.map((r, i) => {
      const prob  = r.buy_probability || 0;
      const pct   = Math.round(prob * 100);
      const color = _probColor(prob);
      const conf  = r.confidence || '';
      const fmt   = v => v != null ? '₹' + Number(v).toLocaleString('en-IN', {maximumFractionDigits:2}) : '—';

      // Quality score ring
      const qs = r.quality_score != null ? r.quality_score : null;
      const qsColor = qs == null ? 'var(--txt3)' : qs >= 75 ? 'var(--green)' : qs >= 50 ? 'var(--live)' : 'var(--red)';
      const qsRing = qs != null ? `
        <div title="Quality Score: ${qs}/100" style="flex-shrink:0;display:flex;flex-direction:column;align-items:center;gap:1px;">
          <div style="position:relative;width:44px;height:44px;">
            <svg width="44" height="44" viewBox="0 0 44 44" style="transform:rotate(-90deg);position:absolute;top:0;left:0;">
              <circle cx="22" cy="22" r="17" fill="none" stroke="var(--s3)" stroke-width="4"/>
              <circle cx="22" cy="22" r="17" fill="none" stroke="${qsColor}" stroke-width="4"
                stroke-dasharray="${Math.round((qs/100)*106.8)} 106.8" stroke-linecap="round"/>
            </svg>
            <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:11px;font-weight:800;color:${qsColor};line-height:1;">${qs}</div>
          </div>
          <div style="font-size:8px;color:var(--txt3);letter-spacing:.3px;font-weight:600;">SCORE</div>
        </div>` : '';

      // R:R ratio bar
      const entry  = r.entry || r.price || 0;
      const sl     = r.stop_loss || 0;
      const tgt    = r.target || r.price_target || 0;
      const risk   = entry > 0 && sl > 0 ? Math.abs(entry - sl) : 0;
      const reward = entry > 0 && tgt > 0 ? Math.abs(tgt - entry) : 0;
      const rr     = risk > 0 ? (reward / risk) : 0;
      const rrPct  = rr > 0 ? Math.min((reward / (risk + reward)) * 100, 95) : 0;
      const rrBar  = risk > 0 ? `
        <div class="rr-bar-wrap">
          <span class="rr-label" style="color:var(--red);">Risk</span>
          <div class="rr-bar-track">
            <div class="rr-bar-reward" style="width:${rrPct}%;"></div>
          </div>
          <span class="rr-label" style="color:var(--green);">R:R ${rr.toFixed(1)}</span>
        </div>` : '';

      // Sector badge
      const sector = _getSector(r.symbol);
      const sectorBadge = `<span class="sector-badge">${sector}</span>`;

      // AI reasoning line
      const aiReason = `<div class="ai-reason">${_buildAIReason(r, _regime)}</div>`;

      const stratTags = (r.strategies || []).map(s =>
        `<span class="strat-tag">${_stratLabel(s)}</span>`
      ).join('');
      const multiTag = r.strategy_count > 1
        ? `<span class="strat-tag" style="background:var(--data-d);color:var(--data);border-color:rgba(99,102,241,.3);">⚡ ${r.strategy_count} strategies</span>`
        : '';

      // Multi-horizon badges
      const horizonRow = (r.prob_3d != null || r.prob_5d != null || r.prob_10d != null) ? (() => {
        const make = (label, p) => {
          if (p == null) return '';
          const c = _hColor(p); const pp = Math.round(p * 100);
          return `<div class="horizon-badge" style="background:${c.bg};border-color:${c.border};color:${c.color};">
            <span class="horizon-badge-label">${label}</span>
            <span class="horizon-badge-pct">${pp}%</span>
          </div>`;
        };
        return `<div class="horizon-row">${make('3D',r.prob_3d)}${make('5D',r.prob_5d||r.buy_probability)}${make('10D',r.prob_10d)}</div>`;
      })() : '';

      // Stability badge
      const stabMap = {
        HIGH:         {bg:'var(--green-d)',color:'var(--green)',border:'rgba(52,211,153,.3)',icon:'●'},
        MEDIUM:       {bg:'var(--live-d)',color:'var(--live)',border:'rgba(251,191,36,.3)',icon:'●'},
        LOW:          {bg:'var(--red-d)',color:'var(--red)',border:'rgba(248,113,113,.3)',icon:'●'},
        INSUFFICIENT: {bg:'var(--s3)',color:'var(--txt3)',border:'var(--border)',icon:'○'},
      };
      const stabBadge = r.stability ? (() => {
        const s = stabMap[r.stability] || stabMap.INSUFFICIENT;
        return `<span class="stab-badge" style="background:${s.bg};color:${s.color};border:1px solid ${s.border};">${s.icon} ${r.stability}</span>`;
      })() : '';

      // Meta filter pill
      const metaDecision = r.meta_filter?.decision;
      const metaPill = metaDecision ? (() => {
        const isTake = metaDecision === 'TAKE';
        const style = isTake
          ? 'background:var(--green-d);color:var(--green);border:1px solid rgba(52,211,153,.3);'
          : 'background:var(--red-d);color:var(--red);border:1px solid rgba(248,113,113,.3);';
        const prob_meta = r.meta_filter?.take_probability != null
          ? ` ${Math.round(r.meta_filter.take_probability*100)}%` : '';
        return `<span class="meta-pill" style="${style}">${isTake ? '✓ TAKE' : '✗ AVOID'}${prob_meta}</span>`;
      })() : '';

      // AI Explanation accordion
      const expl = r.explanation;
      const explSection = expl ? (() => {
        const uid = `expl-${r.symbol}-${i}`;
        const bullets = (expl.bullets || []).map(b => `<li>${b}</li>`).join('');
        const cautions = (expl.cautions || []).map(c => `<li>${c}</li>`).join('');
        return `
          <div class="ai-expl-wrap">
            <button class="ai-expl-toggle" onclick="(function(el){const b=document.getElementById('${uid}');b.classList.toggle('open');el.querySelector('span.chevron').textContent=b.classList.contains('open')?'▲':'▼';})(this)">
              <svg width="13" height="13" fill="none" stroke="var(--data)" viewBox="0 0 24 24" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z"/></svg>
              AI Analysis <span class="chevron" style="margin-left:auto;font-size:9px;">▼</span>
            </button>
            <div class="ai-expl-body" id="${uid}">
              <div class="ai-expl-headline">${expl.headline || ''}</div>
              ${bullets ? `<ul class="ai-expl-bullets">${bullets}</ul>` : ''}
              ${cautions ? `<ul class="ai-expl-cautions">${cautions}</ul>` : ''}
              ${expl.summary ? `<div class="ai-expl-summary">${expl.summary}</div>` : ''}
              ${expl.score_narrative ? `<div class="ai-expl-score">${expl.score_narrative}</div>` : ''}
            </div>
          </div>`;
      })() : '';

      const priceLabel = r.price_source === 'live'
        ? `<span style="color:var(--green);font-size:10px;font-weight:500;">●LIVE</span>`
        : `<span style="color:var(--txt3);font-size:10px;">(last close)</span>`;

      const expRet = r.expected_return_pct != null
        ? `<span style="color:var(--green);font-size:12px;font-weight:500;">+${r.expected_return_pct}% AI target</span>`
        : '';

      const periodLabels = { '1m':'1M', '3m':'3M', '6m':'6M', '1y':'1Y' };
      const trendTag = (r.trend_return_pct != null && r.trend_period && r.trend_period !== 'any')
        ? `<span style="font-size:10px;font-weight:500;padding:2px 7px;border-radius:5px;background:var(--green-d);color:var(--green);border:1px solid rgba(59,109,17,.2);">${periodLabels[r.trend_period]||''} trend +${r.trend_return_pct}%</span>`
        : '';

      const rankBadge = i < 3
        ? `<span style="font-size:9px;font-weight:500;padding:2px 7px;border-radius:5px;background:var(--data-d);color:var(--data);margin-left:6px;">#${i+1}</span>`
        : '';

      return `
        <div class="comb-card" data-sector="${_getSector(r.symbol)}">
          <div class="comb-card-top">
            <div style="display:flex;align-items:center;flex:1;min-width:0;gap:6px;">
              <div class="comb-symbol">${r.symbol}</div>${rankBadge}${sectorBadge}
            </div>
            <div class="comb-prob-wrap" style="flex:1;">
              <div style="display:flex;justify-content:space-between;align-items:baseline;">
                <span style="font-size:10px;color:var(--txt3);">AI probability</span>
                <span class="comb-prob-pct" style="color:${color};">${pct}%</span>
              </div>
              <div class="comb-prob-bar-bg">
                <div class="comb-prob-bar-fill" style="width:${pct}%;background:${color};"></div>
              </div>
              <div style="display:flex;align-items:center;gap:5px;margin-top:5px;flex-wrap:wrap;">
                <div class="comb-conf-badge" style="${_confBadgeStyle(conf)};display:inline-block;">${conf}</div>
                ${stabBadge}
                ${metaPill}
              </div>
            </div>
            ${qsRing}
          </div>

          ${horizonRow}

          <div class="comb-strategies">${multiTag}${trendTag}${stratTags}</div>

          ${aiReason}
          ${explSection}

          <div class="comb-trade-grid" style="margin-top:10px;">
            <div class="comb-trade-cell">
              <div class="comb-trade-label">Entry</div>
              <div class="comb-trade-val">${fmt(entry)}</div>
            </div>
            <div class="comb-trade-cell" style="border:0.5px solid rgba(163,45,45,.2);background:var(--red-d);">
              <div class="comb-trade-label" style="color:var(--red);">Stop Loss</div>
              <div class="comb-trade-val" style="color:var(--red);">${fmt(sl)}</div>
            </div>
            <div class="comb-trade-cell" style="border:0.5px solid rgba(59,109,17,.2);background:var(--green-d);">
              <div class="comb-trade-label" style="color:var(--green);">Target</div>
              <div class="comb-trade-val" style="color:var(--green);">${fmt(tgt)}</div>
            </div>
          </div>

          ${rrBar}

          <div class="comb-footer">
            <div style="display:flex;align-items:center;gap:6px;">
              ${priceLabel}
              <span style="font-size:13px;font-weight:700;">${fmt(r.price)}</span>
            </div>
            ${expRet}
          </div>
          <div style="padding:10px 16px 14px;border-top:0.5px solid var(--border);display:flex;align-items:center;gap:8px;">
            <button onclick='openChart("${r.symbol}", ${JSON.stringify({stop_loss:r.stop_loss,target:r.target||r.price_target,buy_probability:r.buy_probability,entry:r.entry||r.price})})'
              style="flex:1;padding:10px 14px;font-size:12px;font-weight:600;background:var(--data-d);color:var(--data);border:1px solid rgba(129,140,248,.2);border-radius:10px;cursor:pointer;">
              📈 View Chart
            </button>
          </div>
        </div>`;
    }).join('');

    // Sector filter bar
    _buildSectorFilter(d.results);

  } catch(e) {
    insight.style.display = 'none';
    body.innerHTML = `<div class="empty"><div class="empty-icon">⚠️</div><div class="empty-title">Error</div><div class="empty-sub">${e.message || 'Check that ML model is trained'}</div></div>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z"/></svg> Find Today\'s Best Trades';
  }
}

// ══════════════════════════════════════════════════════
// SCREENER
// ══════════════════════════════════════════════════════

let _scrCurrent = 'sector';

function scrGo(id, el) {
  _scrCurrent = id;
  document.querySelectorAll('.scr-panel').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.scr-tab-btn').forEach(b => b.classList.remove('active'));
  const panel = document.getElementById('scr-' + id);
  if (panel) panel.style.display = 'block';
  if (el) el.classList.add('active');

  // Show/hide index selector bar (not relevant for sector heatmap)
  const bar = document.getElementById('scr-index-bar');
  if (bar) bar.style.display = (id === 'sector') ? 'none' : 'flex';

  // Auto-load data when switching to screener tabs
  if (id === 'rs')       loadRS();
  else if (id === '52w') load52W();
  else if (id === 'volume') loadVolume();
  else if (id === 'earnings') loadEarnings();
}

function scrRefreshCurrent() {
  if (_scrCurrent === 'rs')       loadRS();
  else if (_scrCurrent === '52w') load52W();
  else if (_scrCurrent === 'volume') loadVolume();
  else if (_scrCurrent === 'earnings') loadEarnings();
}

function _scrIndex() {
  return document.getElementById('scr-index-select')?.value || '';
}

// Populate both index dropdowns from API
async function loadIndexOptions() {
  try {
    const d = await jAuth(`${API}/api/indices`);
    const indices = d.indices || [];

    // Group by category for <optgroup>
    const groups = {};
    indices.forEach(idx => {
      (groups[idx.category] = groups[idx.category] || []).push(idx);
    });

    function buildOptions() {
      let html = '<option value="">All Symbols</option>';
      for (const [cat, items] of Object.entries(groups)) {
        html += `<optgroup label="${cat}">`;
        items.forEach(i => {
          html += `<option value="${i.name}">${i.name} (${i.count})</option>`;
        });
        html += '</optgroup>';
      }
      return html;
    }

    const scrSel = document.getElementById('scr-index-select');
    if (scrSel) scrSel.innerHTML = buildOptions();

    // Undervalued screener — same indices, default to NIFTY 500
    const uvSel = document.getElementById('uv-index-select');
    if (uvSel) {
      let html = '';
      for (const [cat, items] of Object.entries(groups)) {
        html += `<optgroup label="${cat}">`;
        items.forEach(i => {
          html += `<option value="${i.name}"${i.name === 'NIFTY 500' ? ' selected' : ''}>${i.name} (${i.count})</option>`;
        });
        html += '</optgroup>';
      }
      uvSel.innerHTML = html;
    }

    // Backfill selector: "All Symbols" means full universe
    const yfSel = document.getElementById('yf-index-select');
    if (yfSel) {
      let html = '<option value="">All Symbols (full universe)</option>';
      for (const [cat, items] of Object.entries(groups)) {
        html += `<optgroup label="${cat}">`;
        items.forEach(i => {
          html += `<option value="${i.name}">${i.name} (${i.count})</option>`;
        });
        html += '</optgroup>';
      }
      yfSel.innerHTML = html;
    }
  } catch(e) {}
}

// ── Sector Heatmap ──────────────────────────────────────
async function refreshSectors(btn) {
  if (btn) { btn.disabled = true; btn.textContent = '↻ Loading…'; }
  try {
    await loadSectors();
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '↻ Refresh'; }
  }
}

async function loadSectors() {
  const body = document.getElementById('scr-sector-body');
  body.innerHTML = '<div class="empty"><div class="empty-icon" style="animation:spin 1s linear infinite">⏳</div><div class="empty-title">Calculating sector performance…</div></div>';
  try {
    const d = await jAuth(`${API}/api/sector/heatmap`);
    const sectors = d.sectors || [];
    set('scr-total', sectors.length);
    if (!sectors.length) { body.innerHTML = '<div class="empty"><div class="empty-title">No data — run a backfill first</div></div>'; return; }

    body.innerHTML = sectors.map(s => {
      const r1d  = s.avg_1d  != null ? _retSpan(s.avg_1d)  : '—';
      const r1w  = s.avg_1w  != null ? _retSpan(s.avg_1w)  : '—';
      const r1m  = s.avg_1m  != null ? _retSpan(s.avg_1m)  : '—';
      const r3m  = s.avg_3m  != null ? _retSpan(s.avg_3m)  : '—';
      const barColor = (s.avg_1m || 0) >= 0 ? 'var(--green)' : 'var(--red)';
      const barWidth = Math.min(Math.abs(s.avg_1m || 0) * 5, 100);

      const stockRows = (s.stocks || []).map(st => `
        <div style="display:flex;justify-content:space-between;align-items:center;
                    padding:6px 0;border-top:0.5px solid rgba(0,0,0,0.06);">
          <div>
            <span style="font-size:13px;font-weight:700;color:var(--txt);">${st.symbol}</span>
            <span style="font-size:11px;color:var(--txt3);margin-left:6px;">₹${st.last_price != null ? st.last_price.toLocaleString('en-IN') : '—'}</span>
          </div>
          <div style="display:flex;gap:10px;font-size:11px;font-weight:600;">
            <span style="color:var(--txt3);">1D&nbsp;${_retSpan(st.r1d)}</span>
            <span style="color:var(--txt3);">1W&nbsp;${_retSpan(st.r5d)}</span>
            <span style="color:var(--txt3);">1M&nbsp;${_retSpan(st.r20d)}</span>
            <span style="color:var(--txt3);">3M&nbsp;${_retSpan(st.r60d)}</span>
          </div>
        </div>`).join('');

      return `
        <div class="sector-tile">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <div>
              <span style="font-size:14px;font-weight:800;">${s.sector}</span>
              <span style="font-size:10px;color:var(--txt3);margin-left:6px;">${s.stock_count} stocks</span>
            </div>
            <div style="text-align:right;">
              <div style="font-size:16px;font-weight:800;">${r1m}</div>
              <div style="font-size:9px;color:var(--txt3);">1-Month</div>
            </div>
          </div>
          <div style="background:var(--s2);border-radius:4px;height:4px;margin-bottom:10px;">
            <div style="height:4px;border-radius:4px;background:${barColor};width:${barWidth}%;"></div>
          </div>
          <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:4px;text-align:center;margin-bottom:${stockRows ? '10px' : '0'};">
            <div><div style="font-size:9px;color:var(--txt3);">1D Avg</div><div style="font-size:12px;font-weight:700;">${r1d}</div></div>
            <div><div style="font-size:9px;color:var(--txt3);">1W Avg</div><div style="font-size:12px;font-weight:700;">${r1w}</div></div>
            <div><div style="font-size:9px;color:var(--txt3);">1M Avg</div><div style="font-size:12px;font-weight:700;">${r1m}</div></div>
            <div><div style="font-size:9px;color:var(--txt3);">3M Avg</div><div style="font-size:12px;font-weight:700;">${r3m}</div></div>
          </div>
          ${stockRows}
        </div>`;
    }).join('');
  } catch(e) {
    body.innerHTML = `<div class="empty"><div class="empty-title">Error: ${e.message}</div></div>`;
  }
}

function _retSpan(v) {
  if (v == null) return '—';
  const color = v >= 0 ? 'var(--green)' : 'var(--red)';
  return `<span style="color:${color};font-weight:700;">${v >= 0 ? '+' : ''}${v}%</span>`;
}

// ── Relative Strength ────────────────────────────────────
async function loadRS() {
  const body = document.getElementById('scr-rs-body');
  const idx = _scrIndex();
  body.innerHTML = '<div class="empty"><div class="empty-icon" style="animation:spin 1s linear infinite">⏳</div><div class="empty-title">Calculating RS scores…</div></div>';
  try {
    const url = idx ? `${API}/api/screener/rs?index=${encodeURIComponent(idx)}` : `${API}/api/screener/rs`;
    const d = await jAuth(url);
    const rows = (d.results || []).slice(0, 50);
    const gradeColors = { 'A+':'var(--green)', 'A':'var(--green)', 'B':'var(--data)', 'C':'var(--live)', 'D':'var(--red)', '—':'var(--txt3)' };
    body.innerHTML = rows.map((r, i) => `
      <div class="scr-row">
        <div>
          <div style="display:flex;align-items:center;gap:6px;">
            <span class="scr-sym">${r.symbol}</span>
            <span class="scr-badge" style="background:var(--data-d);color:var(--data);">${r.sector}</span>
          </div>
          <div style="font-size:11px;color:var(--txt3);margin-top:2px;">20D: ${_retSpan(r.return_20d)} &nbsp; 50D: ${_retSpan(r.return_50d)}</div>
        </div>
        <div style="text-align:right;">
          <div style="font-size:20px;font-weight:900;color:${gradeColors[r.rs_grade]};">${r.rs_grade}</div>
          <div style="font-size:10px;color:var(--txt3);">RS ${r.rs_20 != null ? r.rs_20 : '—'}</div>
        </div>
      </div>`).join('') || '<div class="empty"><div class="empty-title">No data</div></div>';
  } catch(e) {
    body.innerHTML = `<div class="empty"><div class="empty-title">Error: ${e.message}</div></div>`;
  }
}

// ── 52W High ─────────────────────────────────────────────
async function load52W() {
  const body = document.getElementById('scr-52w-body');
  const idx = _scrIndex();
  body.innerHTML = '<div class="empty"><div class="empty-icon" style="animation:spin 1s linear infinite">⏳</div><div class="empty-title">Scanning 52-week highs…</div></div>';
  try {
    const url = idx ? `${API}/api/screener/52w?index=${encodeURIComponent(idx)}` : `${API}/api/screener/52w`;
    const d = await jAuth(url);
    const rows = (d.results || []).slice(0, 50);
    body.innerHTML = rows.map(r => {
      const breakoutBadge = r.is_breakout
        ? `<span class="scr-badge" style="background:rgba(16,185,129,.2);color:var(--green);">BREAKOUT</span>`
        : '';
      const pct = r.pct_from_high;
      const pctColor = pct >= -2 ? 'var(--green)' : pct >= -10 ? 'var(--live)' : 'var(--txt3)';
      return `
        <div class="scr-row">
          <div>
            <div style="display:flex;align-items:center;gap:6px;">
              <span class="scr-sym">${r.symbol}</span>${breakoutBadge}
              <span class="scr-badge" style="background:var(--s3);color:var(--txt3);">${r.sector}</span>
            </div>
            <div style="font-size:11px;color:var(--txt3);margin-top:2px;">52W High: ₹${r.high_52w?.toLocaleString('en-IN')} &nbsp; Vol: ${r.vol_ratio}x avg</div>
          </div>
          <div style="text-align:right;">
            <div style="font-size:15px;font-weight:800;color:${pctColor};">${pct >= 0 ? '+' : ''}${pct}%</div>
            <div style="font-size:10px;color:var(--txt3);">from 52W high</div>
          </div>
        </div>`;
    }).join('') || '<div class="empty"><div class="empty-title">No data</div></div>';
  } catch(e) {
    body.innerHTML = `<div class="empty"><div class="empty-title">Error: ${e.message}</div></div>`;
  }
}

// ── Volume Spikes ────────────────────────────────────────
async function loadVolume() {
  const body = document.getElementById('scr-volume-body');
  const idx = _scrIndex();
  body.innerHTML = '<div class="empty"><div class="empty-icon" style="animation:spin 1s linear infinite">⏳</div><div class="empty-title">Scanning volume spikes…</div></div>';
  try {
    const url = idx ? `${API}/api/screener/volume?index=${encodeURIComponent(idx)}` : `${API}/api/screener/volume`;
    const d = await jAuth(url);
    const rows = d.results || [];
    const sigColors = { 'Accumulation':'var(--green)', 'Distribution':'var(--red)', 'Neutral':'var(--txt3)' };
    body.innerHTML = rows.map(r => `
      <div class="scr-row">
        <div>
          <div style="display:flex;align-items:center;gap:6px;">
            <span class="scr-sym">${r.symbol}</span>
            <span class="scr-badge" style="color:${sigColors[r.signal]};background:var(--s3);">${r.signal.toUpperCase()}</span>
          </div>
          <div style="font-size:11px;color:var(--txt3);margin-top:2px;">${r.sector} &nbsp;·&nbsp; Change: ${_retSpan(r.change_pct)}</div>
        </div>
        <div style="text-align:right;">
          <div style="font-size:18px;font-weight:800;color:var(--live);">${r.vol_ratio}x</div>
          <div style="font-size:10px;color:var(--txt3);">avg volume</div>
        </div>
      </div>`).join('') || '<div class="empty"><div class="empty-title">No volume spikes today</div><div class="empty-sub">Market may be in low-volume consolidation</div></div>';
  } catch(e) {
    body.innerHTML = `<div class="empty"><div class="empty-title">Error: ${e.message}</div></div>`;
  }
}

// ── Earnings Calendar ────────────────────────────────────
async function loadEarnings() {
  const body = document.getElementById('scr-earnings-body');
  const idx = _scrIndex();
  body.innerHTML = '<div class="empty"><div class="empty-icon" style="animation:spin 1s linear infinite">⏳</div><div class="empty-title">Fetching from Yahoo Finance…</div><div class="empty-sub">Takes ~30–60 seconds</div></div>';
  try {
    const url = idx ? `${API}/api/earnings?index=${encodeURIComponent(idx)}` : `${API}/api/earnings`;
    const d = await jAuth(url);
    const rows = d.results || [];
    if (!rows.length) {
      body.innerHTML = '<div class="empty"><div class="empty-icon">✅</div><div class="empty-title">No earnings in next 21 days</div><div class="empty-sub">Safe to hold positions</div></div>';
      return;
    }
    body.innerHTML = rows.map(r => {
      const riskColor = r.risk_level === 'High' ? 'var(--red)' : 'var(--live)';
      return `
        <div class="scr-row">
          <div>
            <div style="display:flex;align-items:center;gap:6px;">
              <span class="scr-sym">${r.symbol}</span>
              <span class="scr-badge" style="background:var(--s3);color:var(--txt3);">${r.sector}</span>
            </div>
            <div style="font-size:11px;color:var(--txt3);margin-top:2px;">Results on ${r.earnings_date}</div>
          </div>
          <div style="text-align:right;">
            <div style="font-size:14px;font-weight:800;color:${riskColor};">${r.risk_level} Risk</div>
            <div style="font-size:10px;color:var(--txt3);">in ${r.days_away} day${r.days_away !== 1 ? 's' : ''}</div>
          </div>
        </div>`;
    }).join('');
  } catch(e) {
    body.innerHTML = `<div class="empty"><div class="empty-title">Error: ${e.message}</div></div>`;
  }
}

// ══════════════════════════════════════════════════════
// ── Execute Tab Notification System ───────────────────
//
// Polls /api/execute/poll every 5 minutes.
// • Shows a red badge on the Execute nav button when trades > 0.
// • Sends a browser push notification when new trades appear.
// • On first open, requests Notification permission.

let _execPrevCount = null;   // track previous count to detect changes

function _updateExecuteBadge(count) {
  const badge = document.getElementById('execute-badge');
  if (!badge) return;
  if (count > 0) {
    badge.textContent = count > 99 ? '99+' : count;
    badge.style.display = 'block';
  } else {
    badge.style.display = 'none';
  }
}

function _requestNotificationPermission() {
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }
}

function _sendBrowserNotification(count, symbols, regime) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  const topStr = symbols.length > 0 ? symbols.slice(0, 3).join(', ') : '';
  const body = topStr
    ? `${count} trade${count > 1 ? 's' : ''} ready: ${topStr}${count > 3 ? ' & more' : ''} · ${regime} market`
    : `${count} high-conviction trade${count > 1 ? 's' : ''} ready · ${regime} market`;
  try {
    const n = new Notification('⚡ APEX — New Trades Ready', {
      body,
      icon: '/favicon.ico',
      tag: 'apex-execute',   // replaces previous notification instead of stacking
      renotify: count !== _execPrevCount,
    });
    n.onclick = () => {
      window.focus();
      // Switch to execute tab
      const btn = document.querySelector('.nav-btn[data-tab="execute"]');
      if (btn) go('execute', btn);
      n.close();
    };
  } catch(e) {}
}

async function _pollExecute() {
  try {
    const d = await jAuth(`${API}/api/execute/poll`);
    const count = d.count || 0;
    _updateExecuteBadge(count);

    // Notify only when count goes from 0→N or increases
    if (_execPrevCount !== null && count > 0 && count !== _execPrevCount) {
      _sendBrowserNotification(count, d.top_symbols || [], d.regime || 'Neutral');
    }
    _execPrevCount = count;

    // Populate Home tab picks whenever the poll returns fresh data
    if (d.top_results && d.top_results.length > 0) {
      _homePicksCache = d.top_results;
      renderHomePicks(_homePicksCache);
    }
  } catch(e) {}
}

async function _syncHomePicks() {
  const picks = document.getElementById('hm-picks');

  // First poll — also triggers background refresh if cache is stale
  let d;
  try { d = await jAuth(`${API}/api/execute/poll`); } catch(e) { return; }

  if (d.top_results && d.top_results.length > 0) {
    _homePicksCache = d.top_results;
    renderHomePicks(_homePicksCache);
    return;
  }

  // Cache is empty — show loading state and wait for the background refresh
  if (picks) {
    picks.innerHTML = `<div style="padding:32px 20px;text-align:center;">
      <div class="spin" style="margin:0 auto 10px;width:20px;height:20px;border-width:2px;color:var(--data);"></div>
      <div style="font-size:12px;color:var(--txt3);">Syncing today's best trades…</div>
    </div>`;
  }

  // Retry up to 6× (every 5 s = 30 s total) waiting for refresh to finish
  for (let i = 0; i < 6; i++) {
    await new Promise(r => setTimeout(r, 5000));
    try { d = await jAuth(`${API}/api/execute/poll`); } catch(e) { break; }

    if (d.top_results && d.top_results.length > 0) {
      _homePicksCache = d.top_results;
      renderHomePicks(_homePicksCache);
      return;
    }
    // Refresh finished but found no trades — stop waiting
    if (!d.is_refreshing) break;
  }

  // Restore empty state — nothing came back
  if (picks && !(_homePicksCache && _homePicksCache.length > 0)) {
    picks.innerHTML = `<div class="empty" style="padding:28px 20px;">
      <div class="empty-icon" style="font-size:20px;">⚡</div>
      <div class="empty-title">No trades found today</div>
      <div class="empty-sub">Market conditions may not show strong setups right now. Check back after market hours or run a manual scan.</div>
    </div>`;
  }
}

// Request permission on page load (subtle — no UI prompt before interaction)
document.addEventListener('click', _requestNotificationPermission, { once: true });

// Poll immediately then every 5 minutes
_pollExecute();
setInterval(_pollExecute, 5 * 60 * 1000);


// ── Fyers OAuth ────────────────────────────────────────
async function connectFyers() {
  const btn = document.getElementById('fyers-connect-btn');
  btn.disabled = true;
  btn.innerHTML = '<div class="spin" style="color:#fff;"></div> Opening…';
  try {
    const d = await jAuth(`${API}/api/fyers/login_url`);
    if (!d.url) throw new Error('No URL returned');

    // Open Fyers login in a popup window
    const popup = window.open(d.url, 'fyers_login',
      'width=520,height=680,scrollbars=yes,resizable=yes');

    // Listen for the callback page to post a message back
    const handler = async (e) => {
      if (e.data === 'fyers_connected') {
        window.removeEventListener('message', handler);
        await refreshStatus();
        btn.disabled = false;
        btn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"/></svg> Connect Fyers';
      }
    };
    window.addEventListener('message', handler);

    // Also poll in case postMessage doesn't fire (popup blocked scenario)
    const poll = setInterval(async () => {
      if (!popup || popup.closed) {
        clearInterval(poll);
        window.removeEventListener('message', handler);
        await refreshStatus();
        btn.disabled = false;
        btn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"/></svg> Connect Fyers';
      }
    }, 1000);

  } catch(e) {
    btn.disabled = false;
    btn.innerHTML = 'Connect Fyers';
    alert('Error: ' + e.message);
  }
}

// ══════════════════════════════════════════════════════
// PORTFOLIO BUILDER
// ══════════════════════════════════════════════════════

async function buildPortfolio() {
  const btn = document.getElementById('pb-btn');
  btn.disabled = true;
  btn.innerHTML = '<div class="spin" style="color:#fff;width:16px;height:16px;border-width:2px;"></div> Building…';

  const capital  = parseFloat(document.getElementById('pb-capital').value) || 100000;
  const riskPct  = parseFloat(document.getElementById('pb-risk').value)    || 1.5;
  const maxPos   = parseInt(document.getElementById('pb-maxpos').value)    || 8;
  const maxSec   = parseInt(document.getElementById('pb-sector').value)    || 2;
  const trend    = document.getElementById('pb-trend').value               || 'any';

  try {
    // 1. Fetch current signals
    const sigUrl = `${API}/api/combined/signals?trend_period=${trend}`;
    const sigData = await jAuth(sigUrl);
    const trades = sigData.results || [];

    if (!trades.length) {
      document.getElementById('pb-positions-card').style.display = 'none';
      document.getElementById('pb-summary').style.display = 'block';
      document.getElementById('pb-s-deployed').textContent = '—';
      document.getElementById('pb-s-risk').textContent = '—';
      document.getElementById('pb-s-reward').textContent = '—';
      document.getElementById('pb-s-detail').textContent = 'No signals available — run Execute tab first.';
      return;
    }

    // 2. Build portfolio
    const res = await jAuth(`${API}/api/portfolio/build`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ capital, trades, risk_pct: riskPct, max_positions: maxPos, max_sector_exposure: maxSec }),
    });

    const positions = res.positions || [];
    const s = res.summary || {};
    const fmt = v => v != null ? '₹' + Number(v).toLocaleString('en-IN', {maximumFractionDigits:0}) : '—';
    const fmtPct = v => v != null ? v + '%' : '—';

    // Show summary strip
    const summaryEl = document.getElementById('pb-summary');
    summaryEl.style.display = 'block';
    document.getElementById('pb-s-deployed').textContent = fmt(s.total_deployed) + ' (' + fmtPct(s.deployed_pct) + ')';
    document.getElementById('pb-s-risk').textContent     = fmt(s.total_at_risk)  + ' (' + fmtPct(s.risk_pct_of_cap) + ')';
    document.getElementById('pb-s-reward').textContent   = s.expected_reward > 0 ? fmt(s.expected_reward) : '—';
    document.getElementById('pb-s-detail').textContent   = `${s.positions_count} positions · Cash: ${fmt(s.cash_remaining)} · Sectors: ${(s.sectors_used||[]).join(', ') || '—'}`;

    // Show positions table
    const card = document.getElementById('pb-positions-card');
    const body = document.getElementById('pb-positions-body');
    if (!positions.length) {
      card.style.display = 'block';
      body.innerHTML = '<div class="empty" style="padding:24px 0;"><div class="empty-title">No positions could be sized</div><div class="empty-sub">Check stop_loss values in signals</div></div>';
      return;
    }
    card.style.display = 'block';
    body.innerHTML = positions.map((p, i) => {
      const rrColor = !p.rr_ratio ? 'var(--txt3)' : p.rr_ratio >= 2 ? 'var(--green)' : p.rr_ratio >= 1 ? 'var(--live)' : 'var(--red)';
      const qsColor = !p.quality_score ? 'var(--txt3)' : p.quality_score >= 75 ? 'var(--green)' : p.quality_score >= 50 ? 'var(--live)' : 'var(--red)';
      return `
        <div style="padding:13px 16px;border-bottom:1px solid var(--border);">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <div style="display:flex;align-items:center;gap:8px;">
              <span style="font-size:15px;font-weight:800;">${p.symbol}</span>
              <span style="font-size:9px;padding:2px 7px;border-radius:5px;background:var(--s3);color:var(--txt3);">${p.sector}</span>
              ${p.quality_score != null ? `<span style="font-size:10px;font-weight:700;color:${qsColor};">Q${p.quality_score}</span>` : ''}
            </div>
            <div style="text-align:right;">
              <div style="font-size:14px;font-weight:700;">${fmt(p.position_value)}</div>
              <div style="font-size:10px;color:var(--txt3);">${p.pct_of_portfolio}% of capital · ${p.shares} shares</div>
            </div>
          </div>
          <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;font-size:11px;">
            <div><div style="color:var(--txt3);margin-bottom:2px;">Entry</div><div style="font-weight:700;">₹${p.entry?.toLocaleString('en-IN',{maximumFractionDigits:1})}</div></div>
            <div><div style="color:var(--txt3);margin-bottom:2px;">Stop Loss</div><div style="font-weight:700;color:var(--red);">₹${p.stop_loss?.toLocaleString('en-IN',{maximumFractionDigits:1})}</div></div>
            <div><div style="color:var(--txt3);margin-bottom:2px;">Target</div><div style="font-weight:700;color:var(--green);">${p.target ? '₹'+p.target?.toLocaleString('en-IN',{maximumFractionDigits:1}) : '—'}</div></div>
            <div><div style="color:var(--txt3);margin-bottom:2px;">R:R</div><div style="font-weight:700;color:${rrColor};">${p.rr_ratio != null ? p.rr_ratio + 'x' : '—'}</div></div>
          </div>
          <div style="margin-top:8px;display:flex;justify-content:space-between;font-size:11px;color:var(--txt3);">
            <span>Risk: <b style="color:var(--red);">${fmt(p.risk_amount)}</b></span>
            ${p.potential_reward ? `<span>Reward: <b style="color:var(--green);">${fmt(p.potential_reward)}</b></span>` : ''}
          </div>
        </div>`;
    }).join('');

  } catch(e) {
    alert('Error building portfolio: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M3.75 3v11.25A2.25 2.25 0 006 16.5h2.25M3.75 3h-1.5m1.5 0h16.5m0 0h1.5m-1.5 0v11.25A2.25 2.25 0 0118 16.5h-2.25m-7.5 0h7.5m-7.5 0l-1 3m8.5-3l1 3m0 0l.5 1.5m-.5-1.5h-9.5m0 0l-.5 1.5"/></svg> Build Portfolio';
  }
}


// ══════════════════════════════════════════════════════
// ADMIN PANEL
// ══════════════════════════════════════════════════════

async function loadAdminStats() {
  try {
    const d = await jAuth(`${API}/api/admin/stats`);
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v ?? '—'; };
    set('adm-total-users', d.total_users ?? '—');
    set('adm-active-subs', d.active_subscriptions ?? '—');
    set('adm-expired',     d.expired_subscriptions ?? '—');
  } catch(e) {}
}

async function adminCreateUser() {
  const btn    = document.getElementById('adm-create-btn');
  const msgEl  = document.getElementById('adm-create-msg');
  const uname  = document.getElementById('adm-new-username').value.trim();
  const email  = document.getElementById('adm-new-email').value.trim();
  const pass   = document.getElementById('adm-new-pass').value;
  const mobile = document.getElementById('adm-new-mobile').value.trim();
  const plan   = document.getElementById('adm-new-plan').value;

  msgEl.style.display = 'none';
  if (!uname || !email || !pass) {
    msgEl.textContent = 'Username, email and password are required';
    msgEl.style.cssText = 'font-size:12px;padding:8px;border-radius:8px;text-align:center;display:block;background:var(--red-d);color:var(--red);';
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Creating…';
  try {
    const res = await jAuth(`${API}/api/admin/users`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: uname, email, password: pass, mobile: mobile || null, plan_type: plan }),
    });
    document.getElementById('adm-new-username').value = '';
    document.getElementById('adm-new-email').value    = '';
    document.getElementById('adm-new-pass').value     = '';
    document.getElementById('adm-new-mobile').value   = '';
    msgEl.textContent = `✓ User "${uname}" created — welcome email sent to ${email}`;
    msgEl.style.cssText = 'font-size:12px;padding:8px;border-radius:8px;text-align:center;display:block;background:var(--green-d);color:var(--green);';
    await loadAdminUsers();
    await loadAdminStats();
  } catch(e) {
    msgEl.textContent = 'Error: ' + e.message;
    msgEl.style.cssText = 'font-size:12px;padding:8px;border-radius:8px;text-align:center;display:block;background:var(--red-d);color:var(--red);';
  } finally {
    btn.disabled = false;
    btn.textContent = '+ Create User';
  }
}

async function loadAdminUsers() {
  const body = document.getElementById('adm-users-body');
  body.innerHTML = '<div class="empty"><div class="empty-title" style="padding:16px 0;">Loading…</div></div>';
  try {
    const d = await jAuth(`${API}/api/admin/users`);
    const users = d.users || [];
    if (!users.length) {
      body.innerHTML = '<div class="empty" style="padding:24px 0;"><div class="empty-icon">👤</div><div class="empty-title">No users yet</div><div class="empty-sub">Create a user above</div></div>';
      return;
    }
    body.innerHTML = users.map(u => {
      const today   = new Date();
      const expiry  = u.plan_expiry ? new Date(u.plan_expiry) : null;
      const daysLeft = expiry ? Math.ceil((expiry - today) / 864e5) : null;
      const expired = daysLeft != null && daysLeft <= 0;
      const subColor = !u.is_active ? 'var(--txt3)' : expired ? 'var(--red)' : 'var(--green)';
      const subLabel = !u.is_active ? 'Inactive' : expired ? `Expired ${-daysLeft}d ago` : `${daysLeft}d left`;
      const planBadge = u.plan_type
        ? `<span style="font-size:9px;padding:2px 7px;border-radius:5px;background:var(--s3);color:var(--txt3);font-weight:700;">${u.plan_type.toUpperCase()}</span>`
        : '';
      return `
        <div style="padding:12px 16px;border-bottom:1px solid var(--border);">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
            <div style="display:flex;align-items:center;gap:8px;">
              <span style="font-size:13px;font-weight:700;">${u.username}</span>
              ${planBadge}
              <span style="font-size:10px;color:${subColor};font-weight:600;">${subLabel}</span>
            </div>
            <div style="display:flex;gap:6px;">
              <select onchange="adminRenewPlan(${u.id}, this.value)" style="font-size:11px;padding:3px 6px;background:var(--s2);border:1px solid var(--border);border-radius:6px;color:var(--txt3);">
                <option value="">Renew…</option>
                <option value="1m">+1M</option>
                <option value="3m">+3M</option>
                <option value="6m">+6M</option>
                <option value="12m">+12M</option>
              </select>
              <button onclick="adminToggleActive(${u.id}, ${!u.is_active})"
                style="font-size:11px;padding:3px 9px;border-radius:6px;cursor:pointer;background:${u.is_active ? '#FCEBEB' : '#EAF3DE'};color:${u.is_active ? 'var(--red)' : 'var(--green)'};border:1px solid ${u.is_active ? 'rgba(163,45,45,.3)' : 'rgba(59,109,17,.3)'};">
                ${u.is_active ? 'Deactivate' : 'Activate'}
              </button>
              <button onclick="adminDeleteUser(${u.id}, '${u.username}')"
                style="font-size:11px;padding:3px 9px;border-radius:6px;cursor:pointer;background:var(--s3);color:var(--txt3);border:1px solid var(--border);">
                ✕
              </button>
            </div>
          </div>
          <div style="font-size:11px;color:var(--txt3);">${u.email}${u.mobile ? ' · 📱 ' + u.mobile : ''} · Joined ${u.created_at ? u.created_at.slice(0,10) : '—'} · Last login ${u.last_login ? u.last_login.slice(0,10) : 'never'}</div>
        </div>`;
    }).join('');
  } catch(e) {
    body.innerHTML = `<div class="empty" style="padding:16px 0;"><div class="empty-title">Error: ${e.message}</div></div>`;
  }
}

async function adminRenewPlan(userId, planType) {
  if (!planType) return;
  try {
    await jAuth(`${API}/api/admin/users/${userId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plan_type: planType }),
    });
    await loadAdminUsers();
    await loadAdminStats();
  } catch(e) {
    alert('Error renewing plan: ' + e.message);
  }
}

async function adminToggleActive(userId, newActive) {
  try {
    await jAuth(`${API}/api/admin/users/${userId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_active: newActive }),
    });
    await loadAdminUsers();
    await loadAdminStats();
  } catch(e) {
    alert('Error: ' + e.message);
  }
}

async function adminDeleteUser(userId, username) {
  if (!confirm(`Delete user "${username}"? This cannot be undone.`)) return;
  try {
    await jAuth(`${API}/api/admin/users/${userId}`, { method: 'DELETE' });
    await loadAdminUsers();
    await loadAdminStats();
  } catch(e) {
    alert('Error deleting user: ' + e.message);
  }
}


// ══════════════════════════════════════════════════════
// HOME TAB
// ══════════════════════════════════════════════════════

// Cache last execute results for the HOME top-picks display
let _homePicksCache = null;

async function loadHome() {
  const hasToken = !!getToken();
  // Fire requests in parallel — protected endpoints only if logged in
  const [statusRes, regimeRes, mlRes] = await Promise.allSettled([
    j(`${API}/api/status`),
    hasToken ? jAuth(`${API}/api/ml/regime`)      : Promise.resolve({}),
    hasToken ? jAuth(`${API}/api/ml/train/status`): Promise.resolve({}),
  ]);

  // 1. Status / health data
  try {
    const d = statusRes.status === 'fulfilled' ? statusRes.value : {};
    const totalRows = (d.total_db_rows || 0).toLocaleString('en-IN');
    const dbRowsEl = document.getElementById('hm-db-rows');
    if (dbRowsEl) dbRowsEl.textContent = totalRows;
    const ts = d.last_run && d.last_run !== 'Never' ? d.last_run : '—';
    const updatedEl = document.getElementById('hm-updated');
    if (updatedEl) updatedEl.textContent = 'Last sync: ' + ts;

    // Fyers health  (API field is token_valid, not fyers_token_valid)
    const fyersOk = d.token_valid === true;
    const hFyers = document.getElementById('hm-h-fyers');
    hFyers.className = 'health-dot ' + (fyersOk ? 'ok' : 'warn');
    document.getElementById('hm-h-fyers-val').textContent = fyersOk ? 'Token valid' : 'Token expired';

    // Data health  (API field is total_db_rows, not total_rows)
    const dataOk = (d.total_db_rows || 0) > 10000;
    const hData = document.getElementById('hm-h-data');
    hData.className = 'health-dot ' + (dataOk ? 'ok' : 'warn');
    const rowsLabel = (d.total_db_rows || 0).toLocaleString('en-IN');
    document.getElementById('hm-h-data-val').textContent = dataOk ? rowsLabel + ' records' : 'Run a sync first';
  } catch(e) {}

  // 2. Market Pulse (regime + actionable verdict)
  try {
    const r = regimeRes.status === 'fulfilled' ? regimeRes.value : {};
    const regime = r.regime || 'Unknown';
    const vol    = r.volatility_label || 'Normal';

    const regimeColor = { Bull: 'var(--green)', Neutral: 'var(--live)', Bear: 'var(--red)' };
    const volColor    = { High: 'var(--red)',   Normal:  'var(--live)', Low:  'var(--green)' };

    // Regime pill
    const dot = document.getElementById('hm-regime-dot');
    dot.style.background  = regimeColor[regime] || 'var(--txt3)';
    dot.style.boxShadow   = `0 0 8px ${regimeColor[regime] || 'var(--txt3)'}`;
    document.getElementById('hm-regime-txt').textContent = regime + ' Market';

    // Breadth % — plain English
    const breadth = r.breadth_pct || 0;
    document.getElementById('hm-breadth-pct').textContent = breadth + '%';

    // Volatility — colored
    const volEl = document.getElementById('hm-volatility');
    volEl.textContent  = vol;
    volEl.style.color  = volColor[vol] || 'rgba(255,255,255,.85)';

    // One actionable verdict for the trader
    const verdicts = {
      'Bull+Low':      '🟢 Strong calm market — good conditions for swing trades',
      'Bull+Normal':   '🟢 Market is bullish — focus on momentum & breakout setups',
      'Bull+High':     '🟡 Bullish but volatile — size down, use tighter stop losses',
      'Neutral+Low':   '🟡 Quiet market — wait for clear breakout signals before entering',
      'Neutral+Normal':'🟡 Mixed signals — only take high-conviction setups today',
      'Neutral+High':  '🟠 Choppy conditions — reduce position sizes, avoid overtrading',
      'Bear+Low':      '🔴 Weak market — avoid new long positions, protect your capital',
      'Bear+Normal':   '🔴 Bearish conditions — cash is a position, stay defensive',
      'Bear+High':     '🔴 Dangerous market — do not trade, wait for conditions to improve',
    };
    const verdictEl = document.getElementById('hm-verdict');
    if (verdictEl) verdictEl.textContent = verdicts[`${regime}+${vol}`] || '—';
  } catch(e) {}

  // 3. AI Model health (pre-fetched in parallel)
  try {
    const m = mlRes.status === 'fulfilled' ? mlRes.value : {};
    const trained = m.is_trained === true;
    const hModel = document.getElementById('hm-h-model');
    hModel.className = 'health-dot ' + (trained ? 'ok' : 'warn');
    let aucVal = null;
    if (m.meta && m.meta.horizons) {
      const firstH = Object.values(m.meta.horizons)[0];
      if (firstH && firstH.auc_roc != null) aucVal = firstH.auc_roc;
    }
    document.getElementById('hm-h-model-val').textContent = trained
      ? 'AUC ' + (aucVal != null ? (aucVal * 100).toFixed(1) + '%' : '—')
      : 'Not trained';
  } catch(e) {}

  // 4. Show cached top picks if available
  if (_homePicksCache && _homePicksCache.length > 0) {
    renderHomePicks(_homePicksCache);
  }
}

function renderHomePicks(results) {
  const picks = document.getElementById('hm-picks');
  if (!results || !results.length) return;
  const top3 = results.slice(0, 3);
  const rankColors = ['var(--data)',   'var(--green)',  'var(--live)'];
  const rankBgs    = ['var(--data-d)', 'var(--green-d)','var(--live-d)'];

  picks.innerHTML = top3.map((r, i) => {
    const prob    = Math.round((r.buy_probability || 0) * 100);
    const color   = _probColor(r.buy_probability || 0);
    const fmt     = v => v != null ? '₹' + Number(v).toLocaleString('en-IN', {maximumFractionDigits:0}) : '—';
    const rr      = r.risk_reward ? r.risk_reward.toFixed(1) + 'R' : '—';
    const strats  = r.strategies || [];
    const topStrat = strats[0] ? strats[0].replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase()) : '';

    // RR ≥ 2.5 = Swing trade (hold days–weeks), else Short-term (hold 1–3 days)
    const rrNum    = r.risk_reward || 0;
    const tradeType = rrNum >= 2.5 ? 'Swing' : 'Short-term';
    const typeColor = rrNum >= 2.5 ? 'var(--green)' : 'var(--live)';
    const typeBg    = rrNum >= 2.5 ? 'var(--green-d)' : 'var(--live-d)';

    const tradeJson = JSON.stringify(r).replace(/"/g, '&quot;');
    return `<div class="home-pick" style="cursor:pointer;" onclick='openChart("${r.symbol}", ${JSON.stringify({stop_loss:r.stop_loss,target:r.target||r.price_target,buy_probability:r.buy_probability,entry:r.entry||r.current_price})})'>
      <div class="home-pick-rank" style="background:${rankBgs[i]};color:${rankColors[i]};">#${i+1}</div>
      <div class="home-pick-body">
        <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
          <div class="home-pick-sym">${r.symbol}</div>
          <span style="font-size:9px;font-weight:600;padding:2px 7px;background:${typeBg};color:${typeColor};border-radius:4px;">${tradeType}</span>
        </div>
        <div class="home-pick-meta">SL ${fmt(r.stop_loss)} · T ${fmt(r.target || r.price_target)} · RR ${rr}</div>
        ${topStrat ? `<div style="font-size:9px;color:var(--txt3);margin-top:2px;">${topStrat}</div>` : ''}
      </div>
      <div class="home-pick-right">
        <div class="home-pick-prob" style="color:${color};">${prob}%</div>
        <div style="font-size:10px;color:var(--txt3);">Chart →</div>
      </div>
    </div>`;
  }).join('');
}

// ══════════════════════════════════════════════════════
// EXECUTE — AI INSIGHT + ENHANCED TRADE CARDS
// ══════════════════════════════════════════════════════

// Sector map for badges (mirrors strategies/sector_map.py)
const _SECTOR_MAP = {
  TCS:'IT',INFY:'IT',WIPRO:'IT',HCLTECH:'IT',TECHM:'IT',LTIM:'IT',PERSISTENT:'IT',COFORGE:'IT',MPHASIS:'IT',OFSS:'IT',
  HDFCBANK:'Banking',ICICIBANK:'Banking',KOTAKBANK:'Banking',AXISBANK:'Banking',SBIN:'Banking',BANKBARODA:'Banking',PNB:'Banking',CANBK:'Banking',INDUSINDBK:'Banking',FEDERALBNK:'Banking',IDFCFIRSTB:'Banking',BANDHANBNK:'Banking',AUBANK:'Banking',
  BAJFINANCE:'Finance',BAJAJFINSV:'Finance',CHOLAFIN:'Finance',LICHSGFIN:'Finance',MUTHOOTFIN:'Finance',SHRIRAMFIN:'Finance',HDFCAMC:'Finance',ICICIGI:'Finance',HDFCLIFE:'Finance',SBILIFE:'Finance',SBICARD:'Finance',
  RELIANCE:'Energy',ONGC:'Energy',IOC:'Energy',BPCL:'Energy',GAIL:'Energy',POWERGRID:'Energy',NTPC:'Energy',TATAPOWER:'Energy',ADANIGREEN:'Energy',ADANIENSOL:'Energy',ADANIPOWER:'Energy',TORNTPOWER:'Energy',
  HINDUNILVR:'FMCG',ITC:'FMCG',NESTLEIND:'FMCG',BRITANNIA:'FMCG',DABUR:'FMCG',MARICO:'FMCG',GODREJCP:'FMCG',COLPAL:'FMCG',EMAMILTD:'FMCG',TATACONSUM:'FMCG',VBL:'FMCG',
  MARUTI:'Auto','TATAMOTORS':'Auto','M&M':'Auto','BAJAJ-AUTO':'Auto',EICHERMOT:'Auto',HEROMOTOCO:'Auto',TVSMOTORS:'Auto',ASHOKLEY:'Auto',BALKRISIND:'Auto',MOTHERSON:'Auto',BOSCHLTD:'Auto',BHARATFORG:'Auto',
  SUNPHARMA:'Pharma',DRREDDY:'Pharma',CIPLA:'Pharma',DIVISLAB:'Pharma',BIOCON:'Pharma',AUROPHARMA:'Pharma',ALKEM:'Pharma',TORNTPHARM:'Pharma',LUPIN:'Pharma',MAXHEALTH:'Pharma',APOLLOHOSP:'Pharma',FORTIS:'Pharma',
  TATASTEEL:'Metals',JSWSTEEL:'Metals',HINDALCO:'Metals',VEDL:'Metals',COALINDIA:'Metals',NMDC:'Metals',SAIL:'Metals',NATIONALUM:'Metals',HINDCOPPER:'Metals',JSWENERGY:'Metals',
  LT:'Capital Goods',SIEMENS:'Capital Goods',ABB:'Capital Goods',BHEL:'Capital Goods',CUMMINSIND:'Capital Goods',THERMAX:'Capital Goods',HAVELLS:'Capital Goods',CGPOWER:'Capital Goods',BEL:'Capital Goods',HAL:'Capital Goods',IRFC:'Capital Goods',RVNL:'Capital Goods',TIINDIA:'Capital Goods',
  TITAN:'Consumer',VOLTAS:'Consumer',DMART:'Consumer',TRENT:'Consumer',NYKAA:'Consumer',ZOMATO:'Consumer',JUBLFOOD:'Consumer',
  ULTRACEMCO:'Cement',GRASIM:'Cement',AMBUJACEM:'Cement',ACC:'Cement',SHREECEM:'Cement',
  BHARTIARTL:'Telecom',IDEA:'Telecom',
  DLF:'Real Estate',GODREJPROP:'Real Estate',OBEROIRLTY:'Real Estate',PHOENIXLTD:'Real Estate',PRESTIGE:'Real Estate',LODHA:'Real Estate',
  HINDZINC:'Metals',JINDALSTEL:'Metals',
  MAZDOCK:'Capital Goods',SOLARINDS:'Capital Goods',
  PIDILITIND:'Consumer',NAUKRI:'IT',ETERNAL:'Consumer',UNITDSPR:'FMCG',INDHOTEL:'Consumer',
  INDIGO:'Transport',
  JIOFIN:'Finance',BAJAJHLDNG:'Finance',BAJAJHFL:'Finance',LICI:'Finance',PFC:'Finance',RECLTD:'Finance',
  ADANIPORTS:'Infrastructure',ADANIENT:'Infrastructure',
  TVSMOTOR:'Auto',HYUNDAI:'Auto',TMPV:'Auto',
  ENRIN:'Energy',
  ZYDUSLIFE:'Pharma',
};

function _getSector(sym) { return _SECTOR_MAP[sym.toUpperCase()] || 'Others'; }

function _buildAIReason(r, regime) {
  const parts = [];
  const regime_str = regime || 'Neutral';
  if (regime_str === 'Bull')    parts.push('<span class="ai-insight-key">Bull market</span> — lower threshold favours entry');
  if (regime_str === 'Bear')    parts.push('<span class="ai-insight-key">Bear market</span> — elevated threshold, proceed cautiously');
  if (regime_str === 'Neutral') parts.push('<span class="ai-insight-key">Neutral market</span> — selective conditions');
  if (r.strategy_count >= 3)   parts.push(`<span class="ai-insight-key">${r.strategy_count} strategies</span> fired simultaneously (strong confluence)`);
  if (r.trend_return_pct > 5)  parts.push(`positive <span class="ai-insight-key">${r.trend_return_pct}% trend</span> confirms momentum`);
  if (r.expected_return_pct)   parts.push(`AI regressor projects <span class="ai-insight-key">+${r.expected_return_pct}% gain</span> in 5 days`);
  const strats = (r.strategies || []);
  if (strats.includes('golden_rsi') || strats.includes('bollinger_bounce')) parts.push('RSI pullback into oversold zone — classic buy-the-dip');
  if (strats.includes('breakout') || strats.includes('high_52w')) parts.push('price breaking out with volume confirmation');
  return parts.length ? parts.join(' · ') : 'Multi-factor convergence passed AI validation threshold.';
}

// ── Bootstrap progress poller ──────────────────────────
let _bootstrapInterval = null;

async function checkBootstrap() {
  try {
    const d = await j(`${API}/api/bootstrap/status`);
    const banner = document.getElementById('bootstrap-banner');
    if (!banner) return;

    if (!d.running && !d.done && d.step === 'idle') {
      banner.style.display = 'none';
      return;
    }

    if (d.done && d.step === 'complete') {
      banner.style.display = 'flex';
      banner.style.background = 'linear-gradient(135deg,#1a3a1e,#0f2a13)';
      document.getElementById('bootstrap-step-label').textContent = '✅ Setup complete — data loaded & model trained!';
      document.getElementById('bootstrap-progress-bar').style.width = '100%';
      document.getElementById('bootstrap-progress-detail').textContent =
        `Completed at ${d.completed_at}`;
      if (_bootstrapInterval) { clearInterval(_bootstrapInterval); _bootstrapInterval = null; }
      setTimeout(() => { banner.style.display = 'none'; refreshStatus(); loadHome(); }, 8000);
      return;
    }

    if (d.step === 'error') {
      banner.style.display = 'flex';
      banner.style.background = 'linear-gradient(135deg,#3a1a1a,#2a0f0f)';
      document.getElementById('bootstrap-step-label').textContent = '❌ Setup error: ' + d.error;
      document.getElementById('bootstrap-progress-bar').style.background = '#f87171';
      if (_bootstrapInterval) { clearInterval(_bootstrapInterval); _bootstrapInterval = null; }
      return;
    }

    // Running
    banner.style.display = 'flex';
    const stepLabels = {
      checking: 'Checking database...',
      fetching: 'Fetching 30 years of historical data (this may take 20–40 minutes)...',
      training: 'Training AI model on full dataset...',
    };
    document.getElementById('bootstrap-step-label').textContent =
      stepLabels[d.step] || d.step;

    const p = d.progress || {};
    if (d.step === 'fetching' && p.total) {
      const pct = Math.round(p.processed / p.total * 100);
      document.getElementById('bootstrap-progress-bar').style.width = (pct * 0.8) + '%';
      document.getElementById('bootstrap-progress-detail').textContent =
        `${p.processed}/${p.total} symbols — ${p.symbol} (${p.candles || 0} candles)`;
    } else if (d.step === 'training') {
      document.getElementById('bootstrap-progress-bar').style.width = '85%';
      document.getElementById('bootstrap-progress-detail').textContent = 'ML training in progress...';
    } else {
      document.getElementById('bootstrap-progress-bar').style.width = '5%';
      document.getElementById('bootstrap-progress-detail').textContent = '';
    }
  } catch(e) {}
}

// Check immediately on load; poll every 10s while running
checkBootstrap();
_bootstrapInterval = setInterval(checkBootstrap, 10000);

// ── Init ───────────────────────────────────────────────
loadHome();   // HOME tab is default active — load it immediately
refreshStatus();
refreshScheduler();
loadIndexOptions();
initSymbolSearch();
// Only poll when the browser tab is visible — save resources when user switches tabs
function _visibleInterval(fn, ms) {
  return setInterval(() => { if (!document.hidden) fn(); }, ms);
}
_visibleInterval(refreshStatus, 30000);       // was 5s — status is cached 60s server-side
_visibleInterval(refreshScheduler, 30000);    // was 15s
_visibleInterval(() => { if (document.getElementById('tab-live').classList.contains('active')) refreshLive(); }, 3000);
checkMLStatus();
