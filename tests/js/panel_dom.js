/* Drive the panel's script against a stubbed DOM and canned API answers.
 *
 * The web panel is one Python file and one JavaScript file that must agree with
 * each other; the Python half has tests, and this is the JavaScript half's.  It
 * is deliberately not a browser: it catches the failures that actually happened
 * while the panel was written -- a `$('typo')` that is null, a view that throws
 * when it opens, a button with no handler, a payload the code did not expect.
 *
 * Usage: node tests/js/panel_dom.js <payload.json>
 * It prints what it drew and exits non-zero on the first exception or on a view
 * that stayed empty.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..', '..');
const html = fs.readFileSync(path.join(ROOT, 'simurgh/assets/index.html'), 'utf8');
const script = fs.readFileSync(path.join(ROOT, 'simurgh/assets/app.js'), 'utf8');
const payloadPath = process.argv[2];
if (!payloadPath) {
  console.error('usage: node tests/js/panel_dom.js <payload.json>');
  process.exit(2);
}
const payload = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));
const role = (payload.panel && payload.panel.role) || 'relay';

/* ------------------------------------------------------------- the fake DOM */
const ids = new Set([...html.matchAll(/id="([A-Za-z0-9_-]+)"/g)].map((m) => m[1]));
const nodes = new Map();

function makeNode(tag, id) {
  const gradient = { addColorStop() {} };
  return {
    tagName: (tag || 'div').toUpperCase(),
    id: id || '',
    children: [],
    dataset: {},
    style: {},
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      contains(c) { return this._set.has(c); },
      toggle(c, on) {
        if (on === undefined ? !this._set.has(c) : on) this._set.add(c);
        else this._set.delete(c);
      },
    },
    textContent: '',
    _html: '',
    get innerHTML() { return this._html; },
    set innerHTML(value) {
      this._html = String(value);
      for (const match of this._html.matchAll(/id="([A-Za-z0-9_-]+)"/g)) {
        if (!nodes.has(match[1])) nodes.set(match[1], makeNode('div', match[1]));
      }
      if (this.tagName === 'TR' || this.tagName === 'TBODY') {
        this.children = [...this._html.matchAll(/<(td|th)\b/g)].map(() => makeNode('td'));
      }
    },
    hidden: false,
    value: '',
    checked: true,
    type: 'text',
    disabled: false,
    className: '',
    href: '',
    appendChild(child) { this.children.push(child); return child; },
    append(...kids) { kids.forEach((k) => this.children.push(k)); },
    removeChild(child) { this.children = this.children.filter((c) => c !== child); },
    remove() {},
    setAttribute() {},
    getAttribute() { return ''; },
    addEventListener() {},
    removeEventListener() {},
    click() {},
    focus() {}, blur() {},
    querySelectorAll() { return []; },
    querySelector() { return null; },
    cloneNode() { return makeNode(this.tagName, this.id); },
    getContext() {
      return new Proxy({
        createLinearGradient: () => gradient,
        createRadialGradient: () => gradient,
        measureText: () => ({ width: 10 }),
        getImageData: () => ({ data: [] }),
        createPattern: () => ({}),
      }, {
        get(target, prop) {
          if (prop in target) return target[prop];
          return () => {};                      // every drawing call is a no-op
        },
      });
    },
    getBoundingClientRect() { return { width: 600, height: 220, top: 0, left: 0 }; },
    get firstElementChild() { return this.children[0] || null; },
    get lastElementChild() { return this.children[this.children.length - 1] || null; },
    get offsetWidth() { return 600; },
    get clientWidth() { return 600; },
    get offsetHeight() { return 220; },
  };
}

ids.forEach((id) => nodes.set(id, makeNode('div', id)));
if (nodes.has('panel-meta')) {
  nodes.get('panel-meta').textContent = JSON.stringify({
    i18n: { en: {}, fa: {} }, role, product: 'Simurgh Tunnel', version: '3.2.3',
  });
}
if (nodes.has('log-filter')) nodes.get('log-filter').value = '';
if (nodes.has('link-ttl')) nodes.get('link-ttl').value = '600';

