/* Simurgh panel — the whole UI.  No framework, no CDN, no build step: this file
   plus app.css are served by simurgh/panel.py itself, which is also why the
   panel works on a filtered server that cannot reach any package registry.

   The design is the bird: the map is a Simurgh with the Iranian relay on one
   wing and the foreign exit on the other, the tunnel between them is the body,
   and every user port hangs off the tail as a feather.  Traffic lights the
   feathers up; a quota turns them red before the operator hears about it from
   a customer. */
'use strict';

/* the shell hands the UI its data in a JSON data block (not an inline script):
   with a strict Content-Security-Policy there is no other way, and it keeps the
   page free of eval-shaped surprises */
const META = (() => {
  try { return JSON.parse(document.getElementById('panel-meta').textContent); }
  catch (err) { return { i18n: { en: {}, fa: {} }, role: 'relay' }; }
})();
const I18N = META.i18n || { en: {}, fa: {} };
const ROLE = META.role || 'relay';
const PRODUCT = META.product || 'Simurgh Tunnel';
const VERSION = META.version || '';
const THEME_KEY = 'simurgh.theme';
const VIEW_KEY = 'simurgh.view';

// Site storage can be disabled by browser policy even though the panel itself
// is reachable.  Preferences are optional; a SecurityError must never abort
// the entire script before the dashboard has a chance to load.
const storage = {
  get(key, fallback = null) {
    try {
      const value = localStorage.getItem(key);
      return value === null ? fallback : value;
    } catch (err) { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch (err) { /* optional */ }
  },
};

let LANG = storage.get('simurgh.lang');
if (LANG !== 'en' && LANG !== 'fa') {
  LANG = (navigator.language || '').toLowerCase().startsWith('fa') ? 'fa' : 'en';
}
let data = null;                 // the last /api/layout payload
let view = storage.get(VIEW_KEY, 'dashboard');
const sparks = {};               // per-mapping client-side throughput history
const speedRuns = [];            // this session's speed tests
let logsTimer = null, pollTimer = null, busy = 0;

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
const t = (key, vars) => {
  let s = (I18N[LANG] && I18N[LANG][key]) || (I18N.en && I18N.en[key]) || key;
  if (vars) for (const k in vars) s = s.split('{' + k + '}').join(vars[k]);
  return s;
};

/* ------------------------------------------------------------------ format */
function bytes(v) {
  v = Number(v) || 0;
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let i = 0;
  while (Math.abs(v) >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + ' ' + units[i];
}
function bitrate(v) {                     // bytes/s -> a human rate
  v = Number(v) || 0;
  if (v < 1024) return v.toFixed(0) + ' B/s';
  if (v < 1024 * 1024) return (v / 1024).toFixed(0) + ' KiB/s';
  if (v < 1024 * 1024 * 1024) return (v / 1048576).toFixed(1) + ' MiB/s';
  return (v / 1073741824).toFixed(2) + ' GiB/s';
}
function mbps(v) { return ((Number(v) || 0) * 8 / 1e6).toFixed(1); }
function duration(seconds) {
  seconds = Math.floor(Number(seconds) || 0);
  if (seconds < 60) return seconds + 's';
  const d = Math.floor(seconds / 86400), h = Math.floor((seconds % 86400) / 3600),
        m = Math.floor((seconds % 3600) / 60);
  if (d) return t('dur_days', { n: d }) + ' ' + t('dur_hours', { n: h });
  if (h) return t('dur_hours', { n: h }) + ' ' + t('dur_minutes', { n: m });
  return t('dur_minutes', { n: m });
}
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ------------------------------------------------------------------- http */
async function api(path, opts) {
  let res;
  try {
    res = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {}));
  } catch (e) {
    return { ok: false, error: 'the panel did not answer' };
  }
  let body = null;
  try { body = await res.json(); } catch (e) { body = null; }
  if (!body || typeof body !== 'object') body = { ok: false, error: 'bad response' };
  if (res.status === 401) { location.href = '/'; }
  return body;
}
function toast(message, kind) {
  const box = $('toasts');
  const node = el('div', 'toast' + (kind ? ' ' + kind : ''), message);
  box.appendChild(node);
  setTimeout(() => node.remove(), kind === 'err' ? 9000 : 4200);
}
async function action(name, extra) {
  busy++;
  try {
    const body = await api('/api/action', { method: 'POST', body: JSON.stringify(Object.assign({ name }, extra || {})) });
    if (!body.ok) { toast(t('toast_error', { msg: body.error || '' }), 'err'); return null; }
    return body;
  } finally { busy--; }
}

/* ------------------------------------------------------------------ theme */
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme === 'dawn' ? 'dawn' : 'night');
  const btn = $('theme-toggle');
  if (btn) btn.textContent = theme === 'dawn' ? '🌙' : '☀';
}
function toggleTheme() {
  const next = document.documentElement.getAttribute('data-theme') === 'dawn' ? 'night' : 'dawn';
  storage.set(THEME_KEY, next);
  applyTheme(next);
}

/* -------------------------------------------------------------------- i18n */
function applyLang() {
  document.documentElement.lang = LANG;
  document.documentElement.dir = LANG === 'fa' ? 'rtl' : 'ltr';
  storage.set('simurgh.lang', LANG);
  document.querySelectorAll('[data-i18n]').forEach((node) => { node.textContent = t(node.dataset.i18n); });
  document.querySelectorAll('[data-i18n-ph]').forEach((node) => { node.placeholder = t(node.dataset.i18nPh); });
  const btn = $('lang-toggle');
  if (btn) btn.textContent = LANG === 'fa' ? 'EN' : 'فا';
  updatePageTitle();
}
const VIEW_TITLES = {
  dashboard: 'title_dashboard', pair: 'title_pair', ports: 'title_ports',
  wizard: 'title_wizard', speed: 'title_speed', certs: 'title_certs',
  logs: 'title_logs', security: 'title_security', settings: 'title_settings',
  tools: 'title_tools',
};
function updatePageTitle() {
  const key = VIEW_TITLES[view] || 'title_dashboard';
  const title = $('page-title');
  if (title) title.textContent = t(key);
  document.title = t(key) + ' · ' + PRODUCT;
}

