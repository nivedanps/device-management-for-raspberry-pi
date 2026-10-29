/*
 * SARAS fleet dashboard (CONTRACT.md section 8). Plain browser JavaScript, no libraries.
 *
 * Two views on one page, chosen by the URL hash so a device can be bookmarked:
 *   #/               fleet: status counts, filters and one table row per box
 *   #/device/<id>    one box: status, open alerts, live charts, services, sync, readings, events
 *
 * The read API (section 7) is polled every 5 s while the tab is visible and not at all while it
 * is hidden. Updates are applied in place: rows are matched by key and text is written only when
 * it changed, so nothing flickers, jumps or loses keyboard focus.
 *
 * Security: every value from the server or a device is written with textContent, as an attribute
 * value or as SVG geometry. Nothing is ever parsed as HTML. The CSP allows only same-origin files.
 */
'use strict';

(() => {
  const REFRESH_MS = 5000;
  const FETCH_TIMEOUT_MS = 15000;
  const LOADER_DELAY_MS = 200;
  const API_ROOT = new URL('api/v1/', document.baseURI);
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const DASH = '—';
  const NBSP = ' ';

  const STATES = ['online', 'warning', 'critical', 'offline'];
  const STATE_LABEL = { online: 'Online', warning: 'Warning', critical: 'Critical', offline: 'Offline' };
  const STATE_RANK = { offline: 0, critical: 1, warning: 2, online: 3 }; // server precedence, worst first

  // ------------------------------------------------------------------------------ DOM helpers

  const $ = (id) => document.getElementById(id);

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function svgEl(tag, attrs, parent) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attrs || {})) node.setAttribute(name, String(value));
    if (parent) parent.appendChild(node);
    return node;
  }

  function setText(node, value) {
    const text = value == null ? '' : String(value);
    if (node.textContent !== text) node.textContent = text;
  }

  function setAttr(node, name, value) {
    if (value == null || value === '') {
      if (node.hasAttribute(name)) node.removeAttribute(name);
    } else if (node.getAttribute(name) !== String(value)) {
      node.setAttribute(name, String(value));
    }
  }

  function setClass(node, className) {
    if (node.className !== className) node.className = className;
  }

  /**
   * Make parent's element children match `items`, in order. A child is reused when its data-key
   * matches, so existing rows are updated in place instead of being rebuilt.
   */
  function syncChildren(parent, items, keyOf, create, update) {
    const old = new Map();
    for (const child of Array.from(parent.children)) {
      const key = child.dataset.key;
      if (key === undefined || old.has(key)) child.remove();
      else old.set(key, child);
    }
    const used = new Set();
    const nodes = items.map((item, index) => {
      const key = String(keyOf(item, index));
      let node = used.has(key) ? null : old.get(key);
      if (!node) {
        node = create(item);
        node.dataset.key = key;
      }
      used.add(key);
      update(node, item, index);
      return node;
    });
    for (const [key, child] of old) if (!used.has(key)) child.remove();
    let cursor = parent.firstElementChild;
    for (const node of nodes) {
      if (node === cursor) cursor = node.nextElementSibling;
      else parent.insertBefore(node, cursor);
    }
  }

  function pillInto(node, extraClass) {
    const glyph = el('span', 'glyph');
    glyph.setAttribute('aria-hidden', 'true');
    const label = el('span', 'pill-text');
    node.replaceChildren(glyph, label);
    return { node, glyph, label, extra: extraClass || '' };
  }

  const makePill = (extraClass) => pillInto(el('span'), extraClass);

  function setPill(pill, tone, text) {
    setClass(pill.node, `pill ${tone}${pill.extra ? ` ${pill.extra}` : ''}`);
    setClass(pill.glyph, `glyph ${tone}`);
    setText(pill.label, text);
  }

  function cell(className, label) {
    const td = el('td', className);
    td.setAttribute('role', 'cell');
    td.dataset.label = label;
    return td;
  }

  const plural = (n, word) => (n === 1 ? word : `${word}s`);
  const obj = (value) => (value && typeof value === 'object' && !Array.isArray(value) ? value : {});
  const str = (value) => (typeof value === 'string' && value.trim() ? value : null);

  // ------------------------------------------------------------------------------ formatting

  const isNum = (value) => typeof value === 'number' && Number.isFinite(value);
  const numberFormats = new Map();

  function fmtNumber(value, digits = 0) {
    if (!isNum(value)) return DASH;
    let format = numberFormats.get(digits);
    if (!format) {
      format = new Intl.NumberFormat(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
      numberFormats.set(digits, format);
    }
    const rounded = Number(value.toFixed(digits));
    return format.format(rounded === 0 ? 0 : rounded).replace(/^-/, '−');
  }

  /** "5.3%", "40.9 °C", "26.8 W", "−63 dBm"; an em dash when there is no reading. */
  function fmtValue(value, unit, digits = 1) {
    if (!isNum(value)) return DASH;
    const text = fmtNumber(value, digits);
    if (!unit) return text;
    return unit === '%' ? `${text}%` : `${text}${NBSP}${unit}`;
  }

  const fmtHM = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });
  const fmtHMS = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' });
  const fmtDayHM = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' });
  const fmtDate = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
  const fmtAxisDay = new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric' });
  const fmtFull = new Intl.DateTimeFormat(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit',
    timeZoneName: 'short',
  });
  const TZ_NAME = (() => {
    try {
      const part = new Intl.DateTimeFormat(undefined, { timeZoneName: 'short' })
        .formatToParts(new Date()).find((p) => p.type === 'timeZoneName');
      return part ? part.value : '';
    } catch (err) {
      return '';
    }
  })();

  function parseTime(value) {
    if (typeof value !== 'string' || !value) return null;
    const ms = Date.parse(value);
    return Number.isFinite(ms) ? ms : null;
  }

  function sameDay(a, b) {
    const x = new Date(a);
    const y = new Date(b);
    return x.getFullYear() === y.getFullYear() && x.getMonth() === y.getMonth() && x.getDate() === y.getDate();
  }

  const fmtFullTime = (ms) => (ms == null ? '' : fmtFull.format(new Date(ms)));

  /** Today: "16:32" (or with seconds); another day: "28 Sep, 16:32". Local time. */
  function fmtWhen(ms, withSeconds) {
    if (ms == null) return DASH;
    if (sameDay(ms, serverNow())) return (withSeconds ? fmtHMS : fmtHM).format(new Date(ms));
    return fmtDayHM.format(new Date(ms));
  }

  function fmtAgo(seconds) {
    if (!isNum(seconds)) return DASH;
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return `${s} s ago`;
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 172800) return `${Math.floor(s / 3600)} h ago`;
    return `${Math.floor(s / 86400)} d ago`;
  }

  function fmtDuration(seconds) {
    if (!isNum(seconds)) return DASH;
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return `${s} s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m} min`;
    const h = Math.floor(m / 60);
    if (h < 48) return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
    const d = Math.floor(h / 24);
    return h % 24 ? `${d} d ${h % 24} h` : `${d} d`;
  }

  function fmtHours(hours) {
    if (!isNum(hours)) return DASH;
    if (hours < 10) return `${fmtNumber(hours, 1)} h`;
    const text = `${fmtNumber(hours, 0)} h`;
    return hours >= 48 ? `${text} (${fmtNumber(hours / 24, 1)} days)` : text;
  }

  // ------------------------------------------------------------------------------ plain-language text

  const SERVICE_NAMES = {
    'app-live': 'SchoolAI app (live check)',
    'app-ready': 'SchoolAI app (ready check)',
    postgres: 'PostgreSQL database',
    vllm: 'AI server (vLLM)',
  };

  function serviceLabel(name) {
    if (typeof name !== 'string' || !name) return 'A service';
    if (SERVICE_NAMES[name]) return SERVICE_NAMES[name];
    if (name.startsWith('systemd:')) return name.slice(8);
    if (name.startsWith('container:')) return `Container ${name.slice(10)}`;
    return name;
  }

  function serviceShort(name) {
    return name.replace(/^systemd:/, '').replace(/\.service$/, '').replace(/^container:/, '');
  }

  const REASON_SHORT = {
    DEVICE_OFFLINE: 'No heartbeat',
    GPU_OVERHEAT: 'GPU overheating',
    GPU_TEMP_HIGH: 'GPU running hot',
    SOC_TEMP_HIGH: 'Running hot',
    DISK_CRITICAL: 'Disk almost full',
    DISK_LOW: 'Disk space low',
    RAM_HIGH: 'Memory use high',
    CPU_HIGH: 'CPU use high',
    GPU_HIGH: 'GPU use high',
    SYNC_STALE: 'Backup/sync stale',
    SYNC_MISMATCH: 'Backup copy incomplete',
    CLOCK_UNSYNCED: 'Clock not synchronised',
    NETWORK_POOR: 'Poor network',
    LLM_SATURATED: 'AI server overloaded',
  };

  /** Short label for an open alert code in the fleet table. */
  function reasonShort(code) {
    if (code.startsWith('SERVICE_DOWN:')) return `${serviceShort(code.slice(13))} down`;
    return REASON_SHORT[code] || code;
  }

  const COMPONENT_LABEL = {
    os: 'Operating system',
    kernel: 'Kernel',
    l4t: 'Jetson Linux (L4T)',
    jetpack: 'JetPack',
    nvidia_driver: 'NVIDIA driver',
    cuda_driver: 'CUDA driver',
    docker: 'Docker',
    nvidia_container_toolkit: 'NVIDIA Container Toolkit',
    jetson_stats: 'jetson-stats',
    bootloader: 'Bootloader',
    python: 'Python',
    app_version: 'SchoolAI app',
    vllm_version: 'vLLM',
    llm_model: 'AI model',
  };

  const componentLabel = (name) => COMPONENT_LABEL[name] || String(name);

  /**
   * Plain-language title of an event. ctx.live is set for open alerts: they use the device's
   * current readings (e.g. today's sync age) instead of the values stored when they were raised.
   */
  function eventTitle(event, ctx) {
    const code = typeof event.code === 'string' ? event.code : '';
    const data = obj(event.data);
    if (code.startsWith('SERVICE_DOWN:')) return `${serviceLabel(code.slice(13))} is down`;
    switch (code) {
      case 'DEVICE_OFFLINE':
        return ctx.live && isNum(ctx.secondsSinceSeen)
          ? `No heartbeat for ${fmtDuration(ctx.secondsSinceSeen)}` : 'Device went offline (no heartbeat)';
      case 'GPU_OVERHEAT': return 'GPU overheating';
      case 'GPU_TEMP_HIGH': return 'GPU temperature high';
      case 'SOC_TEMP_HIGH': return 'Board temperature high';
      case 'DISK_CRITICAL': return str(data.mount) ? `Disk ${data.mount} is almost full` : 'Disk almost full';
      case 'DISK_LOW': return str(data.mount) ? `Disk ${data.mount} is running low on space` : 'Disk space low';
      case 'RAM_HIGH': return 'Memory use high';
      case 'CPU_HIGH': return 'CPU use high';
      case 'GPU_HIGH': return 'GPU use high';
      case 'SYNC_STALE': {
        const hours = ctx.live && isNum(ctx.syncAgeH) ? ctx.syncAgeH : data.age_h;
        return isNum(hours) ? `Backup/sync hasn't succeeded for ${fmtHours(hours)}` : "Backup/sync hasn't succeeded recently";
      }
      case 'SYNC_MISMATCH':
        return isNum(data.mismatched_tables)
          ? `The backup copy doesn't match the box (${fmtNumber(data.mismatched_tables, 0)} ${plural(data.mismatched_tables, 'table')})`
          : "The backup copy doesn't match the box";
      case 'CLOCK_UNSYNCED': return 'Clock not synchronised';
      case 'NETWORK_POOR': return 'Poor network connection';
      case 'LLM_SATURATED': return 'AI server (vLLM) overloaded';
      case 'CONNECTION_INTERRUPTED': return 'Connection interrupted';
      case 'SERVICE_RESTARTED': return `${serviceLabel(data.service)} restarted`;
      case 'DEVICE_REGISTERED': return 'Device registered';
      case 'DEVICE_RESTARTED': return 'Device restarted';
      case 'DEVICE_RECONNECTED': return 'Device back online';
      case 'VERSION_CHANGED':
        return str(data.component) ? `${componentLabel(data.component)} version changed` : 'Software version changed';
      default: return code || 'Event';
    }
  }

  // ------------------------------------------------------------------------------ API client

  class ApiError extends Error {
    constructor(status, message) {
      super(message);
      this.status = status;
    }
  }

  let clockOffsetMs = 0; // server clock minus browser clock, from the Date response header
  const serverNow = () => Date.now() + clockOffsetMs;

  function noteServerClock(header, startedMs) {
    const server = Date.parse(header || '');
    if (!Number.isFinite(server)) return;
    const offset = server + 500 - (startedMs + Date.now()) / 2; // the header has whole seconds
    if (Math.abs(offset - clockOffsetMs) > 1500) clockOffsetMs = offset;
  }

  async function getJSON(path, signal) {
    const started = Date.now();
    let response;
    try {
      response = await fetch(new URL(path, API_ROOT), {
        signal, cache: 'no-store', credentials: 'same-origin', headers: { Accept: 'application/json' },
      });
    } catch (err) {
      if (signal && signal.aborted) throw err;
      throw new ApiError(0, 'network error');
    }
    noteServerClock(response.headers.get('Date'), started);
    let body = null;
    try {
      body = await response.json();
    } catch (err) {
      if (signal && signal.aborted) throw err;
    }
    if (!response.ok) {
      const message = body && typeof body.error === 'string' ? body.error : `HTTP ${response.status}`;
      throw new ApiError(response.status, message);
    }
    if (!body || typeof body !== 'object') throw new ApiError(response.status, 'the answer was not JSON');
    return body;
  }

  async function postJSON(path, payload, adminToken, method = 'POST') {
    const headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
    if (adminToken) headers['Authorization'] = `Bearer ${adminToken}`;
    const response = await fetch(new URL(path, API_ROOT), {
      method,
      credentials: 'same-origin',
      headers,
      body: payload !== undefined ? JSON.stringify(payload) : undefined,
    });
    let body = null;
    try { body = await response.json(); } catch (_) {}
    if (!response.ok) {
      const msg = body && typeof body.error === 'string' ? body.error : `HTTP ${response.status}`;
      throw new ApiError(response.status, msg);
    }
    return body;
  }

  function describeError(error) {
    if (error && error.status === 401) return 'The fleet server asks you to sign in: reload the page.';
    if (error && error.timedOut) return 'The fleet server is not answering.';
    if (!error || error.status === 0) return "Can't reach the fleet server.";
    return `The fleet server returned an error (${error.message}).`;
  }

  // ------------------------------------------------------------------------------ connection state

  const conn = { ok: null, lastOk: 0 };
  let announceTimer = 0;

  /** The page's single polite live region. */
  function announce(message, delay = 0) {
    clearTimeout(announceTimer);
    announceTimer = setTimeout(() => {
      const region = $('announcer');
      region.textContent = '';
      announceTimer = setTimeout(() => { region.textContent = message; }, 60);
    }, delay);
  }

  function setConnection(ok, error) {
    const live = $('live');
    if (ok) {
      conn.lastOk = Date.now();
      live.dataset.state = 'ok';
      setText($('live-text'), `Live · updated ${fmtHMS.format(new Date(conn.lastOk))}`);
      $('banner').hidden = true;
      if (conn.ok === false) announce('The connection to the fleet server is back.');
    } else {
      live.dataset.state = 'error';
      setText($('live-text'), 'Not updating');
      const shown = conn.lastOk ? ` The page shows data from ${fmtHMS.format(new Date(conn.lastOk))}.` : '';
      setText($('banner'), `${describeError(error)}${shown} Retrying every 5 seconds.`);
      $('banner').hidden = false;
      if (conn.ok !== false) announce(`${describeError(error)} Retrying every 5 seconds.`);
    }
    conn.ok = ok;
  }

  // ------------------------------------------------------------------------------ routing and refresh

  let route = { view: 'fleet', id: null };

  function parseRoute() {
    const hash = location.hash.replace(/^#/, '');
    const match = /^\/device\/([^?#]+)/.exec(hash);
    if (match) {
      try {
        return { view: 'device', id: decodeURIComponent(match[1]) };
      } catch (err) {
        // a malformed %-escape: fall back to the fleet
      }
    }
    return { view: 'fleet', id: null };
  }

  const deviceHref = (id) => `#/device/${encodeURIComponent(id)}`;

  function applyRoute(initial) {
    const previous = route;
    route = parseRoute();
    const onDevice = route.view === 'device';
    $('view-fleet').hidden = onDevice;
    $('view-device').hidden = !onDevice;
    if (onDevice && (initial || previous.view !== 'device' || previous.id !== route.id)) resetDeviceView(route.id);
    if (!initial) {
      if (onDevice) {
        window.scrollTo(0, 0);
        $('device-title').focus({ preventScroll: true });
      } else {
        const back = previous.view === 'device' ? findDeviceLink(previous.id) : null;
        (back || $('fleet-title')).focus();
      }
    }
    updateTitle();
    refresh();
  }

  function findDeviceLink(id) {
    for (const link of $('fleet-rows').querySelectorAll('a.device-link')) {
      if (link.textContent === id) return link;
    }
    return null;
  }

  const loop = { timer: 0, controller: null, seq: 0 };

  function scheduleRefresh(delay) {
    clearTimeout(loop.timer);
    loop.timer = setTimeout(refresh, delay);
  }

  function stopRefresh() {
    clearTimeout(loop.timer);
    loop.seq += 1;
    if (loop.controller) loop.controller.abort();
    loop.controller = null;
  }

  /** Load the current view now, then again every 5 s. Superseded requests are aborted. */
  async function refresh() {
    clearTimeout(loop.timer);
    if (document.hidden) return; // resumes on visibilitychange
    if (loop.controller) loop.controller.abort();
    const controller = new AbortController();
    loop.controller = controller;
    const seq = ++loop.seq;
    const target = route;
    const timeout = setTimeout(() => {
      controller.timedOut = true;
      controller.abort();
    }, FETCH_TIMEOUT_MS);
    try {
      if (target.view === 'device') await loadDevice(target.id, controller.signal);
      else await loadFleet(controller.signal);
      if (seq === loop.seq) setConnection(true);
    } catch (error) {
      if (seq !== loop.seq) return; // a newer refresh or a route change took over
      let failure = error;
      if (controller.timedOut) failure = Object.assign(new ApiError(0, 'timed out'), { timedOut: true });
      else if (error && error.name === 'AbortError') return;
      setConnection(false, failure);
      if (target.view === 'device') deviceLoadFailed(failure);
      else fleetLoadFailed(failure);
    } finally {
      clearTimeout(timeout);
      if (seq === loop.seq) {
        loop.controller = null;
        if (!document.hidden) scheduleRefresh(REFRESH_MS);
      }
    }
  }

  function updateTitle() {
    let title = 'Fleet · SARAS';
    if (route.view === 'device') {
      const detail = device.detail;
      const dev = detail ? obj(detail.device) : {};
      const state = detail ? STATE_LABEL[obj(detail.status).state] : null;
      title = `${dev.hostname || dev.device_id || route.id}${state ? ` (${state})` : ''} · SARAS fleet`;
    } else if (fleet.data) {
      const counts = obj(fleet.data.counts);
      const parts = ['critical', 'offline', 'warning'].filter((s) => counts[s] > 0).map((s) => `${counts[s]} ${s}`);
      if (parts.length) title = `${parts.join(', ')} · Fleet · SARAS`;
    }
    if (document.title !== title) document.title = title;
  }

  // ------------------------------------------------------------------------------ fleet view

  const fleet = {
    data: null,
    filters: { state: '', type: '', product: '', q: '' },
    sort: { key: 'state', desc: false },
    loadingTimer: 0,
  };

  const FLEET_METRICS = [
    { key: 'cpu', field: 'cpu_pct', label: 'CPU', unit: '%', digits: 1 },
    { key: 'gpu', field: 'gpu_pct', label: 'GPU', unit: '%', digits: 1 },
    { key: 'temp', field: 'max_temp_c', label: 'Hottest', unit: '°C', digits: 1 },
    { key: 'ram', field: 'ram_pct', label: 'RAM', unit: '%', digits: 1 },
    { key: 'disk', field: 'disk_pct', label: 'Disk', unit: '%', digits: 1 },
    { key: 'power', field: 'power_total_w', label: 'Power', unit: 'W', digits: 1 },
  ];
  const DESCENDING_FIRST = new Set(['seen', 'cpu', 'gpu', 'temp', 'ram', 'disk', 'power']);

  // Alert limits from CONTRACT.md section 6: [warning, critical]. Used to emphasise a reading.
  const LIMITS = {
    cpu_pct: [90], gpu_pct: [90], ram_pct: [90], disk_pct: [85, 95], max_temp_c: [95, 105], gpu_temp_c: [95, 105],
  };

  function levelClass(field, value) {
    const limits = LIMITS[field];
    if (!limits || !isNum(value)) return '';
    if (limits.length > 1 && value >= limits[1]) return 'lvl-crit';
    return value >= limits[0] ? 'lvl-warn' : '';
  }

  async function loadFleet(signal) {
    const data = await getJSON('fleet', signal);
    if (signal.aborted) return;
    fleet.data = data;
    clearTimeout(fleet.loadingTimer);
    renderFleet();
  }

  function fleetLoadFailed(error) {
    if (fleet.data) return; // keep showing the last good table; the banner explains
    clearTimeout(fleet.loadingTimer);
    showFleetEmpty("Can't load the fleet", describeError(error), false);
  }

  function showFleetEmpty(title, text, offerClear) {
    setText($('fleet-empty-title'), title);
    setText($('fleet-empty-text'), text);
    $('fleet-empty-clear').hidden = !offerClear;
    $('fleet-empty').hidden = false;
    $('fleet-table-wrap').hidden = true;
  }

  const lower = (value) => (typeof value === 'string' && value ? value.toLowerCase() : null);

  function sortValue(d, key) {
    switch (key) {
      case 'state': return STATE_RANK[d.state] ?? 9;
      case 'device': return lower(d.device_id);
      case 'type': return lower(d.device_type);
      case 'product': return lower(d.product);
      case 'app': return lower(d.versions && d.versions.app_version);
      case 'site': return lower(d.site_code);
      case 'seen': return isNum(d.seconds_since_seen) ? d.seconds_since_seen : null;
      default: {
        const metric = FLEET_METRICS.find((m) => m.key === key);
        const value = metric ? obj(d.latest)[metric.field] : null;
        return isNum(value) ? value : null;
      }
    }
  }

  function sortDevices(list) {
    const { key, desc } = fleet.sort;
    return list
      .map((d) => ({ d, v: sortValue(d, key) }))
      .sort((a, b) => {
        if (a.v !== b.v) {
          if (a.v === null) return 1; // missing values last, in both directions
          if (b.v === null) return -1;
          const c = typeof a.v === 'string' ? a.v.localeCompare(b.v) : a.v - b.v;
          if (c) return desc ? -c : c;
        }
        return ((STATE_RANK[a.d.state] ?? 9) - (STATE_RANK[b.d.state] ?? 9))
          || String(a.d.device_id).localeCompare(String(b.d.device_id));
      })
      .map((x) => x.d);
  }

  function searchText(d) {
    const reasons = Array.isArray(d.reasons) ? d.reasons.filter((r) => typeof r === 'string') : [];
    return [d.device_id, d.hostname, d.device_type, d.model, d.product, d.customer, d.site_code, d.location,
      d.device_group, d.agent_version, (d.versions && d.versions.app_version), STATE_LABEL[d.state], ...reasons, ...reasons.map(reasonShort)]
      .filter((x) => typeof x === 'string').join(' ').toLowerCase();
  }

  function applyFilters(devices) {
    const f = fleet.filters;
    const terms = f.q.toLowerCase().split(/\s+/).filter(Boolean);
    return devices.filter((d) => {
      if (f.state && d.state !== f.state) return false;
      if (f.type && d.device_type !== f.type) return false;
      if (f.product && d.product !== f.product) return false;
      if (terms.length) {
        const text = searchText(d);
        if (!terms.every((t) => text.includes(t))) return false;
      }
      return true;
    });
  }

  const hasFilters = () => Object.values(fleet.filters).some(Boolean);

  function distinct(values) {
    return [...new Set(values.filter((v) => typeof v === 'string' && v))].sort((a, b) => a.localeCompare(b));
  }

  /** Options = "all" + the values in the data; a chosen value that disappeared stays selectable. */
  function syncOptions(select, values, allLabel) {
    const current = select.value;
    const wanted = ['', ...values];
    if (current && !wanted.includes(current)) wanted.push(current);
    const have = Array.from(select.options, (o) => o.value);
    if (have.length === wanted.length && have.every((v, i) => v === wanted[i])) return;
    select.replaceChildren(...wanted.map((value) => {
      const option = el('option', null, value || allLabel);
      option.value = value;
      return option;
    }));
    select.value = current;
  }

  function createFleetRow() {
    const tr = el('tr');
    tr.setAttribute('role', 'row');
    const status = cell('cell-status wide bare', 'Status');
    const pill = makePill();
    const reasons = el('span', 'reasons');
    status.append(pill.node, reasons);
    const deviceCell = cell('cell-device wide bare', 'Device');
    const link = el('a', 'device-link');
    const host = el('span', 'cell-sub');
    deviceCell.append(link, host);
    const type = cell('', 'Type');
    const product = cell('', 'Product');
    const appCell = cell('', 'App');
    const site = cell('', 'Site');
    const seen = cell('num', 'Last seen');
    const metrics = FLEET_METRICS.map((m) => cell('num third', m.label));
    tr.append(status, deviceCell, type, product, appCell, site, seen, ...metrics);
    tr.parts = { pill, reasons, link, host, type, product, appCell, site, seen, metrics };
    return tr;
  }

  function updateFleetRow(tr, d) {
    const p = tr.parts;
    const state = STATE_LABEL[d.state] ? d.state : 'offline';
    setClass(tr, `state-${state}${state === 'offline' ? ' stale' : ''}`);
    setPill(p.pill, state, STATE_LABEL[state]);
    const reasons = Array.isArray(d.reasons) ? d.reasons.filter((r) => typeof r === 'string') : [];
    syncChildren(p.reasons, reasons, (r) => r, () => el('span', 'reason'), (node, r) => setText(node, reasonShort(r)));
    p.reasons.hidden = reasons.length === 0;
    setText(p.link, d.device_id);
    setAttr(p.link, 'href', deviceHref(d.device_id));
    const host = d.hostname && d.hostname !== d.device_id ? d.hostname : '';
    setText(p.host, host);
    p.host.hidden = !host;
    setText(p.type, d.device_type || DASH);
    setText(p.product, d.product || DASH);
    const appVer = (d.versions && d.versions.app_version) || DASH;
    setText(p.appCell, appVer);
    setText(p.site, d.site_code || DASH);
    const extra = [d.customer, d.location].filter((x) => str(x)).join(' · ');
    setAttr(p.site, 'title', extra);
    setText(p.seen, d.last_seen ? fmtAgo(d.seconds_since_seen) : 'Never');
    setAttr(p.seen, 'title', fmtFullTime(parseTime(d.last_seen)));
    const latest = obj(d.latest);
    FLEET_METRICS.forEach((m, i) => {
      const value = latest[m.field];
      setText(p.metrics[i], fmtValue(value, m.unit, m.digits));
      setClass(p.metrics[i], `num third${state === 'offline' ? '' : ` ${levelClass(m.field, value)}`}`.trim());
    });
  }

  function updateSortHeaders() {
    for (const th of document.querySelectorAll('.fleet-table th[data-sort]')) {
      setAttr(th, 'aria-sort', th.dataset.sort === fleet.sort.key ? (fleet.sort.desc ? 'descending' : 'ascending') : null);
    }
  }

  function renderFleet() {
    const data = fleet.data;
    if (!data) return;
    const devices = Array.isArray(data.devices) ? data.devices.filter((d) => d && typeof d.device_id === 'string') : [];
    const counts = obj(data.counts);

    for (const key of ['total', ...STATES]) setText($(`count-${key}`), fmtNumber(isNum(counts[key]) ? counts[key] : 0, 0));
    for (const button of $('counts').querySelectorAll('.count')) {
      const state = button.dataset.state || '';
      setAttr(button, 'aria-pressed', String(state === fleet.filters.state));
      button.classList.toggle('has', Boolean(state) && counts[state] > 0);
    }

    syncOptions($('f-type'), distinct(devices.map((d) => d.device_type)), 'All types');
    syncOptions($('f-product'), distinct(devices.map((d) => d.product)), 'All products');

    const shown = sortDevices(applyFilters(devices));
    syncChildren($('fleet-rows'), shown, (d) => d.device_id, createFleetRow, updateFleetRow);

    const total = devices.length;
    setText($('fleet-count'), hasFilters()
      ? `Showing ${shown.length} of ${total} ${plural(total, 'device')}`
      : `${total} ${plural(total, 'device')}`);
    $('f-clear').disabled = !hasFilters();
    if (!total) {
      showFleetEmpty('No devices have reported yet',
        'A box appears here after its agent sends its first heartbeat to this server.', false);
    } else if (!shown.length) {
      showFleetEmpty('No devices match these filters', 'Change or clear the filters to see more devices.', true);
    } else {
      $('fleet-empty').hidden = true;
      $('fleet-table-wrap').hidden = false;
    }
    updateSortHeaders();
    renderFleetSchedules();
    updateTitle();
  }

  function filtersChanged() {
    renderFleet();
    if (!fleet.data) return;
    const total = Array.isArray(fleet.data.devices) ? fleet.data.devices.length : 0;
    const shown = $('fleet-rows').children.length;
    announce(hasFilters() ? `Showing ${shown} of ${total} ${plural(total, 'device')}.` : `Showing all ${total} ${plural(total, 'device')}.`, 700);
  }

  function clearFilters() {
    fleet.filters = { state: '', type: '', product: '', q: '' };
    $('f-status').value = '';
    $('f-type').value = '';
    $('f-product').value = '';
    $('f-search').value = '';
    filtersChanged();
    $('f-search').focus();
  }

  function wireFleet() {
    $('counts').addEventListener('click', (event) => {
      const button = event.target.closest('button.count');
      if (!button) return;
      const state = button.dataset.state || '';
      fleet.filters.state = fleet.filters.state === state ? '' : state;
      $('f-status').value = fleet.filters.state;
      filtersChanged();
    });
    $('f-status').addEventListener('change', (event) => { fleet.filters.state = event.target.value; filtersChanged(); });
    $('f-type').addEventListener('change', (event) => { fleet.filters.type = event.target.value; filtersChanged(); });
    $('f-product').addEventListener('change', (event) => { fleet.filters.product = event.target.value; filtersChanged(); });
    $('f-search').addEventListener('input', (event) => { fleet.filters.q = event.target.value.trim(); filtersChanged(); });
    $('filters').addEventListener('submit', (event) => event.preventDefault());
    $('f-clear').addEventListener('click', clearFilters);
    $('fleet-empty-clear').addEventListener('click', clearFilters);

    document.querySelector('.fleet-table thead').addEventListener('click', (event) => {
      const th = event.target.closest('th[data-sort]');
      if (!th || !event.target.closest('button')) return;
      const key = th.dataset.sort;
      if (fleet.sort.key === key) fleet.sort.desc = !fleet.sort.desc;
      else fleet.sort = { key, desc: DESCENDING_FIRST.has(key) };
      renderFleet();
    });

    // The whole row opens the device; the link inside it stays the keyboard and middle-click target.
    $('fleet-rows').addEventListener('click', (event) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      if (event.target.closest('a')) return;
      const selection = window.getSelection();
      if (selection && String(selection).trim()) return; // the user is selecting text
      const row = event.target.closest('tr');
      const link = row && row.querySelector('a.device-link');
      if (link) location.hash = link.getAttribute('href');
    });
  }

  // ------------------------------------------------------------------------------ device view

  const device = {
    id: null,
    minutes: 60,
    detail: null,
    metrics: null,
    rows: [],
    charts: [],
    pill: null,
    syncPill: null,
    loadingTimer: 0,
  };

  function resetDeviceView(id) {
    device.id = id;
    device.detail = null;
    device.metrics = null;
    device.rows = [];
    clearTimeout(device.loadingTimer);
    setText($('device-title'), id);
    setText($('crumb-current'), id);
    $('device-state').hidden = true;
    setText($('device-sub'), '');
    $('device-facts').hidden = false;
    $('device-sections').hidden = false;
    for (const factId of ['d-seen', 'd-uptime', 'd-interval', 'd-clock', 'd-ip', 'd-agent']) {
      setText($(factId), DASH);
      setAttr($(factId), 'title', null);
    }
    $('collector-note').hidden = true;
    for (const listId of ['alert-list', 'services-rows', 'sync-kv', 'now-groups', 'about-groups', 'events-rows', 'readings-rows', 'config-groups', 'logs-rows']) {
      $(listId).replaceChildren();
    }
    for (const noteId of ['alerts-empty', 'services-empty', 'sync-empty', 'events-empty', 'sync-note', 'config-empty', 'logs-empty']) $(noteId).hidden = true;
    $('sync-state').hidden = true;
    setText($('alerts-count'), '0');
    setClass($('alerts-count'), 'count-pill');
    setText($('services-meta'), '');
    setText($('now-meta'), '');
    for (const chart of device.charts) hideHover(chart);
    renderCharts();
    device.loadingTimer = setTimeout(() => {
      if (!device.detail && device.id === id) setText($('device-sub'), 'Loading…');
    }, LOADER_DELAY_MS);
    resetHistory(id);
  }

  async function loadDevice(id, signal) {
    const path = `devices/${encodeURIComponent(id)}`;
    let detail;
    let metrics;
    try {
      [detail, metrics] = await Promise.all([
        getJSON(`${path}?extended=1`, signal),
        getJSON(`${path}/metrics?minutes=${device.minutes}`, signal),
      ]);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        if (!signal.aborted && id === device.id) showMissing(id);
        return;
      }
      throw error;
    }
    if (signal.aborted || id !== device.id) return;
    clearTimeout(device.loadingTimer);
    device.detail = detail;
    device.metrics = metrics;
    renderDevice(detail);
    renderCharts();
  }

  function deviceLoadFailed(error) {
    if (device.detail) return; // keep the last good data; the banner explains
    clearTimeout(device.loadingTimer);
    setText($('device-sub'), `Can't load this device right now. ${describeError(error)}`);
  }

  function showMissing(id) {
    clearTimeout(device.loadingTimer);
    device.detail = null;
    setText($('device-title'), 'Device not found');
    setText($('crumb-current'), id);
    setText($('device-sub'), `No device with the ID “${id}” has reported to this server. Check the link, or go back to the fleet.`);
    $('device-state').hidden = true;
    $('device-facts').hidden = true;
    $('collector-note').hidden = true;
    $('device-sections').hidden = true;
    updateTitle();
  }

  function renderDevice(detail) {
    $('device-facts').hidden = false;
    $('device-sections').hidden = false;
    renderDeviceHead(detail);
    renderAlerts(detail);
    renderServices(detail);
    renderSync(detail);
    renderGroups($('now-groups'), readingGroups(detail));
    renderGroups($('about-groups'), aboutGroups(detail));
    renderEvents(detail);
    renderConfig(detail);
    renderLogs(detail);
    renderHardwareDiagnostics(detail);
    renderUpdateJobs(detail);
    updateTitle();
  }

  function renderDeviceHead(detail) {
    const dev = obj(detail.device);
    const st = obj(detail.status);
    const hb = obj(detail.latest);
    const name = dev.hostname || dev.device_id || device.id;
    setText($('device-title'), name);
    setText($('crumb-current'), name);
    const state = STATE_LABEL[st.state] ? st.state : 'offline';
    setPill(device.pill, state, STATE_LABEL[state]);
    $('device-state').hidden = false;

    const idNote = dev.hostname && dev.hostname !== dev.device_id ? `ID ${dev.device_id}` : null;
    const sub = [dev.device_type, dev.product, dev.site_code && `site ${dev.site_code}`, dev.customer, dev.location,
      dev.device_group && `group ${dev.device_group}`, idNote].filter((x) => str(x));
    setText($('device-sub'), sub.join(' · '));

    setText($('d-seen'), st.last_seen ? fmtAgo(st.seconds_since_seen) : 'Never (no live heartbeat yet)');
    setAttr($('d-seen'), 'title', fmtFullTime(parseTime(st.last_seen)));
    const uptime = fmtDuration(st.uptime_s);
    setText($('d-uptime'), state === 'offline' && isNum(st.uptime_s) ? `${uptime} when last seen` : uptime);
    const booted = parseTime(st.booted_at);
    setAttr($('d-uptime'), 'title', booted == null ? null : `Booted ${fmtFullTime(booted)}`);
    setText($('d-interval'), isNum(st.interval_s) ? `Every ${fmtNumber(st.interval_s, 0)} s` : DASH);

    const ntp = obj(hb.system).ntp_synced;
    const offset = st.clock_offset_s;
    const clock = [];
    if (ntp === true) clock.push('NTP synced');
    if (ntp === false) clock.push('NTP off');
    if (isNum(offset)) {
      clock.push(Math.abs(offset) < 1 ? 'within 1 s of the server'
        : `${fmtDuration(Math.abs(offset))} ${offset > 0 ? 'behind' : 'ahead of'} the server`);
    }
    setText($('d-clock'), clock.length ? clock.join(', ') : DASH);
    setText($('d-ip'), st.lan_ip || DASH);
    setText($('d-agent'), st.agent_version ? `Version ${st.agent_version}` : DASH);
    const sw = Array.isArray(detail.software) ? detail.software : [];
    const appVerObj = sw.find((s) => s && s.component === 'app_version');
    setText($('d-app-ver'), appVerObj && str(appVerObj.version) ? appVerObj.version : DASH);

    const errors = Object.entries(obj(hb.collector_errors)).filter(([, reason]) => str(reason));
    $('collector-note').hidden = errors.length === 0;
    setText($('collector-text'), errors.length
      ? `Not collected on this box: ${errors.map(([name, reason]) => `${name} (${reason})`).join('; ')}.` : '');
  }

  const severityRank = (event) => (event.severity === 'critical' ? 0 : event.severity === 'warning' ? 1 : 2);

  function liveContext(detail) {
    const fromHeartbeat = obj(obj(detail.latest).sync).age_h;
    const fromReport = obj(detail.sync).age_h;
    return {
      live: true,
      syncAgeH: isNum(fromHeartbeat) ? fromHeartbeat : (isNum(fromReport) ? fromReport : null),
      secondsSinceSeen: obj(detail.status).seconds_since_seen,
    };
  }

  function renderAlerts(detail) {
    const alerts = (Array.isArray(detail.open_alerts) ? detail.open_alerts : [])
      .filter((a) => a && a.id != null)
      .sort((a, b) => severityRank(a) - severityRank(b) || String(b.ts).localeCompare(String(a.ts)));
    const ctx = liveContext(detail);
    syncChildren($('alert-list'), alerts, (a) => a.id, createAlertItem, (li, a) => updateAlertItem(li, a, ctx));
    $('alerts-empty').hidden = alerts.length > 0;
    setText($('alerts-count'), String(alerts.length));
    const worst = alerts.length ? (alerts[0].severity === 'critical' ? ' critical' : ' warning') : '';
    setClass($('alerts-count'), `count-pill${worst}`);
  }

  function createAlertItem() {
    const li = el('li', 'alert-item');
    const pill = makePill();
    const body = el('div', 'alert-body');
    const title = el('p', 'alert-title');
    const message = el('p', 'alert-detail');
    const since = el('p', 'alert-detail');
    body.append(title, message, since);
    li.append(pill.node, body);
    li.parts = { pill, title, message, since };
    return li;
  }

  function updateAlertItem(li, alert, ctx) {
    const p = li.parts;
    const tone = alert.severity === 'critical' ? 'critical' : 'warning';
    setPill(p.pill, tone, tone === 'critical' ? 'Critical' : 'Warning');
    setText(p.title, eventTitle(alert, ctx));
    setText(p.message, alert.message || '');
    p.message.hidden = !alert.message;
    const opened = parseTime(alert.ts);
    setText(p.since, opened == null ? '' : `Open since ${fmtWhen(opened)} (${fmtAgo((serverNow() - opened) / 1000)})`);
    setAttr(p.since, 'title', fmtFullTime(opened));
  }

  const SERVICE_STATUS = {
    up: ['online', 'Up'],
    down: ['critical', 'Down'],
    degraded: ['warning', 'Degraded'],
    unknown: ['offline', 'Unknown'],
    not_configured: ['offline', 'Not configured'],
  };
  const SERVICE_RANK = { down: 0, degraded: 1, unknown: 2, up: 3, not_configured: 4 };

  function renderServices(detail) {
    const services = (Array.isArray(detail.services) ? detail.services : [])
      .filter((s) => s && typeof s.service === 'string')
      .sort((a, b) => (SERVICE_RANK[a.status] ?? 2) - (SERVICE_RANK[b.status] ?? 2) || a.service.localeCompare(b.service));
    // Rows from the last live heartbeat carry its time (status.last_seen); anything older is a
    // service the box no longer reports.
    const newest = str(obj(detail.status).last_seen)
      || services.reduce((acc, s) => (typeof s.ts === 'string' && s.ts > acc ? s.ts : acc), '');
    // When each service last started (systemd ActiveEnterTimestamp, Docker StartedAt), from the
    // last heartbeat; the box's own clock, like the other times it reports.
    const latest = obj(detail.latest);
    const started = new Map((Array.isArray(latest.services) ? latest.services : [])
      .filter((x) => x && typeof x.name === 'string' && str(x.since))
      .map((x) => [x.name, x.since]));
    syncChildren($('services-rows'), services, (s) => s.service, createServiceRow,
      (tr, s) => updateServiceRow(tr, s, newest, started.get(s.service)));
    $('services-empty').hidden = services.length > 0;
    $('services-wrap').hidden = services.length === 0;
    const checked = services.filter((s) => s.status !== 'not_configured');
    const up = checked.filter((s) => s.status === 'up').length;
    setText($('services-meta'), checked.length ? `${up} of ${checked.length} up` : '');
  }

  function createServiceRow() {
    const tr = el('tr');
    tr.setAttribute('role', 'row');
    const nameCell = cell('wide bare', 'Service');
    const name = el('span', 'cell-title');
    const about = el('span', 'cell-sub');
    nameCell.append(name, about);
    const statusCell = cell('', 'Status');
    const pill = makePill();
    const actBadge = el('span', 'badge-activity');
    actBadge.hidden = true;
    statusCell.append(pill.node, actBadge);
    const latency = cell('num', 'Latency');
    const restarts = cell('num', 'Restarts');
    const availability = cell('num', 'Available (24 h)');
    const detailCell = cell('wide', 'Detail');
    tr.append(nameCell, statusCell, latency, restarts, availability, detailCell);
    tr.parts = { name, about, pill, actBadge, latency, restarts, availability, detail: detailCell };
    return tr;
  }

  function updateServiceRow(tr, s, newest, startedAt) {
    const p = tr.parts;
    setText(p.name, s.service);
    const about = [SERVICE_NAMES[s.service], s.kind].filter((x) => str(x)).join(' · ');
    setText(p.about, about);
    p.about.hidden = !about;
    const [tone, label] = SERVICE_STATUS[s.status] || SERVICE_STATUS.unknown;
    setPill(p.pill, tone, label);
    if (s.activity_state && p.actBadge) {
      p.actBadge.hidden = false;
      setClass(p.actBadge, `badge-activity ${s.activity_state}`);
      setText(p.actBadge, s.activity_state.toUpperCase());
    } else if (p.actBadge) {
      p.actBadge.hidden = true;
    }
    setText(p.latency, fmtValue(s.latency_ms, 'ms', 1));
    setText(p.restarts, fmtNumber(s.restarts, 0));
    const failed = isNum(s.failed_checks_24h) && s.failed_checks_24h > 0
      ? ` · ${fmtNumber(s.failed_checks_24h, 0)} failed` : '';
    setText(p.availability, `${fmtValue(s.availability_24h_pct, '%', 1)}${failed}`);
    setAttr(p.availability, 'title', isNum(s.failed_checks_24h)
      ? `${fmtNumber(s.failed_checks_24h, 0)} ${plural(s.failed_checks_24h, 'check')} failed in the last 24 hours` : null);
    const when = parseTime(s.ts);
    const stale = newest && s.ts !== newest && when != null ? `Not in the latest heartbeat (last reported ${fmtWhen(when)})` : '';
    const up = stale ? null : parseTime(startedAt);
    const logTime = s.last_log_ts ? parseTime(s.last_log_ts) : null;
    const logInfo = logTime != null ? `log ${fmtAgo((serverNow() - logTime) / 1000)}` : '';
    setText(p.detail, [s.detail, up == null ? '' : `started ${fmtWhen(up)}`, logInfo, stale].filter((x) => str(x)).join(' · ') || DASH);
    setAttr(p.detail, 'title', up == null ? null : `Last started ${fmtFullTime(up)} (the box's clock)`);
  }

  function renderSync(detail) {
    const report = detail.sync && typeof detail.sync === 'object' ? detail.sync : null;
    const fromHeartbeat = obj(detail.latest).sync;
    const current = fromHeartbeat && typeof fromHeartbeat === 'object' ? fromHeartbeat : report;
    const pill = device.syncPill;
    if (!current) {
      $('sync-kv').replaceChildren();
      $('sync-empty').hidden = false;
      $('sync-note').hidden = true;
      pill.node.hidden = true;
      return;
    }
    $('sync-empty').hidden = true;
    const ctx = liveContext(detail);
    const reasons = Array.isArray(obj(detail.status).reasons) ? detail.status.reasons : [];
    if (reasons.includes('SYNC_STALE')) setPill(pill, 'critical', 'Stale');
    else if (reasons.includes('SYNC_MISMATCH')) setPill(pill, 'critical', 'Mismatch');
    else if (reasons.includes('SYNC_FAILED')) setPill(pill, 'critical', 'Sync Failed');
    else if (reasons.includes('SYNC_CONFLICTS')) setPill(pill, 'warning', 'Conflicts');
    else if (isNum(ctx.syncAgeH) && ctx.syncAgeH <= 8) setPill(pill, 'online', 'OK');
    else setPill(pill, 'offline', 'Unknown');
    pill.node.hidden = false;

    const deviceTime = (raw) => {
      const ms = parseTime(raw);
      if (ms == null) return str(raw) || DASH;
      return fmtFull.format(new Date(ms));
    };
    const rows = [
      row('success', 'Last success', deviceTime(current.last_success),
        { sub: isNum(ctx.syncAgeH) ? `${fmtHours(ctx.syncAgeH)} ago` : '' }),
      row('attempt', 'Last attempt', deviceTime(current.last_attempt)),
      row('offline', 'Runs while offline', fmtNumber(current.offline_count, 0)),
      row('result', 'Last run result', str(current.unit_result) || DASH),
    ];
    if ('pending_changes' in current) {
      rows.push(row('pending', 'Pending changes', fmtNumber(current.pending_changes, 0)));
    }
    if ('failed_batches' in current || 'conflicts' in current) {
      rows.push(
        row('failed_batches', 'Failed batches', fmtNumber(current.failed_batches, 0)),
        row('conflicts', 'Sync conflicts', fmtNumber(current.conflicts, 0), { level: isNum(current.conflicts) && current.conflicts > 0 ? 'lvl-crit' : '' })
      );
    }
    if ('sync_api_health' in current || 'db_health' in current) {
      rows.push(
        row('api_health', 'Central sync API', str(current.sync_api_health) || DASH),
        row('db_health', 'Database health', str(current.db_health) || DASH)
      );
    }
    if (str(current.last_error)) {
      rows.push(row('last_error', 'Last sync error', current.last_error, { level: 'lvl-warn' }));
    }
    // Row counts of the last sync that reached the server (newer agents only): totals, never table names.
    if ('source_rows' in current || 'mismatched_tables' in current) {
      const { source_rows: box, target_rows: server, source_tables: tables, mismatched_tables: mismatched } = current;
      rows.push(
        row('rows', 'Rows copied', isNum(box) ? `${fmtNumber(box, 0)}${isNum(tables) ? ` in ${fmtNumber(tables, 0)} ${plural(tables, 'table')}` : ''}` : DASH,
          { sub: isNum(box) && isNum(server) && server !== box ? `${fmtNumber(server, 0)} on the server` : '' }),
        row('mismatch', 'Tables not matching', fmtNumber(mismatched, 0),
          { level: isNum(mismatched) && mismatched > 0 ? 'lvl-crit' : '' }),
      );
    }
    renderRows($('sync-kv'), rows);
    const unsynced = reasons.includes('CLOCK_UNSYNCED');
    $('sync-note').hidden = !unsynced;
    setText($('sync-note'), unsynced ? "These times come from the box's own clock, which isn't synchronised." : '');
  }

  const row = (key, label, value, options) => ({ key, label, value, ...(options || {}) });

  function meterClass(value, limits) {
    if (!limits || !isNum(value)) return 'meter-fill';
    if (limits.length > 1 && value >= limits[1]) return 'meter-fill crit';
    return value >= limits[0] ? 'meter-fill warn' : 'meter-fill';
  }

  function createKvRow() {
    const div = el('div', 'kv-row');
    const dt = el('dt');
    const dd = el('dd');
    const value = el('span', 'kv-value');
    const sub = el('span', 'kv-sub');
    const meter = el('span', 'meter');
    const fill = el('span', 'meter-fill');
    meter.setAttribute('aria-hidden', 'true');
    meter.append(fill);
    dd.append(value, sub, meter);
    div.append(dt, dd);
    div.parts = { dt, value, sub, meter, fill };
    return div;
  }

  function updateKvRow(div, r) {
    const p = div.parts;
    setText(p.dt, r.label);
    const empty = r.value == null || r.value === '' || r.value === DASH;
    setText(p.value, empty ? (r.unset || DASH) : r.value);
    setClass(p.value, ['kv-value', empty && r.unset ? 'unset' : '', r.level || ''].filter(Boolean).join(' '));
    setText(p.sub, r.sub || '');
    p.sub.hidden = !r.sub;
    const showMeter = isNum(r.meter);
    p.meter.hidden = !showMeter;
    if (showMeter) {
      const width = `${Math.max(0, Math.min(100, r.meter))}%`;
      if (p.fill.style.width !== width) p.fill.style.width = width;
      setClass(p.fill, meterClass(r.meter, r.limits));
    }
  }

  const renderRows = (dl, rows) => syncChildren(dl, rows, (r) => r.key, createKvRow, updateKvRow);

  function renderGroups(container, groups) {
    syncChildren(container, groups, (g) => g.key, () => {
      const section = el('section', 'group');
      const heading = el('h3');
      const dl = el('dl', 'kv');
      section.append(heading, dl);
      section.parts = { heading, dl };
      return section;
    }, (section, g) => {
      setText(section.parts.heading, g.title);
      renderRows(section.parts.dl, g.rows);
    });
  }

  /** "Current readings": everything in the last live heartbeat that the charts don't show. */
  function readingGroups(detail) {
    const hb = obj(detail.latest);
    const sys = obj(hb.system);
    const gpu = obj(hb.gpu);
    const power = obj(hb.power);
    const net = obj(hb.network);
    const st = obj(detail.status);
    setText($('now-meta'), st.last_seen ? `From the last heartbeat, received ${fmtAgo(st.seconds_since_seen)}` : '');

    const groups = [];
    const loads = [sys.load_1m, sys.load_5m, sys.load_15m];
    // One pegged core (a stuck single-threaded process) hides behind a low total.
    const cores = Array.isArray(sys.cpu_per_core_pct) ? sys.cpu_per_core_pct.filter(isNum) : [];
    const busiest = cores.length ? Math.max(...cores) : null;
    groups.push({
      key: 'cpu',
      title: 'CPU and memory',
      rows: [
        row('cpu', 'CPU use', fmtValue(sys.cpu_pct, '%', 1)),
        row('cores', 'Busiest core', fmtValue(busiest, '%', 1), {
          sub: cores.length ? `${cores.length} cores: ${cores.map((v) => fmtNumber(v, 0)).join(' · ')}` : '',
          level: levelClass('cpu_pct', busiest),
        }),
        row('freq', 'CPU clock', fmtValue(sys.cpu_freq_mhz, 'MHz', 0)),
        row('load', 'Load (1, 5, 15 min)', loads.every(isNum) ? loads.map((v) => fmtNumber(v, 2)).join(' · ') : DASH),
        row('ram', 'Memory', isNum(sys.ram_used_mb) && isNum(sys.ram_total_mb)
          ? `${fmtNumber(sys.ram_used_mb / 1024, 1)} of ${fmtValue(sys.ram_total_mb / 1024, 'GB', 1)}` : DASH,
        { sub: isNum(sys.ram_pct) ? fmtValue(sys.ram_pct, '%', 1) : '', meter: sys.ram_pct, limits: LIMITS.ram_pct }),
        row('swap', 'Swap in use', fmtValue(sys.swap_pct, '%', 1)),
      ],
    });

    const disks = Array.isArray(sys.disks) ? sys.disks.filter((d) => d && typeof d === 'object') : [];
    groups.push({
      key: 'storage',
      title: 'Storage',
      rows: [
        ...disks.map((d, i) => row(`disk:${i}:${d.mount}`, str(d.mount) || `Disk ${i + 1}`,
          isNum(d.used_gb) && isNum(d.total_gb) ? `${fmtNumber(d.used_gb, 1)} of ${fmtValue(d.total_gb, 'GB', 1)} used` : DASH,
          {
            // free_gb is what can still be written: less than total minus used (reserved blocks).
            sub: [isNum(d.pct) ? fmtValue(d.pct, '%', 1) : '', isNum(d.free_gb) ? `${fmtValue(d.free_gb, 'GB', 1)} free` : '']
              .filter(Boolean).join(' · '),
            meter: d.pct,
            limits: LIMITS.disk_pct,
          })),
        row('journal', 'System journal', fmtValue(sys.journal_mb, 'MB', 0)),
        ...('var_log_mb' in sys ? [row('logs', 'All logs (/var/log)', fmtValue(sys.var_log_mb, 'MB', 0),
          { sub: isNum(sys.var_log_mb) ? 'including the journal' : '' })] : []),
      ],
    });

    const clock = isNum(gpu.clock_mhz) && isNum(gpu.max_clock_mhz)
      ? `${fmtNumber(gpu.clock_mhz, 0)} of ${fmtValue(gpu.max_clock_mhz, 'MHz', 0)}` : fmtValue(gpu.clock_mhz, 'MHz', 0);
    groups.push({
      key: 'gpu',
      title: 'GPU',
      rows: [
        row('name', 'Name', str(gpu.name) || DASH, { sub: str(gpu.source) ? `read via ${gpu.source}` : '' }),
        row('util', 'Use', fmtValue(gpu.util_pct, '%', 1)),
        row('temp', 'Temperature', fmtValue(gpu.temp_c, '°C', 1), { level: levelClass('gpu_temp_c', gpu.temp_c) }),
        row('clock', 'Clock', clock),
        row('power', 'Power', fmtValue(gpu.power_w, 'W', 2)),
        row('memory', 'Memory', str(gpu.memory_note) || DASH),
      ],
    });

    const zones = Object.entries(obj(hb.thermal_c)).filter(([, v]) => isNum(v)).sort((a, b) => b[1] - a[1]);
    groups.push({
      key: 'temps',
      title: 'Temperature sensors',
      rows: zones.length
        ? zones.map(([name, v]) => row(`t:${name}`, name, fmtValue(v, '°C', 1), { level: levelClass('max_temp_c', v) }))
        : [row('none', 'Sensors', DASH)],
    });

    const rails = Object.entries(obj(power.rails_w)).filter(([, v]) => isNum(v));
    groups.push({
      key: 'power',
      title: 'Power and cooling',
      rows: [
        row('mode', 'Power mode', str(power.mode) || DASH),
        row('total', 'Total draw', fmtValue(power.total_w, 'W', 1)),
        ...rails.map(([name, v]) => row(`rail:${name}`, name, fmtValue(v, 'W', 2))),
        row('fan', 'Fan', fmtValue(power.fan_rpm, 'rpm', 0)),
      ],
    });

    const signal = net.wifi_signal_dbm;
    const loss = net.gateway_loss_pct;
    groups.push({
      key: 'net',
      title: 'Network',
      rows: [
        row('iface', 'Interface', str(net.iface) ? `${net.iface}${str(net.link) ? ` (link ${net.link})` : ''}` : DASH),
        row('signal', 'Wi-Fi signal', fmtValue(signal, 'dBm', 0), { level: isNum(signal) && signal <= -75 ? 'lvl-warn' : '' }),
        row('rate', 'Wi-Fi bit rate', fmtValue(net.wifi_bitrate_mbps, 'Mbps', 1)),
        row('gateway', 'Gateway round trip', fmtValue(net.gateway_rtt_ms, 'ms', 1)),
        row('loss', 'Gateway packet loss', fmtValue(loss, '%', 1), { level: isNum(loss) && loss >= 20 ? 'lvl-warn' : '' }),
        row('server', 'Server round trip', fmtValue(net.last_server_rtt_ms, 'ms', 1)),
      ],
    });

    const ai = detail.ai && typeof detail.ai === 'object' ? detail.ai : null;
    if (ai) {
      const aborted = obj(obj(hb.ai).vllm).requests_aborted_total; // in the heartbeat, not in ai_usage
      groups.push({
        key: 'ai',
        title: 'AI server (vLLM)',
        rows: [
          row('status', 'Status', str(ai.status) || DASH),
          row('model', 'Model', str(ai.model) || DASH),
          row('running', 'Requests running', fmtNumber(ai.running, 0)),
          row('waiting', 'Requests waiting', fmtNumber(ai.waiting, 0)),
          row('kv', 'KV cache in use', fmtValue(ai.kv_cache_pct, '%', 1)),
          row('served', 'Requests served', fmtNumber(ai.requests_success_total, 0)),
          row('aborted', 'Requests aborted', fmtNumber(aborted, 0)),
          row('prompt', 'Prompt tokens', fmtNumber(ai.prompt_tokens_total, 0)),
          row('generated', 'Generated tokens', fmtNumber(ai.generation_tokens_total, 0)),
          row('e2e', 'Average response time', fmtValue(ai.avg_e2e_latency_s, 's', 2)),
          row('ttft', 'Average time to first token', fmtValue(ai.avg_ttft_s, 's', 2)),
        ],
      });
    }

    const ollama = obj(obj(hb.ai).ollama);
    if (ollama.status) {
      const loaded = Array.isArray(ollama.models) ? ollama.models.map((m) => m.name || m).join(', ') : '';
      const running = Array.isArray(ollama.running_models) ? ollama.running_models.map((m) => m.name || m).join(', ') : '';
      groups.push({
        key: 'ollama',
        title: 'AI server (Ollama)',
        rows: [
          row('ol_status', 'Status', str(ollama.status) || DASH),
          row('ol_models', 'Available models', loaded || 'None'),
          row('ol_running', 'Running models', running || 'None'),
        ],
      });
    }

    if (sys.rpi_throttled) {
      const th = sys.rpi_throttled;
      const flags = [];
      if (th.undervoltage_now) flags.push('Undervoltage NOW');
      if (th.throttled_now) flags.push('Thermal throttled NOW');
      if (th.freq_capped_now) flags.push('Freq capped NOW');
      if (th.undervoltage_past) flags.push('Past undervoltage');
      if (th.throttled_past) flags.push('Past throttling');
      groups.push({
        key: 'rpi_status',
        title: 'Raspberry Pi status',
        rows: [
          row('throttled_raw', 'Throttling bitmask', str(th.raw_hex) || DASH),
          row('throttled_state', 'Condition', flags.length ? flags.join(', ') : 'Normal (No throttling)', {
            level: (th.undervoltage_now || th.throttled_now) ? 'lvl-crit' : (flags.length ? 'lvl-warn' : ''),
          }),
        ],
      });
    }

    return groups;
  }

  const REGISTRY_FIELDS = [
    ['product', 'Product'], ['customer', 'Customer'], ['site_code', 'Site code'], ['location', 'Location'],
    ['device_group', 'Group'], ['installed_on', 'Installed on'], ['support_contact', 'Support contact'],
  ];

  function aboutGroups(detail) {
    const dev = obj(detail.device);
    const st = obj(detail.status);
    const software = (Array.isArray(detail.software) ? detail.software : [])
      .filter((s) => s && typeof s.component === 'string' && str(s.version))
      .sort((a, b) => componentLabel(a.component).localeCompare(componentLabel(b.component)));
    const coords = isNum(dev.latitude) && isNum(dev.longitude)
      ? `${fmtNumber(dev.latitude, 5)}, ${fmtNumber(dev.longitude, 5)}` : null;
    const firstSeen = parseTime(dev.first_seen);
    const booted = parseTime(st.booted_at);
    return [
      {
        key: 'identity',
        title: 'Identity',
        rows: [
          row('id', 'Device ID', str(dev.device_id) || DASH),
          row('serial', 'Serial number', str(dev.serial) || DASH),
          row('model', 'Model', str(dev.model) || DASH),
          row('type', 'Type', str(dev.device_type) || DASH),
          row('host', 'Host name', str(dev.hostname) || DASH),
          row('booted', 'Booted', booted == null ? DASH : fmtFull.format(new Date(booted))),
          row('first', 'First seen', firstSeen == null ? DASH : fmtFull.format(new Date(firstSeen))),
        ],
      },
      {
        key: 'registry',
        title: 'Registry',
        rows: [
          ...REGISTRY_FIELDS.map(([field, label]) => row(field, label, str(dev[field]), { unset: 'Not set' })),
          row('coords', 'Coordinates', coords, { unset: 'Not set' }),
          row('notes', 'Notes', str(dev.notes), { unset: 'None' }),
        ],
      },
      {
        key: 'versions',
        title: 'Software versions',
        rows: [
          row('agent', 'Monitoring agent', str(st.agent_version) || DASH),
          ...software.map((s) => {
            const since = parseTime(s.first_seen);
            return row(`v:${s.component}`, componentLabel(s.component), s.version,
              { sub: since == null ? '' : `since ${fmtDate.format(new Date(since))}` });
          }),
        ],
      },
    ];
  }

  function renderEvents(detail) {
    const events = (Array.isArray(detail.recent_events) ? detail.recent_events : []).filter((e) => e && e.id != null);
    syncChildren($('events-rows'), events, (e) => e.id, createEventRow, updateEventRow);
    $('events-empty').hidden = events.length > 0;
    $('events-wrap').hidden = events.length === 0;
  }

  function createEventRow() {
    const tr = el('tr');
    tr.setAttribute('role', 'row');
    const time = cell('ev-time', 'Time');
    const type = cell('', 'Type');
    const pill = makePill();
    type.append(pill.node);
    const what = cell('wide bare', 'Event');
    const title = el('span', 'cell-title');
    const message = el('span', 'cell-sub');
    what.append(title, message);
    const state = cell('', 'State');
    tr.append(time, type, what, state);
    tr.parts = { time, pill, title, message, state };
    return tr;
  }

  function updateEventRow(tr, event) {
    const p = tr.parts;
    const at = parseTime(event.ts);
    setText(p.time, at == null ? DASH : fmtWhen(at, true));
    setAttr(p.time, 'title', fmtFullTime(at));
    const isAlert = event.kind === 'alert';
    const tone = isAlert ? (event.severity === 'critical' ? 'critical' : 'warning') : 'info';
    setPill(p.pill, tone, isAlert ? (tone === 'critical' ? 'Critical' : 'Warning') : 'Info');
    setText(p.title, eventTitle(event, { live: false }));
    setText(p.message, event.message || '');
    let state = DASH;
    if (isAlert) {
      const resolved = parseTime(event.resolved_at);
      if (event.resolved_at == null) state = 'Open';
      else if (resolved === at) state = 'One-off';
      else state = `Cleared ${fmtWhen(resolved, true)}`;
    }
    setText(p.state, state);
  }

  function renderConfig(detail) {
    const raw = detail.effective_config;
    if (!raw || typeof raw !== 'object' || !Object.keys(raw).length) {
      $('config-empty').hidden = false;
      $('config-groups').replaceChildren();
      setText($('config-version'), 'Default (Version 0)');
      return;
    }
    $('config-empty').hidden = true;
    const cfg = raw.config || raw;
    const rev = raw.revision ? raw.revision.slice(0, 12) : null;
    const verText = rev ? `Revision ${rev} (v${raw.version ?? 0})` : `Version ${raw.version ?? 0}`;
    setText($('config-version'), verText);
    const rows = [];
    if (raw.revision) rows.push(row('revision', 'Effective revision', raw.revision.slice(0, 16)));
    rows.push(row('version', 'Scope version', String(raw.version ?? 0)));
    rows.push(row('hb_int', 'Heartbeat interval', isNum(cfg.interval_s || cfg.heartbeat_interval_s) ? `${cfg.interval_s || cfg.heartbeat_interval_s} s` : DASH));
    rows.push(row('samp_int', 'Telemetry sample interval', isNum(cfg.telemetry_sample_interval_s) ? `${cfg.telemetry_sample_interval_s} s` : DASH));
    rows.push(row('sync_int', 'Sync interval', isNum(cfg.sync_interval_s || cfg.sync_check_interval_s) ? `${cfg.sync_interval_s || cfg.sync_check_interval_s} s` : DASH));
    rows.push(row('log_level', 'Log level', str(cfg.log_level) || DASH));

    const th = cfg.health_thresholds || cfg.thresholds;
    if (th && typeof th === 'object') {
      if (isNum(th.cpu_temp_warn_c)) rows.push(row('temp_warn', 'CPU temp warning limit', `${th.cpu_temp_warn_c} °C`));
      if (isNum(th.cpu_temp_crit_c)) rows.push(row('temp_crit', 'CPU temp critical limit', `${th.cpu_temp_crit_c} °C`));
      if (isNum(th.disk_pct_warn)) rows.push(row('disk_warn', 'Disk warning limit', `${th.disk_pct_warn}%`));
      if (isNum(th.disk_pct_crit)) rows.push(row('disk_crit', 'Disk critical limit', `${th.disk_pct_crit}%`));
    }
    if (cfg.optional_collectors && typeof cfg.optional_collectors === 'object') {
      const activeCols = Object.entries(cfg.optional_collectors).map(([k, v]) => `${k}: ${v ? 'enabled' : 'disabled'}`).join(', ');
      rows.push(row('opt_cols', 'Optional collectors', activeCols));
    }
    if (cfg.monitored_services && Array.isArray(cfg.monitored_services)) {
      rows.push(row('services', 'Monitored services', cfg.monitored_services.join(', ')));
    }
    renderGroups($('config-groups'), [{ key: 'cfg_details', title: 'Settings', rows }]);
  }

  // ------------------------------------------------------------------------------ fleet schedules
  async function renderFleetSchedules() {
    try {
      const res = await getJSON('updates/schedules');
      const list = Array.isArray(res.schedules) ? res.schedules : [];
      setText($('fleet-schedules-count'), String(list.length));
      $('schedules-empty').hidden = list.length > 0;
      $('schedules-wrap').hidden = list.length === 0;

      syncChildren($('schedules-rows'), list, (s) => s.schedule_id,
        () => {
          const tr = el('tr');
          tr.setAttribute('role', 'row');
          const idCell = cell('bare', 'ID');
          const nameCell = cell('bare', 'Name');
          const devCell = cell('bare', 'Device');
          const link = el('a', 'device-link');
          devCell.append(link);
          const opCell = cell('bare', 'Operation');
          const timeCell = cell('num', 'Time');
          const winCell = cell('', 'Window');
          const statusCell = cell('', 'Status');
          const pill = makePill();
          statusCell.append(pill.node);
          const actCell = cell('', 'Action');
          const cancelBtn = el('button', 'btn btn-sm btn-danger', 'Cancel');
          actCell.append(cancelBtn);
          tr.append(idCell, nameCell, devCell, opCell, timeCell, winCell, statusCell, actCell);
          tr.parts = { idCell, nameCell, link, opCell, timeCell, winCell, pill, cancelBtn };
          return tr;
        },
        (tr, s) => {
          const p = tr.parts;
          setText(p.idCell, s.schedule_id);
          setText(p.nameCell, s.name);
          setText(p.link, s.device_id);
          setAttr(p.link, 'href', deviceHref(s.device_id));
          setText(p.opCell, s.operation);
          const schedTime = parseTime(s.scheduled_at);
          setText(p.timeCell, schedTime ? fmtFullTime(schedTime) : s.scheduled_at);
          setText(p.winCell, s.maintenance_window || 'None');
          const tone = s.status === 'completed' ? 'ok' : s.status === 'active' ? 'info' : 'off';
          setPill(p.pill, tone, s.status.toUpperCase());
          p.cancelBtn.disabled = s.status !== 'active';
          p.cancelBtn.onclick = async () => {
            const token = prompt('Enter admin bearer token to cancel schedule:');
            if (!token) return;
            try {
              await postJSON(`updates/schedules/${encodeURIComponent(s.schedule_id)}/cancel`, {}, token);
              announce(`Schedule ${s.schedule_id} cancelled`);
              renderFleetSchedules();
            } catch (err) {
              alert(`Error: ${err.message}`);
            }
          };
        }
      );
    } catch (_) {
      $('schedules-empty').hidden = false;
      $('schedules-wrap').hidden = true;
    }
  }

  // ------------------------------------------------------------------------------ hardware diagnostics
  function renderHardwareDiagnostics(detail) {
    const hb = obj(detail.latest);
    const sys = obj(hb.system);
    const storage = obj(sys.storage_health);
    const mem = obj(sys.memory_health);
    const fan = obj(sys.fan_health);
    const power = obj(hb.power);
    const temps = obj(hb.thermal_c);

    // NVMe / Storage SMART
    const hasStorage = Boolean(storage && (storage.smart_available || isNum(storage.percentage_used)));
    if (hasStorage) {
      const crit = storage.critical_warning;
      const critText = isNum(crit) && crit > 0 ? `Alert flag 0x${crit.toString(16)}` : 'None';
      setText($('diag-storage-crit'), critText);
      setText($('diag-storage-spare'), isNum(storage.available_spare_pct) ? `${storage.available_spare_pct}%` : DASH);
      setText($('diag-storage-wear'), isNum(storage.percentage_used) ? `${storage.percentage_used}%` : DASH);
      setText($('diag-storage-media'), isNum(storage.media_errors) ? String(storage.media_errors) : '0');

      const isStorageCrit = (isNum(crit) && crit > 0) || (isNum(storage.percentage_used) && storage.percentage_used >= 95);
      const isStorageWarn = isNum(storage.percentage_used) && storage.percentage_used >= 80;
      setPill({ node: $('diag-storage-pill') }, isStorageCrit ? 'critical' : isStorageWarn ? 'warning' : 'ok',
        isStorageCrit ? 'CRITICAL' : isStorageWarn ? 'WARNING' : 'HEALTHY');
    } else {
      setText($('diag-storage-crit'), 'Not detected');
      setText($('diag-storage-spare'), DASH);
      setText($('diag-storage-wear'), DASH);
      setText($('diag-storage-media'), DASH);
      setPill({ node: $('diag-storage-pill') }, 'off', 'UNAVAILABLE');
    }

    // Memory EDAC
    const hasMem = Boolean(mem && mem.edac_supported);
    if (hasMem) {
      setText($('diag-mem-support'), 'Supported (Active)');
      const ue = isNum(mem.ue_count) ? mem.ue_count : 0;
      const ce = isNum(mem.ce_count) ? mem.ce_count : 0;
      setText($('diag-mem-ue'), `${ue} errors`);
      setText($('diag-mem-ce'), `${ce} errors`);
      const isMemCrit = ue > 0;
      const isMemWarn = ce > 50;
      setPill({ node: $('diag-memory-pill') }, isMemCrit ? 'critical' : isMemWarn ? 'warning' : 'ok',
        isMemCrit ? 'CRITICAL' : isMemWarn ? 'WARNING' : 'HEALTHY');
    } else {
      setText($('diag-mem-support'), mem && mem.edac_supported === false ? 'Not supported on this SoC' : 'Unknown');
      setText($('diag-mem-ue'), '0 errors');
      setText($('diag-mem-ce'), '0 errors');
      setPill({ node: $('diag-memory-pill') }, 'off', 'NORMAL');
    }

    // Fan health
    const fanRpm = isNum(fan.rpm) ? fan.rpm : isNum(power.fan_rpm) ? power.fan_rpm : null;
    setText($('diag-fan-rpm'), fanRpm !== null ? `${fanRpm} RPM` : DASH);
    const stalled = Boolean(fan && fan.stalled);
    setText($('diag-fan-stall'), stalled ? 'STALLED' : 'Normal');

    const throttled = obj(sys.rpi_throttled);
    const isThrottled = Boolean(throttled && (throttled.under_voltage_now || throttled.throttled_now));
    setText($('diag-fan-throttled'), isThrottled ? 'Active Throttling' : 'Normal');

    const isFanCrit = stalled || (fanRpm !== null && fanRpm <= 50 && isNum(temps.cpu) && temps.cpu >= 68.0);
    setPill({ node: $('diag-fan-pill') }, isFanCrit ? 'critical' : (isThrottled ? 'warning' : 'ok'),
      isFanCrit ? 'STALLED' : (isThrottled ? 'THROTTLED' : 'NORMAL'));

    // Overall status pill
    const openAlerts = Array.isArray(detail.open_alerts) ? detail.open_alerts : [];
    const hasHwAlert = openAlerts.some((a) => ['STORAGE_CRITICAL', 'STORAGE_WEAR_HIGH', 'FAN_STALLED', 'MEMORY_ECC_ERROR'].includes(a.code));
    setPill({ node: $('hw-diag-status') }, hasHwAlert ? 'critical' : 'ok', hasHwAlert ? 'Hardware Fault Detected' : 'All Diagnostics Normal');
  }

  // ------------------------------------------------------------------------------ remote update jobs
  function renderUpdateJobs(detail) {
    const jobs = Array.isArray(detail.update_jobs) ? detail.update_jobs : [];
    $('jobs-empty').hidden = jobs.length > 0;
    $('jobs-wrap').hidden = jobs.length === 0;

    syncChildren($('jobs-rows'), jobs, (j) => j.job_id,
      () => {
        const tr = el('tr');
        tr.setAttribute('role', 'row');
        const idCell = cell('bare', 'Job ID');
        const opCell = cell('bare', 'Operation');
        const verCell = cell('bare', 'Target');
        const statusCell = cell('', 'Status');
        const pill = makePill();
        statusCell.append(pill.node);
        const progCell = cell('num', 'Progress');
        const progWrap = el('span', 'progress-bar-wrap');
        const progFill = el('span', 'progress-bar-fill');
        progWrap.append(progFill);
        const progText = el('span', 'prog-text');
        progCell.append(progWrap, progText);
        const timeCell = cell('num', 'Created');
        const msgCell = cell('wide bare', 'Message');
        const actCell = cell('', 'Action');
        const cancelBtn = el('button', 'btn btn-sm btn-danger', 'Cancel');
        actCell.append(cancelBtn);
        tr.append(idCell, opCell, verCell, statusCell, progCell, timeCell, msgCell, actCell);
        tr.parts = { idCell, opCell, verCell, pill, progFill, progText, timeCell, msgCell, cancelBtn };
        return tr;
      },
      (tr, j) => {
        const p = tr.parts;
        setText(p.idCell, j.job_id);
        setText(p.opCell, j.operation);
        setText(p.verCell, j.target_version || DASH);

        const st = (j.status || 'scheduled').toLowerCase();
        const tone = st === 'completed' ? 'ok' : st === 'failed' ? 'critical' : st === 'cancelled' ? 'off' : 'info';
        setPill(p.pill, tone, st.toUpperCase());

        const pct = Math.max(0, Math.min(100, j.progress_pct || 0));
        p.progFill.style.width = `${pct}%`;
        p.progFill.className = `progress-bar-fill ${st}`;
        setText(p.progText, `${pct}%`);

        const t = parseTime(j.created_at);
        setText(p.timeCell, t ? fmtAgo(Math.max(0, (Date.now() - t) / 1000)) : j.created_at);
        setAttr(p.timeCell, 'title', fmtFullTime(t));

        setText(p.msgCell, j.message || (j.error_details ? `Error: ${j.error_details}` : DASH));

        const canCancel = ['scheduled', 'dispatched', 'in_progress'].includes(st);
        p.cancelBtn.disabled = !canCancel;
        p.cancelBtn.onclick = async () => {
          const token = prompt('Enter admin bearer token to cancel this job:');
          if (!token) return;
          try {
            await postJSON(`updates/jobs/${encodeURIComponent(j.job_id)}/cancel`, {}, token);
            announce(`Job ${j.job_id} cancelled`);
            refresh();
          } catch (err) {
            alert(`Error: ${err.message}`);
          }
        };
      }
    );
  }

  // ------------------------------------------------------------------------------ wire update & config editors
  let cachedAdminToken = '';

  function wireUpdateJobs() {
    const openBtn = $('btn-open-dispatch');
    const closeBtn = $('btn-close-dispatch');
    const formWrap = $('dispatch-form-wrap');
    const form = $('update-dispatch-form');
    if (!openBtn || !form) return;

    openBtn.addEventListener('click', () => { formWrap.hidden = false; $('update-target-ver').focus(); });
    closeBtn.addEventListener('click', () => { formWrap.hidden = true; });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const statusMsg = $('dispatch-status-msg');
      statusMsg.className = 'status-msg';
      setText(statusMsg, 'Submitting update…');

      const op = $('update-op').value;
      const ver = $('update-target-ver').value.trim() || null;
      const schedTime = $('update-schedule-time').value;
      const maintWin = $('update-maint-window').value.trim() || null;
      const dryRun = $('update-dry-run').checked;
      const token = $('update-admin-token').value.trim();
      if (token) cachedAdminToken = token;

      const deviceId = device.id;
      const payload = { dry_run: dryRun };

      try {
        if (schedTime) {
          const isoTime = new Date(schedTime).toISOString();
          await postJSON('updates/schedules', {
            name: `${op} on ${deviceId}`,
            device_id: deviceId,
            operation: op,
            target_version: ver,
            payload,
            scheduled_at: isoTime,
            maintenance_window: maintWin,
          }, token);
          statusMsg.className = 'status-msg success';
          setText(statusMsg, 'Schedule created successfully!');
        } else {
          await postJSON('updates/jobs', {
            device_id: deviceId,
            operation: op,
            target_version: ver,
            payload,
          }, token);
          statusMsg.className = 'status-msg success';
          setText(statusMsg, 'Job dispatched successfully!');
        }
        setTimeout(() => {
          formWrap.hidden = true;
          setText(statusMsg, '');
          refresh();
        }, 1200);
      } catch (err) {
        statusMsg.className = 'status-msg error';
        setText(statusMsg, `Failed: ${err.message}`);
      }
    });
  }

  function wireConfigEditor() {
    const editBtn = $('btn-edit-config');
    const closeBtn = $('btn-close-config');
    const wrap = $('config-editor-wrap');
    const form = $('config-editor-form');
    const scopeSelect = $('cfg-scope-type');
    const scopeIdInput = $('cfg-scope-id');
    const jsonEditor = $('cfg-json-editor');
    const revPreview = $('cfg-rev-preview');
    const statusMsg = $('cfg-status-msg');
    const deleteBtn = $('btn-delete-config');
    if (!editBtn || !form) return;

    function updateScopeId() {
      const type = scopeSelect.value;
      const dev = (device.detail && device.detail.device) || {};
      if (type === 'device') scopeIdInput.value = device.id || '';
      else if (type === 'school') scopeIdInput.value = dev.site_code || 'school-default';
      else if (type === 'org') scopeIdInput.value = dev.customer || 'org-default';
      else scopeIdInput.value = 'default';
    }

    async function updateRevHash() {
      try {
        const parsed = JSON.parse(jsonEditor.value);
        const canon = JSON.stringify(parsed, Object.keys(parsed).sort());
        const hashBuf = await window.crypto.subtle.digest('SHA-256', new TextEncoder().encode(canon));
        const hashArr = Array.from(new Uint8Array(hashBuf));
        const hex = hashArr.map((b) => b.toString(16).padStart(2, '0')).join('');
        setText(revPreview, `Computed Revision Hash: ${hex.slice(0, 16)}…`);
      } catch (_) {
        setText(revPreview, 'Computed Revision Hash: (invalid JSON)');
      }
    }

    editBtn.addEventListener('click', () => {
      wrap.hidden = false;
      updateScopeId();
      if (cachedAdminToken && $('cfg-admin-token')) $('cfg-admin-token').value = cachedAdminToken;
      const curConfig = (device.detail && device.detail.effective_config && device.detail.effective_config.config) || {};
      jsonEditor.value = JSON.stringify(curConfig, null, 2);
      updateRevHash();
      jsonEditor.focus();
    });

    closeBtn.addEventListener('click', () => { wrap.hidden = true; });
    scopeSelect.addEventListener('change', updateScopeId);
    jsonEditor.addEventListener('input', updateRevHash);

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      statusMsg.className = 'status-msg';
      setText(statusMsg, 'Validating and saving…');

      let parsedConfig;
      try {
        parsedConfig = JSON.parse(jsonEditor.value);
        if (!parsedConfig || typeof parsedConfig !== 'object' || Array.isArray(parsedConfig)) {
          throw new Error('Config root must be a JSON object');
        }
      } catch (err) {
        statusMsg.className = 'status-msg error';
        setText(statusMsg, `Invalid JSON: ${err.message}`);
        return;
      }

      const scopeType = scopeSelect.value;
      const scopeId = scopeIdInput.value;
      const token = $('cfg-admin-token').value.trim();
      if (token) cachedAdminToken = token;

      if (!confirm(`Apply configuration updates to scope "${scopeType}:${scopeId}"?`)) {
        setText(statusMsg, 'Update cancelled');
        return;
      }

      try {
        await postJSON('config', {
          target_type: scopeType,
          target_id: scopeId,
          config: parsedConfig,
        }, token);
        statusMsg.className = 'status-msg success';
        setText(statusMsg, 'Configuration saved and active!');
        setTimeout(() => {
          wrap.hidden = true;
          setText(statusMsg, '');
          refresh();
        }, 1200);
      } catch (err) {
        statusMsg.className = 'status-msg error';
        setText(statusMsg, `Save failed: ${err.message}`);
      }
    });

    deleteBtn.addEventListener('click', async () => {
      const scopeType = scopeSelect.value;
      const scopeId = scopeIdInput.value;
      const token = $('cfg-admin-token').value.trim();
      if (!confirm(`Delete overrides for "${scopeType}:${scopeId}" to inherit parent scope settings?`)) return;
      statusMsg.className = 'status-msg';
      setText(statusMsg, 'Resetting scope…');
      try {
        await postJSON('config', {
          target_type: scopeType,
          target_id: scopeId,
          action: 'delete',
        }, token);
        statusMsg.className = 'status-msg success';
        setText(statusMsg, 'Overrides deleted.');
        setTimeout(() => {
          wrap.hidden = true;
          setText(statusMsg, '');
          refresh();
        }, 1000);
      } catch (err) {
        statusMsg.className = 'status-msg error';
        setText(statusMsg, `Reset failed: ${err.message}`);
      }
    });
  }

  function createLogRow() {
    const tr = el('tr');
    tr.setAttribute('role', 'row');
    const time = cell('ev-time', 'Time');
    const service = cell('bare', 'Service');
    const category = cell('bare', 'Category');
    const severity = cell('', 'Severity');
    const pill = makePill();
    severity.append(pill.node);
    const message = cell('wide bare', 'Message');
    const title = el('span', 'cell-title');
    const sub = el('span', 'cell-sub');
    message.append(title, sub);
    tr.append(time, service, category, severity, message);
    tr.parts = { time, service, category, pill, title, sub };
    return tr;
  }

  function updateLogRow(tr, log) {
    const p = tr.parts;
    const at = parseTime(log.ts);
    setText(p.time, at == null ? DASH : fmtWhen(at, true));
    setAttr(p.time, 'title', fmtFullTime(at));
    setText(p.service, log.service || DASH);
    setText(p.category, log.category || DASH);
    const sev = (log.severity || 'info').toLowerCase();
    const tone = sev === 'critical' ? 'critical' : (sev === 'warning' || sev === 'warn') ? 'warning' : 'info';
    setPill(p.pill, tone, sev.toUpperCase());
    setText(p.title, log.message || DASH);
    const attrs = log.attributes && typeof log.attributes === 'object' && Object.keys(log.attributes).length
      ? JSON.stringify(log.attributes) : '';
    setText(p.sub, attrs);
    p.sub.hidden = !attrs;
  }

  function renderLogs(detail) {
    const allLogs = Array.isArray(detail.recent_logs) ? detail.recent_logs : [];
    const catFilter = $('log-cat-filter') ? $('log-cat-filter').value : '';
    const sevFilter = $('log-sev-filter') ? $('log-sev-filter').value.toLowerCase() : '';
    const filtered = allLogs.filter((l) => {
      if (catFilter && l.category !== catFilter) return false;
      if (sevFilter && (l.severity || '').toLowerCase() !== sevFilter) return false;
      return true;
    });
    syncChildren($('logs-rows'), filtered, (l, i) => l.event_id || i, createLogRow, updateLogRow);
    $('logs-empty').hidden = filtered.length > 0;
    $('logs-wrap').hidden = filtered.length === 0;
  }

  // ------------------------------------------------------------------------------ charts

  const CHART_HEIGHT = 180;
  const RANGE_TITLE = { 60: 'Last 60 minutes', 360: 'Last 6 hours', 1440: 'Last 24 hours', 10080: 'Last 7 days' };
  const CHARTS = [
    { title: 'CPU use', unit: '%', digits: 1, fixed: [0, 100], series: [{ key: 'cpu_pct', label: 'CPU' }] },
    { title: 'GPU use', unit: '%', digits: 1, fixed: [0, 100], series: [{ key: 'gpu_pct', label: 'GPU' }] },
    {
      title: 'Temperatures', unit: '°C', digits: 1, span: 10, callout: 'hottest',
      series: [{ key: 'max_temp_c', label: 'Hottest sensor' }, { key: 'cpu_temp_c', label: 'CPU' }, { key: 'gpu_temp_c', label: 'GPU' }],
    },
    { title: 'Power draw', note: 'Whole board', unit: 'W', digits: 1, zero: true, span: 10, series: [{ key: 'power_total_w', label: 'Power' }] },
    { title: 'RAM use', unit: '%', digits: 1, fixed: [0, 100], series: [{ key: 'ram_pct', label: 'RAM' }] },
    {
      title: 'Network latency', note: 'Round trip to the gateway', unit: 'ms', digits: 1, zero: true, span: 10,
      series: [{ key: 'gateway_rtt_ms', label: 'Gateway' }],
    },
  ];
  const READINGS = [
    { key: 'cpu_pct', label: 'CPU', unit: '%', digits: 1 },
    { key: 'gpu_pct', label: 'GPU', unit: '%', digits: 1 },
    { key: 'max_temp_c', label: 'Hottest', unit: '°C', digits: 1 },
    { key: 'cpu_temp_c', label: 'CPU temp', unit: '°C', digits: 1 },
    { key: 'gpu_temp_c', label: 'GPU temp', unit: '°C', digits: 1 },
    { key: 'ram_pct', label: 'RAM', unit: '%', digits: 1 },
    { key: 'disk_pct', label: 'Disk', unit: '%', digits: 1 },
    { key: 'power_total_w', label: 'Power', unit: 'W', digits: 1 },
    { key: 'fan_rpm', label: 'Fan', unit: 'rpm', digits: 0 },
    { key: 'gateway_rtt_ms', label: 'Gateway', unit: 'ms', digits: 1 },
    { key: 'gateway_loss_pct', label: 'Loss', unit: '%', digits: 0 },
    { key: 'wifi_signal_dbm', label: 'Wi-Fi', unit: 'dBm', digits: 0 },
  ];
  const READINGS_SHOWN = 60;
  // A reading the box kept in its offline buffer and delivered after the connection came back
  // (in an averaged bucket: the share of such readings).
  const isLate = (p) => isNum(p.buffered) && p.buffered > 0;

  function buildCharts() {
    const root = $('charts');
    device.charts = CHARTS.map((cfg) => {
      const figure = el('figure', 'card chart');
      const head = el('figcaption', 'chart-head');
      const titles = el('span', 'chart-titles');
      titles.append(el('span', 'chart-title', cfg.title));
      if (cfg.note) titles.append(el('span', 'chart-note', cfg.note));
      const now = el('span', 'chart-now');
      const value = el('span', 'chart-value', DASH);
      const when = el('span', 'chart-when');
      now.append(value, when);
      head.append(titles, now);
      figure.append(head);

      const legend = [];
      if (cfg.series.length > 1) {
        const box = el('div', 'chart-legend');
        cfg.series.forEach((s, i) => {
          const item = el('span', 'legend-item');
          const key = el('span', `key s${i + 1}`);
          key.setAttribute('aria-hidden', 'true');
          const reading = el('b', 'legend-value', DASH);
          item.append(key, el('span', null, s.label), reading);
          box.append(item);
          legend.push(reading);
        });
        figure.append(box);
      }

      const plot = el('div', 'chart-plot');
      const svg = svgEl('svg', { class: 'chart-svg', role: 'img', tabindex: '0', 'aria-label': cfg.title });
      const tip = el('div', 'chart-tip');
      tip.hidden = true;
      tip.setAttribute('aria-hidden', 'true');
      plot.append(svg, tip);
      figure.append(plot);
      root.append(figure);

      const chart = { cfg, value, when, legend, plot, svg, tip, model: null, cross: null, hoverDots: [], hoverTime: null };
      wireChart(chart);
      return chart;
    });
  }

  function niceStep(raw) {
    if (!(raw > 0)) return 1;
    const power = 10 ** Math.floor(Math.log10(raw));
    const f = raw / power;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * power;
  }

  /** Value axis: fixed for percentages, from zero for magnitudes, centred on the data otherwise. */
  function yScale(cfg, lo, hi) {
    if (cfg.fixed) {
      const [a, b] = cfg.fixed;
      return { min: a, max: b, step: (b - a) / 2, ticks: [a, (a + b) / 2, b] };
    }
    const span = cfg.span || 1;
    let a;
    let b;
    if (lo == null) {
      a = 0;
      b = span;
    } else if (cfg.zero) {
      a = Math.min(0, lo);
      b = Math.max(hi, a + span);
    } else {
      const mid = (lo + hi) / 2;
      a = Math.min(lo, mid - span / 2);
      b = Math.max(hi, mid + span / 2);
    }
    const step = niceStep((b - a) / 3);
    const min = Math.floor(a / step) * step;
    let max = Math.ceil(b / step) * step;
    if (max <= min) max = min + step;
    const ticks = [];
    for (let v = min; v <= max + step / 2; v += step) ticks.push(Number(v.toFixed(6)));
    return { min, max, step, ticks };
  }

  const TIME_STEPS_MIN = [1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880];

  /** Time axis ticks on round local times ("16:15", "16:30", or a weekday at midnight). */
  function timeTicks(t0, t1, px) {
    const span = t1 - t0;
    if (!(span > 0)) return [];
    const most = Math.max(2, Math.floor(px / 64));
    const stepMin = TIME_STEPS_MIN.find((s) => span / (s * 60000) <= most) || TIME_STEPS_MIN[TIME_STEPS_MIN.length - 1];
    const step = stepMin * 60000;
    const d = new Date(t0);
    d.setSeconds(0, 0);
    if (stepMin < 60) {
      d.setMinutes(Math.ceil(d.getMinutes() / stepMin) * stepMin);
    } else if (stepMin < 1440) {
      d.setMinutes(0);
      d.setHours(Math.ceil(d.getHours() / (stepMin / 60)) * (stepMin / 60));
    } else {
      d.setHours(0, 0, 0, 0);
    }
    let t = d.getTime();
    while (t < t0) t += step;
    const ticks = [];
    for (; t <= t1 && ticks.length < 50; t += step) {
      const at = new Date(t);
      const midnight = at.getHours() === 0 && at.getMinutes() === 0;
      const label = stepMin >= 1440 || (midnight && stepMin >= 60) ? fmtAxisDay.format(at) : fmtHM.format(at);
      ticks.push({ t, label });
    }
    return ticks;
  }

  /**
   * A line breaks where readings are missing for longer than the server's offline rule allows
   * (CONTRACT.md section 6: max(3 × interval_s, 90 s)), or for more than 1.5 averaging buckets when
   * the server averaged a long range into 720 points. A hole in a chart means the box was silent.
   */
  function gapLimit(minutes) {
    const interval = obj(device.detail && device.detail.status).interval_s;
    const offlineMs = Math.max(3 * (isNum(interval) && interval > 0 ? interval : 60), 90) * 1000;
    return Math.max(offlineMs, (1.5 * minutes * 60000) / 720);
  }

  function segmentsOf(times, values, gap) {
    const segments = [];
    let current = null;
    let previous = null;
    for (let i = 0; i < values.length; i++) {
      if (values[i] === null) {
        current = null;
        continue;
      }
      if (!current || times[i] - previous > gap) {
        current = [];
        segments.push(current);
      }
      current.push(i);
      previous = times[i];
    }
    return segments;
  }

  function nearestIndex(times, t) {
    if (!times.length) return -1;
    let lo = 0;
    let hi = times.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (times[mid] < t) lo = mid + 1;
      else hi = mid;
    }
    if (lo > 0 && Math.abs(times[lo - 1] - t) <= Math.abs(times[lo] - t)) return lo - 1;
    return lo;
  }

  function tickLabel(value, step, unit) {
    return fmtValue(value, unit, step < 1 ? (step < 0.1 ? 2 : 1) : 0);
  }

  function renderCharts() {
    const minutes = device.minutes;
    setText($('charts-title'), RANGE_TITLE[minutes] || `Last ${minutes} minutes`);
    const points = device.metrics && Array.isArray(device.metrics.points) ? device.metrics.points : null;
    const rows = [];
    if (points) {
      for (const p of points) {
        const t = p && typeof p === 'object' ? parseTime(p.ts) : null;
        if (t != null) rows.push({ t, p });
      }
      rows.sort((a, b) => a.t - b.t);
    }
    device.rows = rows;
    const t1 = Math.max(serverNow(), rows.length ? rows[rows.length - 1].t : 0);
    const t0 = t1 - minutes * 60000;
    for (const chart of device.charts) drawChart(chart, rows, t0, t1, points === null);
    renderReadings();
  }

  function drawChart(chart, rows, t0, t1, loading) {
    const { cfg, svg } = chart;
    const width = Math.round(chart.plot.clientWidth);
    if (!width) return; // the view is hidden; the resize observer draws it once it is shown
    const height = CHART_HEIGHT;

    const series = cfg.series.map((s, i) => {
      const values = rows.map((r) => (isNum(r.p[s.key]) ? r.p[s.key] : null));
      let min = null;
      let max = null;
      let lastIndex = -1;
      values.forEach((v, j) => {
        if (v === null) return;
        min = min === null ? v : Math.min(min, v);
        max = max === null ? v : Math.max(max, v);
        lastIndex = j;
      });
      return { ...s, n: i + 1, values, min, max, lastIndex, last: lastIndex >= 0 ? values[lastIndex] : null };
    });
    const present = series.filter((s) => s.lastIndex >= 0);
    const scale = yScale(cfg, present.length ? Math.min(...present.map((s) => s.min)) : null,
      present.length ? Math.max(...present.map((s) => s.max)) : null);
    const labels = scale.ticks.map((v) => tickLabel(v, scale.step, cfg.unit));
    const m = { top: 10, right: 12, bottom: 24, left: Math.max(34, 12 + 7 * Math.max(...labels.map((l) => l.length))) };
    const iw = Math.max(10, width - m.left - m.right);
    const ih = height - m.top - m.bottom;
    const times = rows.map((r) => r.t);
    const x = (t) => m.left + ((t - t0) / (t1 - t0)) * iw;
    const y = (v) => m.top + (1 - (v - scale.min) / (scale.max - scale.min)) * ih;

    const nodes = [];
    const axes = svgEl('g', { 'aria-hidden': 'true' });
    scale.ticks.forEach((v, i) => {
      const yy = Math.round(y(v)) + 0.5;
      svgEl('line', { class: i === 0 ? 'baseline' : 'gridline', x1: m.left, x2: width - m.right, y1: yy, y2: yy }, axes);
      svgEl('text', { class: 'tick', x: m.left - 8, y: yy, dy: '0.32em', 'text-anchor': 'end' }, axes).textContent = labels[i];
    });
    for (const tick of timeTicks(t0, t1, iw)) {
      const xx = x(tick.t);
      const half = (tick.label.length * 6.6) / 2; // estimated width of a 12px label
      if (xx - half < 0 || xx + half > width) continue; // centred labels only, none sticking out
      svgEl('text', { class: 'tick', x: xx.toFixed(1), y: height - 6, 'text-anchor': 'middle' }, axes).textContent = tick.label;
    }
    nodes.push(axes);

    // Shaded bands where the readings arrived late: the connection to the server was down then.
    const bands = svgEl('g', { 'aria-hidden': 'true' });
    let first = null;
    rows.forEach((r, i) => {
      const late = isLate(r.p);
      if (late && first === null) first = i;
      if (first !== null && (!late || i === rows.length - 1)) {
        const last = late ? i : i - 1;
        const x0 = Math.max(m.left, x(times[first]) - 2);
        const x1 = Math.min(width - m.right, x(times[last]) + 2);
        if (x1 > x0) svgEl('rect', { class: 'late', x: x0.toFixed(1), y: m.top, width: (x1 - x0).toFixed(1), height: ih }, bands);
        first = null;
      }
    });
    if (bands.firstChild) nodes.push(bands);

    const marks = svgEl('g', { 'aria-hidden': 'true' });
    const gap = gapLimit(device.minutes);
    const base = y(scale.min).toFixed(1);
    const pt = (j, s) => `${x(times[j]).toFixed(1)},${y(s.values[j]).toFixed(1)}`;
    for (const s of series.slice().reverse()) { // series 1 is drawn last, on top
      let line = '';
      let area = '';
      for (const seg of segmentsOf(times, s.values, gap)) {
        if (seg.length === 1) {
          svgEl('circle', { class: `lone s${s.n}`, cx: x(times[seg[0]]).toFixed(1), cy: y(s.values[seg[0]]).toFixed(1), r: 2.5 }, marks);
          continue;
        }
        const pts = seg.map((j) => pt(j, s)).join('L');
        line += `M${pts}`;
        if (series.length === 1) {
          area += `M${x(times[seg[0]]).toFixed(1)},${base}L${pts}L${x(times[seg[seg.length - 1]]).toFixed(1)},${base}Z`;
        }
      }
      if (area) svgEl('path', { class: 'area', d: area }, marks);
      if (line) svgEl('path', { class: `line s${s.n}`, d: line }, marks);
    }
    for (const s of series.slice().reverse()) {
      if (s.lastIndex >= 0) {
        svgEl('circle', { class: `dot s${s.n}`, cx: x(times[s.lastIndex]).toFixed(1), cy: y(s.last).toFixed(1), r: 4 }, marks);
      }
    }
    nodes.push(marks);

    if (!present.length) {
      const note = svgEl('text', { class: 'empty-note', x: m.left + iw / 2, y: m.top + ih / 2, dy: '0.32em', 'text-anchor': 'middle' });
      note.textContent = loading ? 'Loading…' : 'No readings in this period';
      nodes.push(note);
    }

    const cross = svgEl('line', { class: 'cross', x1: 0, x2: 0, y1: m.top, y2: m.top + ih, visibility: 'hidden' });
    const hoverDots = series.map((s) => svgEl('circle', { class: `dot s${s.n}`, cx: 0, cy: 0, r: 4, visibility: 'hidden' }));
    nodes.push(cross, ...hoverDots);

    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.replaceChildren(...nodes);
    chart.model = { times, series, x, y, m, iw, width, t0, t1 };
    chart.cross = cross;
    chart.hoverDots = hoverDots;

    updateCallout(chart, series, times, loading);
    updateSummary(chart, present, loading);
    if (chart.hoverTime != null && times.length) showHover(chart, nearestIndex(times, chart.hoverTime));
    else hideHover(chart);
  }

  function updateCallout(chart, series, times, loading) {
    const { cfg } = chart;
    const first = series[0];
    if (first.lastIndex < 0) {
      setText(chart.value, DASH);
      setText(chart.when, loading ? '' : 'no readings');
    } else {
      const interval = isNum(obj(device.detail && device.detail.status).interval_s) ? device.detail.status.interval_s : 60;
      const at = times[first.lastIndex];
      const fresh = (serverNow() - at) / 1000 <= Math.max(3 * interval, 90);
      setText(chart.value, fmtValue(first.last, cfg.unit, cfg.digits));
      setText(chart.when, `${cfg.callout ? `${cfg.callout}, ` : ''}${fresh ? 'now' : `at ${fmtWhen(at)}`}`);
    }
    chart.legend.forEach((node, i) => {
      const s = series[i];
      setText(node, s && s.lastIndex >= 0 ? fmtValue(s.last, cfg.unit, cfg.digits) : DASH);
    });
  }

  function updateSummary(chart, present, loading) {
    const { cfg } = chart;
    const range = (RANGE_TITLE[device.minutes] || '').toLowerCase();
    let text;
    if (loading) text = `${cfg.title}: loading.`;
    else if (!present.length) text = `${cfg.title}, ${range}: no readings.`;
    else {
      const fmt = (v) => fmtValue(v, cfg.unit, cfg.digits);
      text = `${cfg.title}, ${range}. ${present.map((s) => `${s.label}: latest ${fmt(s.last)}, lowest ${fmt(s.min)}, highest ${fmt(s.max)}`).join('; ')}.`;
    }
    setAttr(chart.svg, 'aria-label', text);
  }

  function showHover(chart, index) {
    const model = chart.model;
    if (!model || index < 0 || index >= model.times.length) return;
    const t = model.times[index];
    const xx = model.x(t);
    chart.cross.setAttribute('x1', xx.toFixed(1));
    chart.cross.setAttribute('x2', xx.toFixed(1));
    chart.cross.setAttribute('visibility', 'visible');
    const lines = [];
    model.series.forEach((s, k) => {
      const v = s.values[index];
      const dot = chart.hoverDots[k];
      if (v === null) {
        dot.setAttribute('visibility', 'hidden');
      } else {
        dot.setAttribute('cx', xx.toFixed(1));
        dot.setAttribute('cy', model.y(v).toFixed(1));
        dot.setAttribute('visibility', 'visible');
      }
      const line = el('div', 'tip-row');
      const key = el('span', `key s${s.n}`);
      line.append(key, el('b', null, fmtValue(v, chart.cfg.unit, chart.cfg.digits)), el('span', 'tip-label', s.label));
      lines.push(line);
    });
    const when = device.minutes > 1440 ? fmtDayHM.format(new Date(t)) : fmtHMS.format(new Date(t));
    chart.tip.replaceChildren(el('div', 'tip-time', when), ...lines);
    chart.tip.hidden = false;
    const plotWidth = chart.plot.clientWidth;
    const px = xx * (plotWidth / model.width);
    const w = chart.tip.offsetWidth;
    let left = px + 12;
    if (left + w > plotWidth) left = px - 12 - w;
    chart.tip.style.left = `${Math.max(0, Math.round(left))}px`;
  }

  function hideHover(chart) {
    chart.hoverTime = null;
    chart.tip.hidden = true;
    if (chart.cross) chart.cross.setAttribute('visibility', 'hidden');
    for (const dot of chart.hoverDots) dot.setAttribute('visibility', 'hidden');
  }

  const CHART_KEYS = { ArrowLeft: -1, ArrowRight: 1, PageUp: -10, PageDown: 10 };

  function focusVisible(node) {
    try {
      return node.matches(':focus-visible');
    } catch (err) {
      return true; // an older browser without :focus-visible
    }
  }

  function wireChart(chart) {
    const { svg } = chart;
    const hoverAt = (index) => {
      const model = chart.model;
      if (!model || !model.times.length) return;
      const i = Math.max(0, Math.min(model.times.length - 1, index));
      chart.hoverTime = model.times[i];
      showHover(chart, i);
    };
    const fromPointer = (event) => {
      const model = chart.model;
      if (!model || !model.times.length) return;
      const box = svg.getBoundingClientRect();
      const px = ((event.clientX - box.left) / box.width) * model.width;
      const t = model.t0 + ((px - model.m.left) / model.iw) * (model.t1 - model.t0);
      hoverAt(nearestIndex(model.times, t));
    };
    const currentIndex = () => {
      const model = chart.model;
      return chart.hoverTime != null ? nearestIndex(model.times, chart.hoverTime) : model.times.length - 1;
    };
    svg.addEventListener('pointermove', fromPointer);
    svg.addEventListener('pointerdown', fromPointer);
    svg.addEventListener('pointerleave', (event) => {
      if (event.pointerType !== 'touch') hideHover(chart); // a lifted finger keeps the reading shown
    });
    svg.addEventListener('blur', () => hideHover(chart));
    svg.addEventListener('focus', () => {
      if (focusVisible(svg) && chart.model && chart.model.times.length) hoverAt(currentIndex());
    });
    svg.addEventListener('keydown', (event) => {
      const model = chart.model;
      if (!model || !model.times.length) return;
      let index = currentIndex();
      if (Object.prototype.hasOwnProperty.call(CHART_KEYS, event.key)) index += CHART_KEYS[event.key];
      else if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = model.times.length - 1;
      else if (event.key === 'Escape') {
        hideHover(chart);
        return;
      } else return;
      event.preventDefault();
      hoverAt(index);
    });
  }

  function buildReadingsHead() {
    const head = $('readings-head');
    const th = (text, className) => {
      const node = el('th', className, text);
      node.scope = 'col';
      return node;
    };
    head.replaceChildren(th('Time'), ...READINGS.map((r) => th(r.label, 'num')));
  }

  function renderReadings() {
    if (!$('readings').open) return;
    const rows = device.rows;
    const shown = rows.slice(-READINGS_SHOWN).reverse();
    const withDay = device.minutes > 1440;
    syncChildren($('readings-rows'), shown, (r) => r.t, () => {
      const tr = el('tr');
      tr.append(el('td'), ...READINGS.map(() => el('td', 'num')));
      return tr;
    }, (tr, r) => {
      const cells = tr.children;
      const late = isLate(r.p);
      setClass(tr, late ? 'late' : '');
      setText(cells[0], `${(withDay ? fmtDayHM : fmtHMS).format(new Date(r.t))}${late ? ' · late' : ''}`);
      setAttr(cells[0], 'title', late ? "Delivered late from the box's offline buffer: the connection was down" : null);
      READINGS.forEach((c, i) => setText(cells[i + 1], fmtValue(r.p[c.key], c.unit, c.digits)));
    });
    setText($('readings-meta'), rows.length > READINGS_SHOWN
      ? `The latest ${READINGS_SHOWN} of ${rows.length} readings in this period. Times in your time zone.`
      : `${rows.length} ${plural(rows.length, 'reading')} in this period. Times in your time zone.`);
  }

  function wireDevice() {
    $('range').addEventListener('click', (event) => {
      const button = event.target.closest('button[data-minutes]');
      if (!button) return;
      const minutes = Number(button.dataset.minutes);
      if (minutes === device.minutes) return;
      device.minutes = minutes;
      for (const b of $('range').querySelectorAll('button')) setAttr(b, 'aria-pressed', String(b === button));
      for (const chart of device.charts) hideHover(chart);
      refresh();
    });
    $('readings').addEventListener('toggle', renderReadings);
    const catSelect = $('log-cat-filter');
    const sevSelect = $('log-sev-filter');
    if (catSelect) catSelect.addEventListener('change', () => { if (device.detail) renderLogs(device.detail); });
    if (sevSelect) sevSelect.addEventListener('change', () => { if (device.detail) renderLogs(device.detail); });
    wireUpdateJobs();
    wireConfigEditor();
  }

  // ------------------------------------------------------------------------------ data availability and stored readings

  const AVAIL_REFRESH_MS = 60000;
  const tzOffsetMin = () => -new Date().getTimezoneOffset();
  const fmtDayLabel = new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
  const history = { id: null, timer: 0, data: null, day: null, at: null, seq: 0 };

  /** 'YYYY-MM-DD' (a day in the viewer's time zone) -> that local midnight, in ms. */
  function localDayStart(day) {
    const [y, m, d] = day.split('-').map(Number);
    return new Date(y, m - 1, d).getTime();
  }

  function resetHistory(id) {
    clearTimeout(history.timer);
    Object.assign(history, { id, data: null, day: null, at: null, seq: history.seq + 1 });
    $('avail-rows').replaceChildren();
    $('gap-list').replaceChildren();
    $('avail-empty').hidden = true;
    $('gaps-empty').hidden = true;
    setText($('avail-meta'), 'Loading…');
    setText($('raw-meta'), '');
    $('raw-day').replaceChildren();
    $('raw-time').replaceChildren();
    setText($('raw-json'), '');
    loadAvailability(id);
  }

  /** Loaded when a device opens and every minute after; it isn't part of the 5 s refresh. */
  async function loadAvailability(id) {
    clearTimeout(history.timer);
    if (route.view !== 'device' || route.id !== id) return;
    const seq = history.seq;
    try {
      const data = await getJSON(`devices/${encodeURIComponent(id)}/availability?days=30&tz_offset_min=${tzOffsetMin()}`);
      if (seq !== history.seq) return;
      history.data = data;
      renderAvailability(data);
      fillRawDays(data);
    } catch (error) {
      if (seq !== history.seq) return;
      if (!history.data) setText($('avail-meta'), `Can't load the data availability. ${describeError(error)}`);
    }
    if (seq === history.seq) history.timer = setTimeout(() => loadAvailability(id), AVAIL_REFRESH_MS);
  }

  function renderAvailability(data) {
    const days = (Array.isArray(data.days) ? data.days : []).slice().reverse(); // newest first
    const gaps = Array.isArray(data.gaps) ? data.gaps : [];
    const first = parseTime(data.first_reading);
    const last = parseTime(data.last_reading);
    setText($('avail-meta'), first == null ? ''
      : `Readings from ${fmtFullTime(first)} to ${fmtFullTime(last)}. `
        + `A gap is ${fmtDuration(data.threshold_s)} or more without a reading.`);
    $('avail-empty').hidden = days.length > 0;
    const rows = $('avail-rows');
    rows.replaceChildren();
    for (const d of days) {
      const tr = el('tr');
      tr.append(el('td', null, fmtDayLabel.format(new Date(localDayStart(d.day)))));
      tr.append(el('td', 'num', fmtNumber(d.readings)));
      tr.append(el('td', null, fmtHMS.format(new Date(parseTime(d.first)))));
      tr.append(el('td', null, fmtHMS.format(new Date(parseTime(d.last)))));
      const td = el('td');
      td.append(coverageBar(d, gaps));
      tr.append(td);
      rows.append(tr);
    }
    const list = $('gap-list');
    list.replaceChildren(...gaps.slice().reverse().map(gapItem));
    $('gaps-empty').hidden = gaps.length > 0 || days.length === 0;
  }

  /** A midnight-to-midnight bar: green where readings were recorded, grey where there were none. */
  function coverageBar(d, gaps) {
    const W = 288;
    const H = 14;
    const start = localDayStart(d.day);
    const end = start + 86400000;
    let segs = [[parseTime(d.first), parseTime(d.last)]];
    for (const g of gaps) {
      const gs = parseTime(g.from);
      const ge = g.to ? parseTime(g.to) : serverNow();
      segs = segs.flatMap(([a, b]) => ((ge <= a || gs >= b) ? [[a, b]] : [[a, gs], [ge, b]].filter(([x, y]) => y > x)));
    }
    const x = (t) => Math.max(0, Math.min(W, ((t - start) / 86400000) * W));
    const svg = svgEl('svg', { class: 'cov-bar', viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', role: 'img' });
    svgEl('rect', { class: 'cov-bg', x: 0, y: 0, width: W, height: H }, svg);
    let covered = 0;
    for (const [a, b] of segs) {
      svgEl('rect', { class: 'cov-on', x: x(a), y: 0, width: Math.max(x(b) - x(a), 0.8), height: H }, svg);
      covered += Math.max(0, Math.min(b, end) - Math.max(a, start));
    }
    for (let h = 6; h < 24; h += 6) svgEl('line', { class: 'cov-tick', x1: h * 12, y1: 0, x2: h * 12, y2: H }, svg);
    const label = `Recorded for about ${fmtDuration(covered / 1000)} of this day`;
    svg.setAttribute('aria-label', label);
    svgEl('title', {}, svg).textContent = label;
    return svg;
  }

  function gapItem(g) {
    const from = parseTime(g.from);
    const to = g.to ? parseTime(g.to) : null;
    const li = el('li', g.ongoing ? 'gap-ongoing' : (g.restarted ? 'gap-restart' : null));
    li.append(el('span', 'gap-when',
      `${fmtFullTime(from)} → ${to == null ? 'now' : fmtFullTime(to)} · ${fmtDuration(g.duration_s)}`));
    let why;
    if (g.ongoing) why = 'No readings since then: the box is off, or the agent or the server is not running.';
    else if (g.restarted) why = `The box was switched off or restarting. It started again at ${fmtFullTime(parseTime(g.booted_at))}.`;
    else if (g.restarted === false) why = 'The box stayed on, but nothing was recorded: the agent or the server was stopped, or the data was lost.';
    else why = 'No readings were recorded; the reason is not known.';
    li.append(el('span', 'gap-why', why));
    return li;
  }

  function fillRawDays(data) {
    const select = $('raw-day');
    if (!data.raw_available) {
      select.disabled = true;
      $('raw-time').disabled = true;
      if (!history.at) setText($('raw-meta'), 'Plain-text copies are turned off on this server: only the latest reading can be shown.');
      return;
    }
    const days = (Array.isArray(data.days) ? data.days : []).map((d) => d.day).reverse();
    if (Array.from(select.options).map((o) => o.value).join('|') !== days.join('|')) {
      select.replaceChildren(...days.map((day) => {
        const option = el('option', null, fmtDayLabel.format(new Date(localDayStart(day))));
        option.value = day;
        return option;
      }));
      if (history.day && days.includes(history.day)) select.value = history.day;
    }
    if (!history.day && days.length) {
      select.value = days[0];
      loadRawDay(days[0]);
    }
  }

  async function loadRawDay(day) {
    history.day = day;
    const { id, seq } = history;
    setText($('raw-meta'), 'Loading the readings of this day…');
    try {
      const idx = await getJSON(`devices/${encodeURIComponent(id)}/raw?date=${day}&tz_offset_min=${tzOffsetMin()}`);
      if (seq !== history.seq || day !== history.day) return;
      const readings = (Array.isArray(idx.readings) ? idx.readings : []).slice().reverse(); // newest first
      $('raw-time').replaceChildren(...readings.map((r) => {
        const option = el('option', null,
          `${fmtHMS.format(new Date(parseTime(r.time)))}${r.buffered ? ' (arrived late)' : ''}`);
        option.value = r.time;
        return option;
      }));
      setText($('raw-meta'), `${fmtNumber(readings.length)} readings stored on this day. Pick one to see every field.`);
      if (readings.length) {
        $('raw-time').value = readings[0].time;
        loadRawReading(readings[0].time);
      } else {
        setText($('raw-json'), '');
      }
    } catch (error) {
      if (seq === history.seq) setText($('raw-meta'), `Can't load this day. ${describeError(error)}`);
    }
  }

  async function loadRawReading(at) {
    history.at = at;
    const { id, seq } = history;
    try {
      const one = await getJSON(`devices/${encodeURIComponent(id)}/raw?at=${encodeURIComponent(at)}`);
      if (seq !== history.seq || at !== history.at) return;
      setText($('raw-json'), JSON.stringify(one.reading, null, 2));
    } catch (error) {
      if (seq === history.seq) setText($('raw-json'), `Can't load this reading. ${describeError(error)}`);
    }
  }

  function wireHistory() {
    $('raw-day').addEventListener('change', (event) => loadRawDay(event.target.value));
    $('raw-time').addEventListener('change', (event) => loadRawReading(event.target.value));
    $('raw-latest').addEventListener('click', () => {
      history.at = null;
      const latest = device.detail ? device.detail.latest : null;
      if (!latest) {
        setText($('raw-json'), 'No reading has arrived yet.');
        return;
      }
      setText($('raw-json'), JSON.stringify(latest, null, 2));
      setText($('raw-meta'), `The latest reading, received ${fmtFullTime(parseTime(obj(device.detail.status).last_seen))}.`);
    });
  }

  // ------------------------------------------------------------------------------ start

  function init() {
    device.pill = pillInto($('device-state'), 'pill-lg');
    device.syncPill = pillInto($('sync-state'));
    buildCharts();
    buildReadingsHead();
    wireFleet();
    wireDevice();
    wireHistory();

    const zone = TZ_NAME ? ` (${TZ_NAME})` : '';
    setText($('charts-meta'), `Times in your time zone${zone}. Averaged when a range holds more than 720 readings. `
      + 'Shaded: readings that arrived late, after the connection to the server dropped.');
    setText($('events-time-head'), `Time${zone}`);

    $('skip-link').addEventListener('click', (event) => {
      event.preventDefault();
      $('main').focus();
    });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) stopRefresh();
      else refresh();
    });
    window.addEventListener('hashchange', () => applyRoute(false));

    let frame = 0;
    const redraw = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        if (route.view === 'device') renderCharts();
      });
    };
    if ('ResizeObserver' in window) new ResizeObserver(redraw).observe($('charts'));
    else window.addEventListener('resize', redraw);

    fleet.loadingTimer = setTimeout(() => {
      if (!fleet.data) showFleetEmpty('Loading devices…', '', false);
    }, LOADER_DELAY_MS);
    applyRoute(true);
  }

  init();
})();