/* the canned panel: every route the script may call, answering like the real one */
const SPEEDTEST = {
  download_mbps: 84.2, upload_mbps: 41.7, streams: 4, seconds: 6, udp: false,
  latency: { idle_ms: 12.4, loaded_ms: 28.9, increase_ms: 16.5, grade: 'A' },
  loss: null,
  history: [{ t: 0, mbps: 40 }, { t: 1, mbps: 84 }],
};
const CONFIG = {
  ok: true, role, name: 'ir-tehran', log_level: 'info', engine: 'python',
  dial: 'relay', tunnel: { carrier: 'tls', port: 8443, path: '/ws' },
  panel_port: 8787, keepalive: 0, stream_window: 262144, max_stream_window: 8388608,
  chunk: 65536, connections: 1, speedtest_port: 8808,
  rate_limit: '20M', quota: '100G', quota_period: 'month', quota_action: 'block',
  max_conns: 100, token_masked: 'DEMO…123',
};
const LOGS = { ok: true, lines: ['2026-10-17 10:00:00 INFO tunnel up',
                                 '2026-10-17 10:00:01 INFO port 8443 -> 127.0.0.1:9000'],
               total: 2 };
const ANSWERS = [
  [/^\/api\/layout/, () => payload],
  [/^\/api\/status/, () => ({ ok: true, node: {}, panel: {} })],
  [/^\/api\/config/, () => CONFIG],
  [/^\/api\/logs/, () => LOGS],
  [/^\/api\/action/, () => ({ ok: true, speedtest: SPEEDTEST, action: 'speedtest' })],
  [/^\/api\/mappings/, () => ({ ok: true, mappings: payload.mappings })],
  [/^\/api\/setup-link/, () => ({ ok: true, target: role === 'exit' ? 'relay' : 'exit',
                                  url: 'simurgh://203.0.113.9:8787/setup?u=admin&p=c2VjcmV0' })],
  [/^\/api\/pair/, () => ({ ok: true, exit: 'tls://198.51.100.7:443', applied: 'live',
                            mappings: [443], added: [443],
                            backup: '/etc/simurgh/relay.toml.bak-pair' })],
  [/^\/api\/tune/, () => ({ ok: true, profile: 'speed', changes: [{ key: 'chunk', old: 1, new: 2 }],
                            note: 'take effect after a restart' })],
  [/^\/api\/wizard/, () => ({ ok: true, changes: ['[exit] carrier tls -> wss'],
                              backup: '/etc/simurgh/relay.toml.bak-wizard' })],
  [/^\/api\/login-token/, () => ({ ok: true, url: 'http://127.0.0.1:8787/?t=abc',
                                   expires_in: 600 })],
  [/^\/api\/update/, (init) => init.method === 'POST'
    ? { ok: true, available: true, current: '3.0.0', latest: '3.1.0',
        action: 'updated', restart_required: true, restarting: true }
    : { ok: true, update_available: true, version: '3.0.0',
        latest: '3.1.0', notes_url: 'https://example.invalid' }],
  [/^\/healthz/, () => ({ ok: true, version: '3.1.0' })],
];
const seen = [], bodies = [];

const document = {
  documentElement: makeNode('html'),
  body: makeNode('body'),
  head: makeNode('head'),
  title: '',
  hidden: false,
  _collections: new Map(),
  getElementById(id) { return nodes.get(id) || null; },
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
  querySelectorAll(sel) {
    // the sidebar lives in <nav class="nav">: both selectors are the same list
    if (sel === '.nav button') return this.querySelectorAll('[data-view]');
    if (this._collections.has(sel)) return this._collections.get(sel);
    let out = [];
    if (sel.startsWith('[data-view]')) {
      out = [...html.matchAll(/data-view="([a-z]+)"/g)].map((m) => {
        const node = makeNode('button');
        node.dataset.view = m[1];
        return node;
      });
    } else if (sel.startsWith('[data-')) {
      const attr = sel.slice(1, -1);
      out = [...html.matchAll(new RegExp(attr + '="([^"]+)"', 'g'))].map((m) => {
        const node = makeNode('button');
        const key = attr.replace('data-', '').replace(/-([a-z])/g, (_, c) => c.toUpperCase());
        node.dataset[key] = m[1];
        return node;
      });
    } else if (sel.startsWith('.')) {
      // Preserve the real element's id as well as its class.  View switching
      // compares section ids; anonymous class-only stubs made every section
      // look inactive even when the application had selected the dashboard.
      const className = sel.slice(1);
      const tags = new RegExp('<([a-z][a-z0-9-]*)\\b(?=[^>]*class="[^"]*\\b'
        + className + '\\b[^"]*")[^>]*>', 'g');
      out = [...html.matchAll(tags)].map((match) => {
        const id = (match[0].match(/\bid="([^"]+)"/) || [])[1] || '';
        return (id && nodes.get(id)) || makeNode(match[1], id);
      });
    }
    this._collections.set(sel, out);
    return out;
  },
  created: [],
  createElement(tag) {
    const node = makeNode(tag);
    this.created.push(node);            // the rows' own buttons live here
    return node;
  },
  addEventListener(event, handler) {
    (sandbox.__listeners[event] = sandbox.__listeners[event] || []).push(handler);
  },
};