/* ------------------------------------------------------------------ views */
function show(name) {
  // A stale bookmark or a view saved by an older release must not hide every
  // section.  Fall back to the dashboard whenever the target does not exist.
  if (!document.getElementById('view-' + name)) name = 'dashboard';
  view = name;
  storage.set(VIEW_KEY, name);
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('on', v.id === 'view-' + name));
  document.querySelectorAll('.nav button').forEach((b) => b.setAttribute('aria-current', String(b.dataset.view === name)));
  location.hash = name;
  updatePageTitle();
  if (name === 'logs') loadLogs();
  if (name === 'dashboard') renderAll();
}
function buildNav() {
  document.querySelectorAll('.nav button').forEach((b) => { b.onclick = () => show(b.dataset.view); });
  show(location.hash ? location.hash.slice(1) : view);
}

/* ------------------------------------------------------------------- map */
/* The Simurgh: a wide bird.  Left wing = this server, right wing = the peer,
   the body between them is the tunnel, the tail feathers are the user ports. */
function renderMap() {
  const svg = $('map');
  if (!data) return;
  const W = 760, H = 330;
  const [rateIn, rateOut] = [data.rates.in, data.rates.out];
  const hot = rateIn + rateOut > 2048;
  const stateColor = data.state === 'up' ? 'var(--ok)' : data.state === 'connecting' ? 'var(--warn)' : 'var(--err)';
  const peer = data.peer || {};
  const rows = (data.mappings || []).filter((m) => !m.listener).slice(0, 8);
  const wings = [];

  // wings: feather strokes that thin out towards the tip
  for (let i = 0; i < 5; i++) {
    const len = 150 - i * 16, lift = 26 + i * 12;
    wings.push(`<path class="link${hot ? ' hot' : ''}" d="M300 ${150 + i * 6} C${300 - len * .45} ${150 + i * 6 - lift} ${300 - len * .7} ${120 + i * 14} ${300 - len * .85} ${96 + i * 16}" stroke-width="${4.4 - i * .55}" opacity="${.9 - i * .12}"/>`);
    wings.push(`<path class="link${hot ? ' hot' : ''}" d="M460 ${150 + i * 6} C${460 + len * .45} ${150 + i * 6 - lift} ${460 + len * .7} ${120 + i * 14} ${460 + len * .85} ${96 + i * 16}" stroke-width="${4.4 - i * .55}" opacity="${.9 - i * .12}"/>`);
  }

  const tail = rows.map((m, i) => {
    const x = 300 + (i + 1) * (160 / (rows.length + 1));
    const y = 260 + (i % 2) * 16;
    const load = (m.in_rate + m.out_rate) > 1024 ? ' hot' : '';
    const quota = m.quota && m.quota.limit ? Math.min(1, (m.quota.used || 0) / m.quota.limit) : 0;
    const ring = quota ? `<circle class="ring-bg" cx="${x}" cy="${y}" r="11" stroke-dasharray="${(quota * 69).toFixed(1)} 69"/>` : '';
    return `
      <g class="tail-feather" data-key="${esc(m.key)}" tabindex="0">
        <path class="spur${load}" d="M380 224 C${(380 + x) / 2} 238 ${x} 240 ${x} ${y - 12}"/>
        ${ring}
        <circle cx="${x}" cy="${y}" r="7" fill="var(--surface2)" stroke="var(--gold)" stroke-width="2"/>
        <text class="ip" x="${x}" y="${y + 24}" text-anchor="middle">${m.listen || (m.udp ? 'udp' : '')}</text>
      </g>`;
  }).join('');

  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = `
  <defs>
    <linearGradient id="goldFlow" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="var(--gold)"/><stop offset="1" stop-color="var(--gold2)"/>
    </linearGradient>
    <radialGradient id="halo" cx=".5" cy=".5" r=".5">
      <stop offset="0" stop-color="var(--gold)" stop-opacity=".22"/>
      <stop offset="1" stop-color="var(--gold)" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <circle cx="380" cy="150" r="132" fill="url(#halo)"/>
  ${wings.join('')}
  <path class="link${hot ? ' hot dash' : ''}" d="M300 170 C340 190 420 190 460 170" stroke-width="8" opacity="${data.state === 'up' ? '1' : '.35'}"/>
  <path class="link" d="M300 150 C345 128 415 128 460 150" stroke-width="3" opacity=".5"/>
  <g>
    <rect class="node-box pulse" x="150" y="44" width="150" height="52" rx="13" opacity="${data.state === 'up' ? '.4' : '.9'}"/>
    <text class="node-title" x="225" y="66" text-anchor="middle">${esc(data.name || ROLE)}</text>
    <text class="node-sub" x="225" y="84" text-anchor="middle">${ROLE === 'relay' ? 'IRAN · relay' : 'FOREIGN · exit'}</text>
  </g>
  <g>
    <rect class="node-box" x="460" y="44" width="160" height="52" rx="13"/>
    <text class="node-title" x="540" y="66" text-anchor="middle">${esc(peer.name || peer.role || 'peer')}</text>
    <text class="node-sub" x="540" y="84" text-anchor="middle">${esc((peer.endpoint || '—').replace('://', '://'))}</text>
  </g>
  <text class="ip" x="380" y="132" text-anchor="middle">${data.state === 'up' ? '══ tunnel ══' : (data.state === 'connecting' ? '... dialling ...' : '✕ down ✕')}</text>
  <circle cx="380" cy="150" r="26" fill="var(--surface2)" stroke="${stateColor}" stroke-width="3"/>
  <text x="380" y="155" text-anchor="middle" font-size="13" font-weight="700" fill="${stateColor}">${data.state === 'up' ? 'UP' : data.state === 'connecting' ? '…' : 'DOWN'}</text>
  <text class="ip" x="380" y="214" text-anchor="middle">${bitrate(rateIn)} ↓ · ${bitrate(rateOut)} ↑</text>
  ${tail}
  <text class="node-state" x="380" y="320" text-anchor="middle" fill="${stateColor}">${data.last_error ? esc(String(data.last_error).slice(0, 80)) : (peer.rtt_ms != null ? data.rtt_ms + ' ms' : '')}</text>`;

  svg.querySelectorAll('.tail-feather').forEach((g) => {
    const open = () => { show('ports'); const row = document.querySelector(`[data-row="${CSS.escape(g.dataset.key)}"]`); if (row) { row.scrollIntoView({ block: 'center' }); row.classList.add('pill', 'hot'); setTimeout(() => row.classList.remove('pill', 'hot'), 1600); } };
    g.addEventListener('click', open);
    g.addEventListener('keydown', (e) => { if (e.key === 'Enter') open(); });
  });
}

/* ----------------------------------------------------------------- chart */
function renderChart() {
  const canvas = $('chart');
  if (!canvas || !data) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 640, h = 172;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const series = data.chart || { t: [], in: [], out: [] };
  const style = getComputedStyle(document.documentElement);
  const grid = style.getPropertyValue('--line') || '#232a45';
  ctx.strokeStyle = grid; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = 8 + (h - 26) * i / 4;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
  const all = series.in.concat(series.out);
  const peak = Math.max(1024, ...all);
  const n = series.in.length;
  if (n < 2) { ctx.fillStyle = style.getPropertyValue('--dim'); ctx.font = '13px system-ui'; ctx.fillText(t('chart_empty'), 10, h / 2); return; }

  const xAt = (i) => (w - 4) * i / (n - 1) + 2;
  const yAt = (v) => 8 + (h - 26) * (1 - Math.min(1, v / peak));
  const draw = (values, color, fill) => {
    ctx.beginPath(); ctx.moveTo(xAt(0), yAt(values[0]));
    values.forEach((v, i) => ctx.lineTo(xAt(i), yAt(v)));
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke();
    if (fill) {
      ctx.lineTo(xAt(n - 1), h - 18); ctx.lineTo(xAt(0), h - 18); ctx.closePath();
      const grad = ctx.createLinearGradient(0, 0, 0, h);
      grad.addColorStop(0, color + '55'); grad.addColorStop(1, color + '05');
      ctx.fillStyle = grad; ctx.fill();
    }
  };
  draw(series.out, style.getPropertyValue('--out').trim(), true);
  draw(series.in, style.getPropertyValue('--in').trim(), true);
  ctx.fillStyle = style.getPropertyValue('--dim');
  ctx.font = '11px ui-monospace, monospace';
  ctx.fillText(bitrate(peak), 4, h - 4);
  ctx.fillText(t('chart_now'), w - 92, h - 4);
}