const localStorage = {
  store: {},
  getItem(key) { return Object.prototype.hasOwnProperty.call(this.store, key) ? this.store[key] : null; },
  setItem(key, value) { this.store[key] = String(value); },
  removeItem(key) { delete this.store[key]; },
};
if (process.env.PANEL_TEST_STORAGE === 'blocked') {
  localStorage.getItem = () => { throw new Error('SecurityError: storage disabled'); };
  localStorage.setItem = () => { throw new Error('SecurityError: storage disabled'); };
}
if (process.env.PANEL_TEST_VIEW === 'removed-view') {
  localStorage.store['simurgh.view'] = 'removed-view';
}

const location = { href: 'http://localhost/', hash: '', protocol: 'http:', host: 'localhost',
                   reload() { this.reloaded = true; } };
const window = { location, addEventListener() {}, matchMedia: () => ({ matches: false }) };

const sandbox = {
  window, document, localStorage, location, console,
  history: { replaceState() {}, pushState() {} },
  navigator: { language: 'en-US', clipboard: null },
  setInterval, clearInterval, setTimeout, clearTimeout, Date, Math, JSON, Promise,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  devicePixelRatio: 1,
  getComputedStyle: () => ({ getPropertyValue: () => '#e9a83c' }),
  confirm: () => true,
  alert: () => {}, prompt: () => null,
  Blob: class { constructor(parts) { this.parts = parts; } },
  URL: { createObjectURL: () => 'blob:stub', revokeObjectURL() {} },
  TextDecoder, TextEncoder,
  Event: class {}, CustomEvent: class {},
  fetch: async (url, init = {}) => {
    const path = String(url);
    seen.push(path);
    if (init.body) bodies.push(JSON.parse(init.body));
    const answer = ANSWERS.find(([pattern]) => pattern.test(path));
    const body = answer ? answer[1](init) : { ok: false, error: `no stub for ${path}` };
    return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
  },
  __listeners: {},
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;

const drawn = () => [...nodes.entries()]
  .filter(([, node]) => (node.innerHTML && node.innerHTML.length > 20) || node.children.length)
  .map(([id]) => id);

const problems = [];
try {
  vm.createContext(sandbox);
  vm.runInContext(script, sandbox, { filename: 'app.js' });
} catch (err) {
  console.error('the script threw while loading:', err.stack);
  process.exit(1);
}

(async () => {
  for (const handler of sandbox.__listeners.DOMContentLoaded || []) handler();
  await new Promise((r) => setTimeout(r, 120));
  const afterBoot = drawn();
  if (!afterBoot.length) throw new Error('the page drew nothing after boot');
  const activeViews = document.querySelectorAll('.view')
    .filter((node) => node.classList.contains('on'));
  if (activeViews.length !== 1) {
    problems.push(`a stale saved view did not fall back to one visible dashboard (active=${activeViews.length}, view=${vm.runInContext('view', sandbox)})`);
  }

  // Every row that is actually attached to either Ports table must own live
  // buttons. Testing only document.created used to miss handlers lost by
  // cloneNode(true), because the original, unattached row still worked.
  const attachedButtons = [];
  const collectButtons = (node, out) => {
    if (node.tagName === 'BUTTON') out.push(node);
    (node.children || []).forEach((child) => collectButtons(child, out));
  };
  if ((payload.mappings || []).some((m) => !m.listener)) {
    for (const id of ['mapping-body', 'mapping-body-2']) {
      const body = nodes.get(id);
      const rows = (body.children || []).filter((node) => node.tagName === 'TR');
      if (!rows.length) { problems.push(`${id} has no attached mapping rows`); continue; }
      for (const row of rows) {
        const buttons = [];
        collectButtons(row, buttons);
        if (buttons.length < 2) problems.push(`${id} contains a row without both actions`);
        for (const button of buttons) {
          if (typeof button.onclick !== 'function') problems.push(`${id} contains a dead action`);
          else attachedButtons.push(button);
        }
      }
    }
  }

  const views = document.querySelectorAll('[data-view]');
  for (const button of views) {
    if (!button.onclick) { problems.push(`the ${button.dataset.view} tab has no handler`); continue; }
    button.onclick();
    await new Promise((r) => setTimeout(r, 40));
  }
  for (const selector of ['[data-act]', '[data-tune]', '[data-cert]', '[data-tool]']) {
    for (const button of document.querySelectorAll(selector)) {
      if (!button.onclick) { problems.push(`${selector} button ${JSON.stringify(button.dataset)} has no handler`); continue; }
      button.onclick();
      await new Promise((r) => setTimeout(r, 40));
    }
  }
  for (const id of ['add-mapping', 'theme-toggle', 'lang-toggle', 'log-refresh',
                    'log-auto', 'make-link', 'save-settings', 'logout',
                    'update-check', 'download-logs', 'export-config']) {
    const node = nodes.get(id);
    if (node && node.onclick) { node.onclick(); await new Promise((r) => setTimeout(r, 60)); }
  }

  // the wizard, step by step: details -> preview -> apply -> rollback
  if (!nodes.has('wz_next')) throw new Error('the wizard did not render its first step');
  nodes.get('wz_next').onclick();
  await new Promise((r) => setTimeout(r, 60));
  if (!nodes.has('wz_apply')) throw new Error('the wizard did not render its second step');
  nodes.get('wz_apply').onclick();
  await new Promise((r) => setTimeout(r, 80));
  if (nodes.has('wz_rollback')) { nodes.get('wz_rollback').onclick(); await new Promise((r) => setTimeout(r, 80)); }

  // and the speed test result, which is the most table-shaped thing on the page
  const speed = nodes.get('run-speedtest');
  if (speed && speed.onclick) { speed.disabled = false; speed.onclick(); await new Promise((r) => setTimeout(r, 80)); }

  // Setup links are panel-only on an exit or a reverse relay. An exit can
  // also import a reverse relay's link from its own Pair page.
  if (role === 'exit' || (payload.panel && payload.panel.setup_link)) {
    const host = nodes.get('setup-host');
    if (host) host.value = '203.0.113.9:8787';
    const make = nodes.get('setup-link-btn');
    if (!make || typeof make.onclick !== 'function') problems.push('setup-link button is not wired');
    else {
      make.onclick();
      await new Promise((r) => setTimeout(r, 80));
      if (!seen.includes('/api/setup-link')) problems.push('setup-link button never called its API');
      if (!(nodes.get('setup-link-out').textContent || '').startsWith('simurgh://')) {
        problems.push('setup link was not rendered');
      }
    }
  }
  if (role === 'exit') {
    const field = nodes.get('exit-pair-link');
    if (field) field.value = 'simurgh://198.51.100.8:8787/setup?u=admin&p=c2VjcmV0';
    const button = nodes.get('exit-pair-btn');
    if (!button || typeof button.onclick !== 'function') problems.push('exit Pair button is not wired');
    else { button.onclick(); await new Promise((r) => setTimeout(r, 80)); }
  }

  // A direct relay can complete pairing or explicitly replace its exit. The
  // latter must send the replacement acknowledgement, never silently re-pair.
  if (role === 'relay' && payload.panel && !payload.panel.reverse) {
    const field = nodes.get('pair-link');
    const button = nodes.get('pair-btn');
    if (!button) problems.push('a direct relay has no Pair button');
    else {
      if (field) field.value = 'simurgh://198.51.100.7:8787/setup?u=admin&p=c2VjcmV0#de-frankfurt';
      button.onclick();
      await new Promise((r) => setTimeout(r, 80));
      if (!seen.some((url) => url.split('?')[0] === '/api/pair')) {
        problems.push('the Pair button never called /api/pair');
      }
      if (payload.panel.paired && !bodies.some((body) => body.link && body.replace === true)) {
        problems.push('replacing a paired exit did not send explicit confirmation');
      }
    }
  }

  // the buttons the table rows built themselves (enable/disable, delete)
  for (const node of document.created) {
    if (typeof node.onclick === 'function') {
      node.onclick();
      await new Promise((r) => setTimeout(r, 40));
    }
  }

  // The install-update action waits for the restarted panel's new version.
  await new Promise((r) => setTimeout(r, 2200));
  if (!location.reloaded) problems.push('a healthy new panel version did not reload the page');

  const calls = new Set(seen.map((url) => url.split('?')[0]));
  const required = ['/api/layout'];
  if ((payload.mappings || []).some((m) => !m.listener)) required.push('/api/mappings');
  for (const path of required) {
    if (!calls.has(path)) problems.push(`nothing ever called ${path}`);
  }
  console.log('drew:', drawn().join(', '));
  console.log('called:', [...calls].sort().join(' '));
  if (problems.length) {
    console.error('problems:\n  ' + problems.join('\n  '));
    process.exit(1);
  }
  console.log('ok');
  process.exit(0);
})().catch((err) => {
  console.error('the panel threw while drawing:', err.stack);
  process.exit(1);
});