function sparkFor(key, inRate, outRate) {
  const s = sparks[key] || (sparks[key] = []);
  s.push(inRate + outRate);
  if (s.length > 48) s.shift();
  const canvas = document.createElement('canvas');
  const dpr = window.devicePixelRatio || 1;
  canvas.width = 72 * dpr; canvas.height = 20 * dpr; canvas.className = 'spark';
  canvas.style.width = '72px'; canvas.style.height = '20px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const peak = Math.max(1024, ...s);
  ctx.beginPath();
  s.forEach((v, i) => { const x = 72 * i / Math.max(1, s.length - 1), y = 19 - 17 * (v / peak); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--gold').trim() || '#e9a83c';
  ctx.lineWidth = 1.6; ctx.stroke();
  return canvas;
}

/* --------------------------------------------------------------- dashboard */
function renderCards() {
  const box = $('cards');
  if (!box || !data) return;
  const peer = data.peer || {};
  const totals = data.totals || {};
  const cards = [
    [t('card_state'), data.state === 'up' ? t('state_up') : data.state === 'connecting' ? t('state_connecting') : t('state_down'), `${t('lbl_peer')}: ${peer.name || peer.endpoint || '—'}`],
    [t('card_rate'), `${bitrate(data.rates.in)} ↓`, `${bitrate(data.rates.out)} ↑ · ${mbps(data.rates.in + data.rates.out)} Mbps`],
    [t('card_rtt'), data.rtt_ms != null ? data.rtt_ms + ' ms' : '—', `${t('lbl_reconnects')}: ${data.reconnects || 0}`],
    [t('card_uptime'), duration(data.uptime), `${t('lbl_conns')}: ${totals.conns || 0}`],
    [t('card_today'), bytes(totals.today), `${t('lbl_month')}: ${bytes(totals.month)}`],
    [t('card_total'), bytes(totals.sum), `${t('lbl_ports')}: ${totals.mappings || 0}`],
  ];
  box.innerHTML = cards.map(([title, big, sub], i) => `
    <div class="card${i === 0 ? ' metric-' + esc(data.state) : ''}"><h2>${esc(title)}</h2>
      <div class="big">${esc(big)}</div>
      <div class="sub">${esc(sub)}</div>
    </div>`).join('');
}

function renderMappingTable() {
  const bodies = ['mapping-body', 'mapping-body-2']
    .map((id) => $(id)).filter(Boolean);
  if (!bodies.length || !data) return;
  const rows = data.mappings || [];
  if (!rows.length) {
    bodies.forEach((body) => {
      body.innerHTML = `<tr><td colspan="7" class="empty">${esc(t('ports_empty'))}</td></tr>`;
    });
    return;
  }
  bodies.forEach((body) => { body.innerHTML = ''; });
  rows.forEach((m) => {
    // Build one row per table. cloneNode(true) copies markup but not onclick
    // handlers, which used to leave every visible toggle/delete button dead.
    bodies.forEach((body) => {
      const tr = el('tr');
      tr.dataset.row = m.key;
      const quota = m.quota && m.quota.limit;
      const pct = quota ? Math.min(100, ((m.quota.used || 0) / m.quota.limit) * 100) : 0;
      tr.innerHTML = `
        <td>${esc(m.name || '—')}${m.listener ? ' <span class="pill">' + esc(t('lbl_listener')) + '</span>' : ''}</td>
        <td class="mono">${m.listener ? esc(m.target) : (m.listen + ' → ' + esc(m.target))}</td>
        <td>${m.udp ? 'UDP' : 'TCP'}</td>
        <td class="num">${bitrate(m.in_rate + m.out_rate)}</td>
        <td class="num">${bytes(m.in_bytes + m.out_bytes)}</td>
        <td>${quota ? `<div class="qbar${pct > 85 ? ' hot' : ''}"><i style="width:${pct.toFixed(1)}%"></i></div><div class="sub">${bytes(m.quota.used)} / ${bytes(m.quota.limit)} (${esc(m.quota.period)})</div>` : esc(t('lbl_no_quota'))}</td>
        <td class="num"></td>`;
      const last = tr.lastElementChild;
      last.appendChild(sparkFor(m.key, m.in_rate, m.out_rate));
      if (!m.listener) {
        const tog = el('button', 'sm ghost', m.enabled ? t('btn_turn_off') : t('btn_turn_on'));
        tog.onclick = () => mappingAction({ action: 'toggle', key: m.key });
        const del = el('button', 'sm danger', t('btn_delete'));
        del.onclick = () => { if (confirm(t('confirm_delete'))) mappingAction({ action: 'remove', key: m.key }); };
        const wrap = el('div', 'row');
        wrap.style.justifyContent = 'flex-end';
        wrap.append(tog, del);
        last.appendChild(wrap);
      }
      body.appendChild(tr);
    });
  });
}

function renderStateBadge() {
  const badge = $('badge-state');
  if (!badge || !data) return;
  const key = data.state === 'up' ? 'state_up'
    : data.state === 'connecting' ? 'state_connecting' : 'state_down';
  badge.textContent = t(key);
  badge.classList.remove('ok', 'err', 'warn', 'on', 'off');
  badge.classList.add(data.state === 'up' ? 'ok' : data.state === 'connecting' ? 'warn' : 'err');
}

function renderAll() {
  if (!data) return;
  renderStateBadge();
  renderPairing();
  renderMap();
  renderCards();
  renderChart();
  renderMappingTable();
}

/* ------------------------------------------------------------------ ports */
async function mappingAction(payload) {
  const res = await api('/api/mappings', { method: 'POST', body: JSON.stringify(payload) });
  toast(res.ok ? t('toast_done') : t('toast_error', { msg: res.error || '' }), res.ok ? '' : 'err');
  if (res.port_conflicts && res.port_conflicts.length) toast(t('toast_conflict', { list: res.port_conflicts.join(', ') }), 'warn');
  await load();
}
function bindPorts() {
  $('add-mapping').onclick = () => {
    const listen = parseInt($('m_listen').value || '0', 10);
    if (!listen) { toast(t('toast_need_listen'), 'warn'); return; }
    mappingAction({
      action: 'add', listen,
      target_port: parseInt($('m_target').value || String(listen), 10),
      name: $('m_name').value, udp: $('m_udp').value === 'yes',
    });
    $('m_listen').value = ''; $('m_target').value = ''; $('m_name').value = '';
  };
}

/* ------------------------------------------------------------------ speed */
function renderSpeedList() {
  const box = $('speed-runs');
  if (!box) return;
  if (!speedRuns.length) { box.innerHTML = `<div class="empty">${esc(t('speed_no_runs'))}</div>`; return; }
  box.innerHTML = speedRuns.map((run) => {
    const l = run.latency || {}, u = run.udp || {};
    return `<div class="kv"><span>${new Date(run.at).toLocaleTimeString()}</span>
      <span class="v">${run.download_mbps.toFixed(1)} ↓ / ${run.upload_mbps.toFixed(1)} ↑ Mbps${l.grade ? ' · ' + t('lbl_grade') + ' ' + l.grade : ''}${u.loss_percent != null ? ' · ' + u.loss_percent.toFixed(2) + '% ' + t('lbl_loss') : ''}</span></div>`;
  }).join('');
}
function bindSpeed() {
  $('run-speedtest').onclick = async () => {
    const btn = $('run-speedtest');
    btn.disabled = true; btn.textContent = t('speed_running');
    try {
      const streams = parseInt($('st_streams') ? $('st_streams').value : '1', 10) || 1;
      const seconds = parseInt($('st_seconds') ? $('st_seconds').value : '6', 10) || 6;
      const udp = $('st_udp') ? $('st_udp').checked : false;
      const body = await api('/api/action', { method: 'POST', body: JSON.stringify({ name: 'speedtest', streams, seconds, udp }) });
      if (!body.ok) { toast(t('toast_error', { msg: body.error || '' }), 'err'); return; }
      const r = body.speedtest;
      if (!r) { toast(t('toast_error', { msg: body.error || '' }), 'err'); return; }
      r.at = Date.now();
      speedRuns.unshift(r);
      if (speedRuns.length > 12) speedRuns.pop();
      renderSpeedResult(r);
      renderSpeedList();
    } finally { btn.disabled = false; btn.textContent = t('btn_speedtest'); }
  };
}
function renderSpeedResult(r) {
  const box = $('speed-result');
  if (!box) return;
  const l = r.latency || {}, u = r.udp || {};
  const gradeColor = { A: 'var(--ok)', B: 'var(--ok)', C: 'var(--warn)', D: 'var(--warn)' }[l.grade] || (l.grade ? 'var(--err)' : 'var(--dim)');
  box.innerHTML = `
    <div class="big">${r.download_mbps.toFixed(1)} <span class="unit">Mbps ↓</span></div>
    <div class="sub">${r.upload_mbps.toFixed(1)} Mbps ↑ · ${bytes(r.download_bytes)} / ${bytes(r.upload_bytes)} in ${r.seconds}s · ${r.streams} stream(s)</div>
    <div class="sep"></div>
    <div class="kv"><span>${esc(t('lbl_latency_idle'))}</span><span class="v">${l.idle_ms != null ? l.idle_ms.toFixed(1) + ' ms' : '—'}</span></div>
    <div class="kv"><span>${esc(t('lbl_latency_load'))}</span><span class="v">${l.loaded_ms != null ? l.loaded_ms.toFixed(1) + ' ms' : '—'}</span></div>
    <div class="kv"><span>${esc(t('lbl_jitter'))}</span><span class="v">${l.jitter_ms != null ? l.jitter_ms.toFixed(1) + ' ms' : '—'}</span></div>
    <div class="kv"><span>${esc(t('lbl_grade'))} (bufferbloat)</span><span class="v" style="color:${gradeColor};font-weight:700">${l.grade || '—'}</span></div>
    ${u.loss_percent != null ? `<div class="kv"><span>${esc(t('lbl_udp'))}</span><span class="v">${u.received}/${u.sent} · ${u.loss_percent.toFixed(2)}% ${t('lbl_loss')} · ${(u.rtt_ms || 0).toFixed(1)} ms</span></div>` : ''}`;
}

/* ------------------------------------------------------------------- logs */
async function loadLogs() {
  const pre = $('logs');
  if (!pre) return;
  const lines = ($('log-lines') && $('log-lines').value) || '200';
  const filter = ($('log-filter') && $('log-filter').value || '').toLowerCase();
  const body = await api('/api/logs?lines=' + encodeURIComponent(lines));
  let out = body.lines || [];
  if (filter) out = out.filter((line) => line.toLowerCase().includes(filter));
  const near = pre.scrollTop + pre.clientHeight > pre.scrollHeight - 40;
  pre.textContent = out.join('\n') || t('logs_empty');
  if (near) pre.scrollTop = pre.scrollHeight;
}
function bindLogs() {
  if ($('log-refresh')) $('log-refresh').onclick = loadLogs;
  if ($('log-lines')) $('log-lines').onchange = loadLogs;
  if ($('log-filter')) $('log-filter').oninput = loadLogs;
  if ($('log-auto')) $('log-auto').onchange = () => {
    if (logsTimer) { clearInterval(logsTimer); logsTimer = null; }
    if ($('log-auto').checked) logsTimer = setInterval(() => { if (view === 'logs' && !document.hidden) loadLogs(); }, 3000);
  };
}

/* --------------------------------------------------------------- settings */
async function loadSettings() {
  const box = $('settings-form');
  if (!box) return;
  const cfg = await api('/api/config');
  if (!cfg.ok) { box.innerHTML = `<div class="empty">${esc(cfg.error || '')}</div>`; return; }
  const rows = [
    ['name', t('lbl_name'), cfg.name || '', 'text'],
    ['log_level', t('lbl_log_level'), cfg.log_level || 'info', 'select:debug,info,warning,error'],
    ['engine', t('lbl_engine'), cfg.engine || 'python', 'select:python,go'],
    ['rate_limit', t('lbl_rate_limit'), cfg.rate_limit || '', 'text'],
    ['quota', t('lbl_quota'), cfg.quota || '', 'text'],
    ['quota_period', t('lbl_quota_period'), cfg.quota_period || 'month', 'select:day,month,total'],
    ['quota_action', t('lbl_quota_action'), cfg.quota_action || 'block', 'select:block,throttle,warn'],
    ['max_conns', t('lbl_max_conns'), cfg.max_conns ?? 0, 'number'],
  ];
  if (ROLE === 'relay') {
    rows.push(['dial', t('lbl_dial'), cfg.dial || 'relay', 'select:relay,exit']);
    rows.push(['tunnel_carrier', t('lbl_reverse_carrier'),
      (cfg.tunnel && cfg.tunnel.carrier) || 'tls', 'select:tls,wss,raw,plain,udp']);
    rows.push(['tunnel_port', t('lbl_reverse_port'),
      (cfg.tunnel && cfg.tunnel.port) || 8443, 'number']);
    rows.push(['tunnel_path', t('lbl_reverse_path'),
      (cfg.tunnel && cfg.tunnel.path) || '/ws', 'text']);
    rows.push(['panel_port', t('lbl_panel_port'), cfg.panel_port || 8787, 'number']);
    rows.push(['keepalive', t('lbl_ping'), cfg.keepalive ?? 25, 'number']);
    rows.push(['stream_window', t('lbl_window'), cfg.stream_window ?? 262144, 'number']);
    rows.push(['max_stream_window', t('lbl_max_window'), cfg.max_stream_window ?? 8388608, 'number']);
    rows.push(['chunk', t('lbl_chunk'), cfg.chunk ?? 65536, 'number']);
    rows.push(['connections', t('lbl_conns_cfg'), cfg.connections ?? 1, 'number']);
  } else {
    rows.push(['stream_window', t('lbl_window'), cfg.stream_window ?? 262144, 'number']);
    rows.push(['max_stream_window', t('lbl_max_window'), cfg.max_stream_window ?? 8388608, 'number']);
    rows.push(['chunk', t('lbl_chunk'), cfg.chunk ?? 65536, 'number']);
    rows.push(['speedtest_port', 'speedtest_port', cfg.speedtest_port ?? 8808, 'number']);
  }
  box.innerHTML = rows.map(([key, label, value, kind]) => {
    if (kind.startsWith('select:')) {
      const options = kind.slice(7).split(',').map((v) => `<option value="${v}" ${String(value) === v ? 'selected' : ''}>${v}</option>`).join('');
      return `<div class="field"><label for="cfg_${key}">${esc(label)}</label><select id="cfg_${key}" data-key="${key}">${options}</select></div>`;
    }
    return `<div class="field"><label for="cfg_${key}">${esc(label)}</label><input id="cfg_${key}" data-key="${key}" type="${kind}" value="${esc(value)}"></div>`;
  }).join('');
  $('save-settings').onclick = async () => {
    const payload = {};
    box.querySelectorAll('[data-key]').forEach((node) => {
      const raw = node.value;
      payload[node.dataset.key] = node.type === 'number' ? Number(raw) : raw;
    });
    const res = await api('/api/config', { method: 'POST', body: JSON.stringify(payload) });
    toast(res.ok ? t('toast_saved') : t('toast_error', { msg: res.error || '' }), res.ok ? '' : 'err');
    await load();
  };
  const tokenBox = $('token-box');
  if (tokenBox) tokenBox.textContent = cfg.token_masked || '—';
  const engineBox = $('engine-box');
  if (engineBox && data) engineBox.textContent = data.engine || '—';
}
function bindTune() {
  document.querySelectorAll('[data-tune]').forEach((btn) => {
    btn.onclick = async () => {
      const res = await api('/api/tune', { method: 'POST', body: JSON.stringify({ profile: btn.dataset.tune }) });
      if (!res.ok) { toast(t('toast_error', { msg: res.error || '' }), 'err'); return; }
      const n = (res.changes || []).length;
      toast(n ? t('toast_tuned', { n, profile: btn.dataset.tune }) : t('toast_tuned_same', { profile: btn.dataset.tune }));
      loadSettings();
    };
  });
}

/* ----------------------------------------------------------- security tab */
function signOut() {
  // Stop polling before the server revokes this session; otherwise an in-flight
  // layout request can race the redirect and produce a misleading error toast.
  if (pollTimer) clearInterval(pollTimer);
  if (logsTimer) clearInterval(logsTimer);
  location.href = '/logout?lang=' + encodeURIComponent(LANG);
}
async function loadSecurity() {
  const box = $('security-info');
  if (!box || !data) return;
  const p = data.panel || {};
  box.innerHTML = `
    <div class="kv"><span>${esc(t('lbl_panel_port'))}</span><span class="v mono">${p.port || '—'}</span></div>
    <div class="kv"><span>${esc(t('lbl_panel_user'))}</span><span class="v mono">${esc(p.user || '')}</span></div>
    <div class="kv"><span>${esc(t('lbl_requests'))}</span><span class="v">${p.requests || 0}</span></div>
    <div class="kv"><span>${esc(t('lbl_lockouts'))}</span><span class="v">${p.lockouts || 0}</span></div>
    <div class="kv"><span>${esc(t('lbl_failed'))}</span><span class="v">${p.failed_logins || 0}</span></div>`;
}
function bindSecurity() {
  if ($('logout')) $('logout').onclick = signOut;
  if ($('logout-top')) $('logout-top').onclick = signOut;
  if (!$('make-link')) return;
  $('make-link').onclick = async () => {
    const ttl = parseInt($('link-ttl').value || '600', 10);
    const res = await api('/api/login-token', { method: 'POST', body: JSON.stringify({ ttl }) });
    if (!res.ok) { toast(t('toast_error', { msg: res.error || '' }), 'err'); return; }
    $('link-out').textContent = res.url;
    $('link-out').hidden = false;
    toast(t('toast_link', { mins: Math.round(res.expires_in / 60) }));
    if (navigator.clipboard) navigator.clipboard.writeText(res.url).catch(() => {});
  };
}

/* --------------------------------------------------------------- wizard */
const wizard = { step: 1, plan: {}, backup: '' };
function renderWizard() {
  const box = $('wizard-body');
  if (!box) return;
  document.querySelectorAll('.step').forEach((s) => {
    const n = Number(s.dataset.step);
    s.classList.toggle('on', n === wizard.step);
    s.classList.toggle('done', n < wizard.step);
  });
  if (wizard.step === 1) {
    box.innerHTML = `
      <div class="cols2">
        <div class="field"><label for="wz_carrier">${esc(t('lbl_carrier'))}</label>
          <select id="wz_carrier"><option value="tls">tls</option><option value="wss">wss</option><option value="raw">raw</option><option value="plain">plain</option></select></div>
        <div class="field"><label for="wz_port">${esc(t('lbl_tunnel_port'))}</label>
          <input id="wz_port" type="number" value="443"></div>
        <div class="field"><label for="wz_host">${esc(t('lbl_exit_host'))}</label>
          <input id="wz_host" placeholder="203.0.113.9"></div>
        <div class="field"><label for="wz_path">${esc(t('lbl_path'))}</label>
          <input id="wz_path" value="/ws"></div>
        <div class="field"><label for="wz_split">${esc(t('lbl_split'))}</label>
          <input id="wz_split" type="number" value="3"></div>
        <div class="field"><label for="wz_mapping">${esc(t('lbl_first_mapping'))}</label>
          <input id="wz_mapping" placeholder="8443:8443"></div>
      </div>
      <div class="row" style="margin-top:14px"><button class="primary" id="wz_next">${esc(t('btn_continue'))}</button></div>`;
    $('wz_next').onclick = () => {
      wizard.plan = {
        carrier: $('wz_carrier').value, port: parseInt($('wz_port').value, 10) || 443,
        host: $('wz_host').value.trim(), path: $('wz_path').value.trim() || '/ws',
        split: parseInt($('wz_split').value, 10) || 0, mapping: $('wz_mapping').value.trim(),
      };
      wizard.step = 2; renderWizard();
    };
  } else if (wizard.step === 2) {
    box.innerHTML = `<pre class="logs" id="wz_preview">${esc(t('wz_checking'))}</pre>
      <div class="row" style="margin-top:14px">
        <button class="ghost" id="wz_back">${esc(t('btn_back'))}</button>
        <button class="primary" id="wz_apply">${esc(t('btn_apply'))}</button></div>`;
    $('wz_back').onclick = () => { wizard.step = 1; renderWizard(); };
    $('wz_apply').onclick = async () => {
      const res = await api('/api/wizard', { method: 'POST', body: JSON.stringify({ step: 'apply', plan: wizard.plan }) });
      if (!res.ok) { toast(t('toast_error', { msg: res.error || '' }), 'err'); return; }
      wizard.backup = res.backup || '';
      $('wz_preview').textContent = (res.changes || []).map((c) => '• ' + c).join('\n') + '\n\n' + t('wz_applied');
      waitForState(res.state || 'up').then((ok) => {
        wizard.checked = ok;
        if (!ok) toast(t('wz_probe_failed'), 'err');
      });
      wizard.step = 3; renderWizard();
    };
    api('/api/wizard', { method: 'POST', body: JSON.stringify({ step: 'preview', plan: wizard.plan }) })
      .then((res) => {
        const pre = $('wz_preview');
        if (!pre) return;
        pre.textContent = res.ok ? ((res.changes || []).map((c) => '• ' + c).join('\n') || t('wz_no_changes')) : t('wz_invalid', { msg: res.error || '' });
        if (!res.ok) { $('wz_apply').disabled = true; toast(t('wz_invalid', { msg: res.error || '' }), 'err'); }
      });
  } else if (wizard.step === 3) {
    box.innerHTML = `<pre class="logs">${esc(t('wz_after_apply'))}</pre>
      <div class="row" style="margin-top:14px">
        <button class="ghost" id="wz_rollback">${esc(t('btn_rollback'))}</button>
        <button class="primary" id="wz_done">${esc(t('btn_finish'))}</button></div>`;
    $('wz_rollback').onclick = async () => {
      const res = await api('/api/wizard', { method: 'POST', body: JSON.stringify({ step: 'rollback', backup: wizard.backup }) });
      toast(res.ok ? t('toast_rolled_back') : t('toast_error', { msg: res.error || '' }), res.ok ? '' : 'err');
      wizard.step = 1; renderWizard(); await load();
    };
    $('wz_done').onclick = () => { wizard.step = 1; renderWizard(); show('dashboard'); };
  }
}
async function waitForState(want, seconds) {
  const deadline = Date.now() + (seconds || 20) * 1000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 1500));
    await load(true);
    if (data && data.state === want) return true;
  }
  return false;
}

/* ---------------------------------------------------------------- tools */
async function waitForUpdatedPanel(expectedVersion) {
  // The restart response is sent before systemd stops the process.  Poll the
  // unauthenticated liveness endpoint rather than mistaking a closed fetch for
  // a failed update.  The old process can also remain healthy after a failed
  // restart, so require the newly installed version before reloading.
  await new Promise((r) => setTimeout(r, 1800));
  for (let attempt = 0; attempt < 40; attempt++) {
    try {
      const res = await fetch('/healthz', { cache: 'no-store' });
      if (res.ok) {
        const body = await res.json();
        if (body && body.ok && body.version === expectedVersion) {
          location.reload();
          return true;
        }
      }
    } catch (e) { /* the expected restart window */ }
    await new Promise((r) => setTimeout(r, 1000));
  }
  toast(t('toast_update_restart_failed'), 'err');
  return false;
}

function bindTools() {
  document.querySelectorAll('[data-act]').forEach((btn) => {
    btn.onclick = async () => {
      const name = btn.dataset.act;
      if (name === 'update-check') {
        const res = await api('/api/update', { method: 'GET' });
        toast(res.ok ? (res.update_available ? t('toast_update_available', { version: res.latest || '' }) : t('toast_up_to_date', { version: res.version || VERSION })) : t('toast_error', { msg: res.error || '' }), res.ok && !res.update_available ? '' : (res.ok ? 'warn' : 'err'));
        return;
      }
      if (name === 'update-install') {
        if (!confirm(t('confirm_update'))) return;
        btn.disabled = true;
        toast(t('toast_update_installing'), 'warn');
        try {
          const res = await api('/api/update', { method: 'POST', body: '{}' });
          if (!res.ok) { toast(t('toast_error', { msg: res.error || '' }), 'err'); return; }
          if (res.restarting) {
            toast(t('toast_update_restarting'), 'warn');
            await waitForUpdatedPanel(res.latest);
          } else if (res.restart_required) {
            toast(t('toast_update_restart_required'), 'warn');
          } else {
            toast(t('toast_up_to_date', { version: res.version || VERSION }));
          }
        } finally { btn.disabled = false; }
        return;
      }
      if (name === 'export') {
        const cfg = await api('/api/config');
        const blob = new Blob([JSON.stringify(cfg, null, 2)], { type: 'application/json' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'simurgh-' + ROLE + '-config.json';
        a.click(); URL.revokeObjectURL(a.href);
        toast(t('toast_exported'));
        return;
      }
      if (name === 'logs-download') {
        const body = await api('/api/logs?lines=2000');
        const blob = new Blob([(body.lines || []).join('\n')], { type: 'text/plain' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'simurgh-' + ROLE + '.log';
        a.click(); URL.revokeObjectURL(a.href);
        return;
      }
      const labels = { restart: 'toast_restart', stop: 'toast_stop', start: 'toast_start', reconnect: 'toast_reconnect' };
      const asks = { restart: 'confirm_restart', stop: 'confirm_stop' };
      if (asks[name] && !confirm(t(asks[name]))) return;
      const res = await action(name);
      if (res && labels[name]) toast(t(labels[name]));
    };
  });
}

/* ---------------------------------------------------------------- pairing */
/* A relay can be installed before its exit exists (`--pair-later`): the panel
   then shows one page whose only job is "paste the link the exit printed".
   Pairing keeps every choice this install already made — only the secret and
   the exit's address arrive from the link. */
function pairingRequired() {
  return !!(data && data.panel && data.panel.pairing_required);
}
function renderPairing() {
  const unpaired = pairingRequired();
  const panel = data && data.panel || {};
  const offer = ROLE === 'exit' || !!panel.setup_link;
  const banner = $('pair-banner'), nav = $('nav-pair');
  if (banner) banner.hidden = !unpaired;
  if (nav) nav.hidden = false;
  const form = $('pair-form'), done = $('pair-done'), offerBox = $('pair-offer');
  const exitForm = $('exit-pair-form');
  if (offerBox) offerBox.hidden = !offer;
  if (form) form.hidden = ROLE === 'exit' || !!panel.reverse;
  if (exitForm) exitForm.hidden = ROLE !== 'exit';
  if (done && (unpaired || ROLE === 'exit')) done.hidden = true;
  const replacing = ROLE === 'relay' && !!panel.paired && !panel.reverse;
  if ($('pair-title')) $('pair-title').textContent = t(replacing ? 'pair_replace_title' : 'pair_title');
  if ($('pair-lead')) $('pair-lead').textContent = t(replacing ? 'pair_replace_lead' : 'pair_lead');
  if ($('pair-steps')) $('pair-steps').hidden = replacing;
  if ($('pair-btn')) $('pair-btn').textContent = t(replacing ? 'pair_replace_btn' : 'pair_btn');
}
function bindPair() {
  const btn = $('pair-btn');
  if (btn) {
    btn.onclick = async () => {
      const field = $('pair-link');
      const link = (field ? field.value : '').trim();
      if (!link) { toast(t('pair_need_link'), 'warn'); return; }
      const replacing = !!(data && data.panel && data.panel.paired);
      if (replacing && !confirm(t('confirm_replace_pair'))) return;
      btn.disabled = true;
      const status = $('pair-status');
      if (status) status.textContent = '…';
      const res = await api('/api/pair', {
        method: 'POST', body: JSON.stringify({ link, replace: replacing }),
      });
      btn.disabled = false;
      if (status) status.textContent = '';
      if (!res.ok) {
        if (status) status.textContent = res.error || '';
        toast(t('toast_error', { msg: res.error || '' }), 'err');
        return;
      }
      if (field) field.value = '';
      if ($('pair-exit')) $('pair-exit').textContent = res.exit || '—';
      if ($('pair-ports')) $('pair-ports').textContent = (res.mappings || []).join(', ') || '—';
      if ($('pair-form')) $('pair-form').hidden = true;
      if ($('pair-done')) $('pair-done').hidden = false;
      toast(t('pair_ok'));
      await load(true);
      setTimeout(() => show('dashboard'), 1200);
    };
  }
  const open = $('pair-open');
  if (open) open.onclick = () => show('pair');
  const exitBtn = $('exit-pair-btn');
  if (exitBtn) exitBtn.onclick = async () => {
    const field = $('exit-pair-link');
    const link = (field ? field.value : '').trim();
    if (!link) { toast(t('pair_need_link'), 'warn'); return; }
    exitBtn.disabled = true;
    const status = $('exit-pair-status');
    if (status) status.textContent = '…';
    const res = await api('/api/pair', { method: 'POST', body: JSON.stringify({ link }) });
    exitBtn.disabled = false;
    if (status) status.textContent = res.ok ? (res.note || '') : (res.error || '');
    if (!res.ok) { toast(t('toast_error', { msg: res.error || '' }), 'err'); return; }
    if (field) field.value = '';
    toast(t('pair_saved_restart'));
    await load(true);
  };
  const make = $('setup-link-btn');
  if (make) make.onclick = async () => {
    const host = ($('setup-host') && $('setup-host').value || '').trim();
    make.disabled = true;
    const res = await api('/api/setup-link', {
      method: 'POST', body: JSON.stringify({ host }),
    });
    make.disabled = false;
    if (!res.ok) { toast(t('toast_error', { msg: res.error || '' }), 'err'); return; }
    const out = $('setup-link-out');
    if (out) { out.textContent = res.url; out.hidden = false; }
    toast(t('setup_created'));
    if (navigator.clipboard) navigator.clipboard.writeText(res.url).catch(() => {});
  };
}

/* ---------------------------------------------------------- certificates */
function renderCerts() {
  const body = $('certs-body');
  if (!body || !data) return;
  const rows = data.certificates || [];
  if (!rows.length) { body.innerHTML = `<tr><td colspan="5" class="empty">${esc(t('cert_none'))}</td></tr>`; return; }
  body.innerHTML = rows.map((c) => {
    const days = c.days == null ? '—' : c.days.toFixed(0);
    const warn = c.days != null && c.days <= 14;
    return `<tr>
      <td class="mono">${esc(c.endpoint)}</td>
      <td>${c.exists ? `<span class="pill ${warn ? 'hot' : 'on'}">${days} ${esc(t('cert_days'))}</span>` : `<span class="pill off">${esc(t('cert_missing'))}</span>`}</td>
      <td class="mono">${esc((c.fingerprint || '').slice(0, 24) || '—')}</td>
      <td class="mono">${esc(c.cert_file || '—')}</td>
      <td>${esc(c.note || '')}</td></tr>`;
  }).join('');
}

/* ------------------------------------------------------------------- poll */
async function load(quiet) {
  const body = await api('/api/layout');
  if (!body || body.ok === false) { if (!quiet) toast(t('toast_error', { msg: (body && body.error) || '' }), 'err'); return; }
  data = body;
  renderPairing();
  if (view === 'dashboard') renderAll();
  if (view === 'ports') renderMappingTable();
  if (view === 'certs') renderCerts();
  if (view === 'security') loadSecurity();
}

/* ------------------------------------------------------------------- boot */
function boot() {
  applyTheme(storage.get(THEME_KEY, 'night'));
  applyLang();
  buildNav();
  bindPorts(); bindLogs(); bindSpeed(); bindTune(); bindSecurity(); bindTools(); bindPair();
  if ($('lang-toggle')) $('lang-toggle').onclick = () => {
    LANG = LANG === 'fa' ? 'en' : 'fa'; applyLang(); renderAll(); renderCerts();
    loadSettings(); loadLogs(); renderPairing();
  };
  if ($('theme-toggle')) $('theme-toggle').onclick = toggleTheme;
  document.querySelectorAll('.nav button').forEach((b) => b.addEventListener('click', () => {
    if (b.dataset.view === 'settings') loadSettings();
    if (b.dataset.view === 'security') loadSecurity();
    if (b.dataset.view === 'certs') renderCerts();
  }));
  renderWizard();
  load();
  loadSettings();
  pollTimer = setInterval(() => { if (!document.hidden) load(true); }, 2000);
  if ($('log-auto') && $('log-auto').checked) logsTimer = setInterval(() => { if (view === 'logs' && !document.hidden) loadLogs(); }, 3000);
}
document.addEventListener('DOMContentLoaded', boot);
