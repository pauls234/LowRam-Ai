/* LowRAM AI Studio – shared helpers (vanilla JS, no dependencies).
 *
 * Security: untrusted text (model output, file names, error messages, device
 * names) is only ever written with textContent / createElement – never with
 * innerHTML. Every POST carries Content-Type: application/json and the
 * per-process X-LowRAM-Token from <meta name="lowram-token">.
 */
(function () {
  'use strict';

  const LR = (window.LR = window.LR || {});
  const meta = document.querySelector('meta[name="lowram-token"]');
  LR.token = meta ? meta.getAttribute('content') || '' : '';
  LR.state = { features: null, status: null, settings: null, engine: null, catalog: null, memory: null };

  /* ------------------------------------------------------------ events */

  const listeners = new Map();
  LR.on = function (name, fn) {
    if (!listeners.has(name)) listeners.set(name, new Set());
    listeners.get(name).add(fn);
    return () => listeners.get(name).delete(fn);
  };
  LR.emit = function (name, payload) {
    const set = listeners.get(name);
    if (!set) return;
    for (const fn of [...set]) {
      try { fn(payload); } catch (err) { console.error(err); }
    }
  };

  /* ------------------------------------------------------------ storage (never throws) */

  LR.store = {
    get(key) {
      try { return window.localStorage.getItem(key); } catch (_) { return null; }
    },
    set(key, value) {
      try { window.localStorage.setItem(key, String(value)); } catch (_) { /* ignore */ }
    },
    remove(key) {
      try { window.localStorage.removeItem(key); } catch (_) { /* ignore */ }
    },
  };

  /* ------------------------------------------------------------ small utils */

  const isNum = (n) => typeof n === 'number' && Number.isFinite(n);
  const clamp = (n, min, max) => Math.min(max, Math.max(min, n));
  let uidCounter = 0;
  LR.isNum = isNum;
  LR.clamp = clamp;
  LR.uid = (prefix) => (prefix || 'lr') + '-' + (++uidCounter);
  LR.$ = (id) => document.getElementById(id);
  LR.reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  LR.str = (value, fallback) => (typeof value === 'string' ? value : value === null || value === undefined ? (fallback || '') : String(value));

  /* ------------------------------------------------------------ DOM building */

  function append(node, children) {
    if (children === undefined || children === null || children === false) return;
    if (Array.isArray(children)) {
      for (const child of children) append(node, child);
      return;
    }
    node.append(children instanceof Node ? children : String(children));
  }

  /** createElement helper. Text always goes through textContent / text nodes. */
  function h(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const key of Object.keys(attrs)) {
        const value = attrs[key];
        if (value === null || value === undefined || value === false) continue;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = String(value);
        else if (key === 'dataset') Object.assign(node.dataset, value);
        else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
        else if (key === 'value') node.value = value;
        else if (key === 'checked' || key === 'disabled' || key === 'hidden') node[key] = Boolean(value);
        else node.setAttribute(key, value === true ? '' : String(value));
      }
    }
    append(node, children);
    return node;
  }
  LR.h = h;
  LR.append = append;

  const SVGNS = 'http://www.w3.org/2000/svg';
  /** An icon from the sprite in index.html. */
  LR.icon = function (name, cls) {
    const svg = document.createElementNS(SVGNS, 'svg');
    svg.setAttribute('class', 'icon' + (cls ? ' ' + cls : ''));
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    const use = document.createElementNS(SVGNS, 'use');
    use.setAttribute('href', '#i-' + name);
    svg.append(use);
    return svg;
  };
  const icon = LR.icon;

  /* ------------------------------------------------------------ formatting */

  const GB = 1024 * 1024 * 1024;
  function parseTime(value) {
    if (!value) return null;
    if (value instanceof Date) return value;
    if (isNum(value)) return new Date(value > 1e12 ? value : value * 1000);
    let s = String(value).trim();
    // "YYYY-MM-DD HH:MM:SS" (SQLite) is UTC without a zone.
    if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(s)) s = s.replace(' ', 'T') + 'Z';
    const d = new Date(s);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  const fmt = {
    parseTime,
    bytes(n) {
      if (!isNum(n) || n < 0) return '—';
      const units = ['B', 'KB', 'MB', 'GB', 'TB'];
      let v = n;
      let i = 0;
      while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
      const digits = i === 0 ? 0 : v < 10 ? 1 : 0;
      return v.toFixed(digits) + ' ' + units[i];
    },
    gb(n) {
      if (!isNum(n)) return '—';
      const v = n / GB;
      return (v < 10 ? v.toFixed(1) : Math.round(v)) + ' GB';
    },
    ramGb(gb) {
      return isNum(gb) ? (Number.isInteger(gb) ? gb : gb.toFixed(1)) + ' GB' : '—';
    },
    speed(bps) {
      return isNum(bps) && bps > 0 ? fmt.bytes(bps) + '/s' : '';
    },
    duration(sec) {
      if (!isNum(sec) || sec < 0) return '';
      if (sec < 10) return sec.toFixed(1) + ' s';
      if (sec < 60) return Math.round(sec) + ' s';
      if (sec < 3600) {
        const m = Math.floor(sec / 60);
        const s = Math.round(sec % 60);
        return m + ' min' + (s ? ' ' + s + ' s' : '');
      }
      const hrs = Math.floor(sec / 3600);
      const m = Math.round((sec % 3600) / 60);
      return hrs + ' h' + (m ? ' ' + m + ' min' : '');
    },
    eta(sec) {
      if (!isNum(sec) || sec <= 0) return '';
      if (sec < 10) return LR.t('a few seconds left');
      if (sec < 60) return LR.t('about {t} left', { t: Math.round(sec / 5) * 5 + ' s' });
      if (sec < 3600) return LR.t('about {t} left', { t: Math.round(sec / 60) + ' min' });
      return LR.t('about {t} left', { t: fmt.duration(sec) });
    },
    clock(ms) {
      const total = Math.max(0, Math.floor(ms / 1000));
      const m = Math.floor(total / 60);
      return m + ':' + String(total % 60).padStart(2, '0');
    },
    time(value) {
      const d = parseTime(value);
      if (!d) return value ? String(value) : '';
      const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
      if (d.toDateString() === new Date().toDateString()) return time;
      return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ', ' + time;
    },
    dateLong(value) {
      const d = parseTime(value);
      if (!d) return value ? String(value) : '';
      return d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    },
    relative(value) {
      const d = parseTime(value);
      if (!d) return '';
      const sec = (Date.now() - d.getTime()) / 1000;
      if (sec < 45) return LR.t('just now');
      if (sec < 3600) return LR.t('{n} min ago', { n: Math.max(1, Math.round(sec / 60)) });
      if (sec < 86400) return LR.t('{n} h ago', { n: Math.round(sec / 3600) });
      return fmt.time(value);
    },
    plural(n, word, many) {
      return LR.plural(n, word, many);
    },
    int(n) {
      return isNum(n) ? Math.round(n).toLocaleString(LR.locale || 'en-US') : '—';
    },
  };
  LR.fmt = fmt;

  /* ------------------------------------------------------------ API */

  class ApiError extends Error {
    constructor(message, status, data, network) {
      super(message);
      this.name = 'ApiError';
      this.status = status || 0;
      this.data = data || null;
      this.network = Boolean(network);
    }
  }
  LR.ApiError = ApiError;

  let backendDown = false;
  LR.isBackendDown = () => backendDown;
  LR.setBackendDown = function (down) {
    if (backendDown === down) return;
    backendDown = down;
    LR.emit('backend', { down });
  };

  async function send(method, path, body, signal) {
    const init = {
      method,
      headers: { Accept: 'application/json' },
      cache: 'no-store',
      credentials: 'same-origin',
      signal,
    };
    if (method !== 'GET') {
      init.headers['Content-Type'] = 'application/json';
      init.headers['X-LowRAM-Token'] = LR.token;
      init.body = JSON.stringify(body || {});
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (err) {
      if (err && err.name === 'AbortError') throw err;
      LR.setBackendDown(true);
      throw new ApiError(LR.t('LowRAM AI is not reachable right now.'), 0, null, true);
    }
    LR.setBackendDown(false);
    return res;
  }

  async function errorFrom(res) {
    let data = null;
    try {
      const text = await res.text();
      data = text ? JSON.parse(text) : null;
    } catch (_) {
      data = null;
    }
    const message = data && typeof data.error === 'string' && data.error
      ? data.error
      : LR.t('Request failed (HTTP ') + res.status + ').';
    if (res.status === 403 && /token/i.test(message)) LR.emit('token-rejected');
    return new ApiError(message, res.status, data);
  }

  async function api(method, path, body, opts) {
    const res = await send(method, path, body, opts && opts.signal);
    if (!res.ok) throw await errorFrom(res);
    let data;
    try {
      data = await res.json();
    } catch (_) {
      throw new ApiError(LR.t('Unexpected response from LowRAM AI.'), res.status);
    }
    if (!data || typeof data !== 'object') throw new ApiError(LR.t('Unexpected response from LowRAM AI.'), res.status);
    return data;
  }
  LR.api = api;
  LR.get = (path, opts) => api('GET', path, null, opts);
  LR.post = (path, body, opts) => api('POST', path, body, opts);

  /**
   * POST and consume a text/event-stream answer (EventSource cannot POST).
   * JSON errors sent before the stream starts are thrown as ApiError.
   */
  LR.stream = async function (path, body, opts) {
    const res = await send('POST', path, body, opts.signal);
    if (!res.ok) throw await errorFrom(res);
    const type = res.headers.get('Content-Type') || '';
    if (!type.includes('text/event-stream') || !res.body) {
      let data = null;
      try { data = await res.json(); } catch (_) { data = null; }
      if (data && typeof data.error === 'string') throw new ApiError(data.error, res.status, data);
      throw new ApiError(LR.t('Expected a streamed answer from LowRAM AI.'), res.status, data);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    const dispatch = (chunk) => {
      const lines = [];
      for (const line of chunk.split('\n')) {
        if (line.startsWith('data:')) lines.push(line.slice(5).replace(/^ /, ''));
      }
      if (!lines.length) return;
      let event;
      try { event = JSON.parse(lines.join('\n')); } catch (_) { return; }
      if (event && typeof event === 'object') opts.onEvent(event);
    };
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true }).replace(/\r/g, '');
        let index;
        while ((index = buffer.indexOf('\n\n')) !== -1) {
          const chunk = buffer.slice(0, index);
          buffer = buffer.slice(index + 2);
          dispatch(chunk);
        }
      }
    } catch (err) {
      if (err && err.name === 'AbortError') throw err;
      throw new ApiError(LR.t('The connection to LowRAM AI was interrupted.'), 0, null, true);
    }
    buffer += decoder.decode();
    if (buffer.trim()) dispatch(buffer);
  };

  /* ------------------------------------------------------------ features */

  LR.has = (feature) => Boolean(LR.state.features && LR.state.features.has(feature));

  /* ------------------------------------------------------------ live region & toasts */

  LR.announce = function (text) {
    const live = LR.$('live');
    if (!live) return;
    live.textContent = '';
    window.setTimeout(() => { live.textContent = text; }, 60);
  };

  LR.toast = function (text, tone) {
    const region = LR.$('toasts');
    if (!region) return;
    const el = h('div', { class: 'toast' + (tone ? ' toast-' + tone : '') }, [
      icon(tone === 'error' ? 'alert' : tone === 'info' ? 'info' : 'check'),
      h('span', { text }),
    ]);
    region.append(el);
    while (region.children.length > 3) region.firstElementChild.remove();
    window.requestAnimationFrame(() => el.classList.add('is-in'));
    window.setTimeout(() => {
      el.classList.remove('is-in');
      window.setTimeout(() => el.remove(), 260);
    }, tone === 'error' ? 5000 : 2600);
  };

  /* ------------------------------------------------------------ clipboard */

  LR.copy = async function (text) {
    try {
      if (!navigator.clipboard) throw new Error('no clipboard');
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      const ta = h('textarea', { class: 'clipboard-helper', readonly: true, 'aria-hidden': 'true', tabindex: '-1' });
      ta.value = text;
      document.body.append(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (__) { ok = false; }
      ta.remove();
      return ok;
    }
  };

  /** A button that copies getText() and briefly confirms with a check mark. */
  LR.copyButton = function (opts) {
    const label = opts.label || LR.t('Copy');
    const textEl = opts.iconOnly ? null : h('span', { text: label });
    const btn = h('button', {
      type: 'button',
      class: opts.cls || 'btn btn-ghost btn-sm',
      'aria-label': opts.ariaLabel || label,
      title: opts.iconOnly ? opts.ariaLabel || label : null,
    }, [icon('copy'), textEl]);
    let timer = 0;
    btn.addEventListener('click', async () => {
      const ok = await LR.copy(opts.getText());
      btn.replaceChildren(...[icon(ok ? 'check' : 'alert'), opts.iconOnly ? null : h('span', { text: ok ? LR.t('Copied') : LR.t('Copy failed') })].filter(Boolean));
      btn.classList.toggle('is-done', ok);
      if (opts.iconOnly) LR.announce(ok ? LR.t('Copied to clipboard') : LR.t('Copy failed'));
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        btn.replaceChildren(...[icon('copy'), textEl].filter(Boolean));
        btn.classList.remove('is-done');
      }, 1600);
    });
    return btn;
  };

  /* ------------------------------------------------------------ building blocks */

  const TONE_ICON = { info: 'info', ok: 'check', warn: 'alert', error: 'alert', accent: 'sparkle' };

  LR.button = function (o) {
    return h('button', {
      type: o.type || 'button',
      class: 'btn btn-' + (o.variant || 'secondary') + (o.size ? ' btn-' + o.size : '') + (o.cls ? ' ' + o.cls : ''),
      disabled: o.disabled,
      'aria-label': o.label || null,
      title: o.title || null,
      onclick: o.onclick,
      dataset: o.dataset,
    }, [o.icon ? icon(o.icon) : null, o.text ? h('span', { text: o.text }) : null]);
  };

  LR.setBusy = function (btn, busy, busyText) {
    if (!btn) return;
    if (busy) {
      if (!btn.dataset.idle) btn.dataset.idle = btn.textContent;
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
      btn.classList.add('is-busy');
      if (busyText) {
        const span = btn.querySelector('span');
        if (span) span.textContent = busyText;
      }
    } else {
      btn.disabled = false;
      btn.removeAttribute('aria-busy');
      btn.classList.remove('is-busy');
      const span = btn.querySelector('span');
      if (span && btn.dataset.idle) span.textContent = btn.dataset.idle;
      delete btn.dataset.idle;
    }
  };

  LR.badge = (text, tone) => h('span', { class: 'badge' + (tone ? ' badge-' + tone : ''), text });
  LR.dot = (tone) => h('span', { class: 'dot dot-' + (tone || 'idle'), 'aria-hidden': 'true' });
  LR.spinner = (cls) => h('span', { class: 'spinner' + (cls ? ' ' + cls : ''), 'aria-hidden': 'true' });

  LR.callout = function (o) {
    const tone = o.tone || 'info';
    return h('div', { class: 'callout callout-' + tone + (o.cls ? ' ' + o.cls : ''), role: o.role || null }, [
      icon(o.icon || TONE_ICON[tone] || 'info', 'callout-icon'),
      h('div', { class: 'callout-body' }, [
        o.title ? h('p', { class: 'callout-title', text: o.title }) : null,
        o.text ? h('p', { class: 'callout-text', text: o.text }) : null,
        o.extra || null,
        o.actions && o.actions.length ? h('div', { class: 'callout-actions' }, o.actions) : null,
      ]),
    ]);
  };

  LR.empty = function (o) {
    return h('div', { class: 'empty' + (o.cls ? ' ' + o.cls : '') }, [
      o.icon ? h('div', { class: 'empty-icon', 'aria-hidden': 'true' }, icon(o.icon)) : null,
      h(o.headingTag || 'p', { class: 'empty-title', text: o.title }),
      o.text ? h('p', { class: 'empty-text', text: o.text }) : null,
      o.actions && o.actions.length ? h('div', { class: 'empty-actions' }, o.actions) : null,
    ]);
  };

  LR.skeleton = function (lines, cls) {
    const group = h('div', { class: 'skeleton-group' + (cls ? ' ' + cls : ''), 'aria-hidden': 'true' });
    for (let i = 0; i < lines; i++) group.append(h('div', { class: 'skeleton sk-' + (i % 4) }));
    return group;
  };

  LR.unavailable = function (name, text) {
    return LR.empty({
      icon: 'info',
      title: name + LR.t(' is not available in this build'),
      text: text || LR.t('This version of LowRAM AI was built without this part. Everything else keeps working.'),
      cls: 'empty-page',
    });
  };

  /** Determinate/indeterminate progress bar; value is a fraction 0..1 or null. */
  LR.progress = function (opts) {
    const o = opts || {};
    const fill = h('span', { class: 'progress-fill' });
    const el = h('div', {
      class: 'progress' + (o.size ? ' progress-' + o.size : '') + (o.cls ? ' ' + o.cls : ''),
      role: 'progressbar',
      'aria-valuemin': '0',
      'aria-valuemax': '100',
      'aria-label': o.label || null,
    }, fill);
    function set(fraction, text) {
      if (!isNum(fraction)) {
        el.classList.add('is-indeterminate');
        el.removeAttribute('aria-valuenow');
      } else {
        const pct = clamp(fraction * 100, 0, 100);
        el.classList.remove('is-indeterminate');
        el.style.setProperty('--value', pct.toFixed(1) + '%');
        el.setAttribute('aria-valuenow', String(Math.round(pct)));
      }
      if (text) el.setAttribute('aria-valuetext', text);
      else el.removeAttribute('aria-valuetext');
    }
    set(o.value);
    return { el, set };
  };

  /** Accessible segmented control (radiogroup with roving tabindex). */
  LR.segmented = function (o) {
    let value = o.value;
    const group = h('div', { class: 'segmented' + (o.cls ? ' ' + o.cls : ''), role: 'radiogroup', 'aria-label': o.label });
    const buttons = o.options.map((opt) => h('button', {
      type: 'button',
      role: 'radio',
      class: 'seg',
      dataset: { value: opt.value },
      title: opt.title || null,
      onclick: () => set(opt.value, true),
    }, [opt.icon ? icon(opt.icon) : null, h('span', { text: opt.label })]));
    group.append(...buttons);
    group.addEventListener('keydown', (ev) => {
      const keys = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
      if (!(ev.key in keys)) return;
      ev.preventDefault();
      const enabled = buttons.filter((b) => !b.disabled);
      const idx = enabled.findIndex((b) => b.dataset.value === value);
      const next = enabled[(idx + keys[ev.key] + enabled.length) % enabled.length];
      if (next) {
        set(next.dataset.value, true);
        next.focus();
      }
    });
    function set(v, user) {
      const changed = v !== value;
      value = v;
      for (const b of buttons) {
        const on = b.dataset.value === v;
        b.setAttribute('aria-checked', String(on));
        b.tabIndex = on ? 0 : -1;
      }
      if (user && changed && o.onChange) o.onChange(v);
    }
    set(value, false);
    return {
      el: group,
      set: (v) => set(v, false),
      get: () => value,
      setDisabled(disabled, title) {
        for (const b of buttons) b.disabled = disabled;
        if (title) group.title = title;
        else group.removeAttribute('title');
      },
    };
  };

  /** A labelled on/off switch row (native checkbox with role="switch"). */
  LR.switchRow = function (o) {
    const id = LR.uid('switch');
    const descId = o.description ? id + '-desc' : null;
    const input = h('input', { type: 'checkbox', role: 'switch', class: 'switch', id, 'aria-describedby': descId });
    input.checked = Boolean(o.checked);
    input.addEventListener('change', () => { if (o.onChange) o.onChange(input.checked, input); });
    const row = h('div', { class: 'switch-row' }, [
      h('div', { class: 'switch-text' }, [
        h('label', { class: 'switch-label', for: id, text: o.label }),
        o.description ? h('p', { class: 'switch-desc', id: descId, text: o.description }) : null,
      ]),
      input,
    ]);
    return { el: row, input };
  };

  LR.field = function (o) {
    const id = o.id || LR.uid('field');
    const hintId = o.hint ? id + '-hint' : null;
    const errId = id + '-err';
    const control = o.control;
    control.id = id;
    if (hintId) control.setAttribute('aria-describedby', hintId);
    const error = h('p', { class: 'field-error', id: errId, hidden: true });
    const el = h('div', { class: 'field' + (o.cls ? ' ' + o.cls : '') }, [
      h('label', { class: 'label', for: id }, [o.label, o.optional ? h('span', { class: 'label-hint', text: ' optional' }) : null]),
      control,
      o.hint ? h('p', { class: 'help', id: hintId, text: o.hint }) : null,
      error,
    ]);
    return {
      el,
      control,
      setError(message) {
        error.textContent = message || '';
        error.hidden = !message;
        if (message) {
          control.setAttribute('aria-invalid', 'true');
          control.setAttribute('aria-errormessage', errId);
        } else {
          control.removeAttribute('aria-invalid');
          control.removeAttribute('aria-errormessage');
        }
      },
    };
  };

  /** Live "m:ss" timer inside el; returns a stop function. */
  LR.elapsed = function (el, startMs) {
    const start = startMs || Date.now();
    const tick = () => { el.textContent = fmt.clock(Date.now() - start); };
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  };

  /* ------------------------------------------------------------ dialogs */

  LR.modal = function (o) {
    const titleId = LR.uid('dlg');
    const dlg = h('dialog', { class: 'dialog' + (o.size ? ' dialog-' + o.size : '') + (o.cls ? ' ' + o.cls : ''), 'aria-labelledby': titleId });
    const titleEl = h('h2', { class: 'dialog-title', id: titleId, text: o.title });
    const subEl = h('p', { class: 'dialog-sub', text: o.sub || '', hidden: !o.sub });
    const body = h('div', { class: 'dialog-body' }, o.body);
    const foot = h('div', { class: 'dialog-foot', hidden: !(o.actions && o.actions.length) }, o.actions || null);
    const inner = h('div', { class: 'dialog-inner' }, [
      h('div', { class: 'dialog-head' }, [
        h('div', { class: 'dialog-heading' }, [titleEl, subEl]),
        h('button', { type: 'button', class: 'btn btn-ghost btn-icon dialog-x', 'aria-label': LR.t('Close'), onclick: () => close() }, icon('x')),
      ]),
      body,
      foot,
    ]);
    dlg.append(inner);
    document.body.append(dlg);
    const returnFocus = document.activeElement;
    let closed = false;
    function close() {
      if (closed) return;
      closed = true;
      dlg.classList.add('is-closing');
      window.setTimeout(() => dlg.close(), LR.reducedMotion() ? 0 : 140);
    }
    dlg.addEventListener('cancel', (ev) => {
      ev.preventDefault();
      close();
    });
    dlg.addEventListener('close', () => {
      closed = true;
      dlg.remove();
      if (o.onClose) o.onClose();
      if (returnFocus && returnFocus.isConnected && typeof returnFocus.focus === 'function') returnFocus.focus();
    });
    dlg.addEventListener('mousedown', (ev) => {
      if (ev.target === dlg) close();
    });
    dlg.showModal();
    return {
      el: dlg,
      body,
      foot,
      close,
      setTitle(text) { titleEl.textContent = text; },
      setSub(text) { subEl.textContent = text || ''; subEl.hidden = !text; },
      setActions(actions) { foot.replaceChildren(...actions); foot.hidden = !actions.length; },
    };
  };

  LR.confirm = function (o) {
    return new Promise((resolve) => {
      let result = false;
      const cancel = LR.button({ text: o.cancelText || LR.t('Cancel'), variant: 'secondary', onclick: () => m.close() });
      const ok = LR.button({
        text: o.confirmText || LR.t('Confirm'),
        variant: o.danger ? 'danger' : 'primary',
        onclick: () => { result = true; m.close(); },
      });
      const m = LR.modal({
        title: o.title,
        size: 'sm',
        body: o.message ? h('p', { class: 'dialog-text', text: o.message }) : null,
        actions: [cancel, ok],
        onClose: () => resolve(result),
      });
      (o.danger ? cancel : ok).focus();
    });
  };

  LR.prompt = function (o) {
    return new Promise((resolve) => {
      let result = null;
      const control = o.multiline
        ? h('textarea', { class: 'input textarea', rows: '3', placeholder: o.placeholder || '', maxlength: o.maxlength || null })
        : h('input', { class: 'input', type: 'text', placeholder: o.placeholder || '', maxlength: o.maxlength || null, autocomplete: 'off' });
      control.value = o.value || '';
      const field = LR.field({ label: o.label, control, hint: o.hint, optional: o.optional });
      const form = h('form', { class: 'form', novalidate: true }, [o.message ? h('p', { class: 'dialog-text', text: o.message }) : null, field.el]);
      const submit = () => {
        const value = control.value.trim();
        if (o.required && !value) {
          field.setError(o.requiredText || LR.t('This field is required.'));
          control.focus();
          return;
        }
        result = value;
        m.close();
      };
      form.addEventListener('submit', (ev) => { ev.preventDefault(); submit(); });
      if (!o.multiline) control.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); submit(); } });
      const m = LR.modal({
        title: o.title,
        size: 'sm',
        body: form,
        actions: [
          LR.button({ text: LR.t('Cancel'), variant: 'secondary', onclick: () => m.close() }),
          LR.button({ text: o.confirmText || LR.t('Save'), variant: o.danger ? 'danger' : 'primary', onclick: submit }),
        ],
        onClose: () => resolve(result),
      });
      control.focus();
      if (control.select) control.select();
    });
  };

  /* ------------------------------------------------------------ polling */

  /** Repeats fn (async) with a delay; fn may return the next delay in ms. */
  LR.poller = function (fn, interval) {
    let timer = 0;
    let active = false;
    let inFlight = false;
    let again = false;
    async function tick() {
      timer = 0;
      if (!active) return;
      inFlight = true;
      let next = null;
      try { next = await fn(); } catch (_) { next = null; }
      inFlight = false;
      if (!active) return;
      if (again) {
        again = false;
        tick();
        return;
      }
      let ms = isNum(next) ? next : typeof interval === 'function' ? interval() : interval;
      if (document.hidden) ms = Math.max(ms, 15000);
      timer = window.setTimeout(tick, ms);
    }
    return {
      start() {
        if (active) return;
        active = true;
        tick();
      },
      stop() {
        active = false;
        window.clearTimeout(timer);
        timer = 0;
      },
      now() {
        if (!active) return;
        if (inFlight) {
          again = true;
          return;
        }
        window.clearTimeout(timer);
        tick();
      },
      get active() { return active; },
    };
  };

  /* ------------------------------------------------------------ views */

  LR.views = new Map();
  /** def = {title, create() -> Element, show(params), hide()} */
  LR.registerView = (name, def) => LR.views.set(name, def);
  LR.go = function (hash) {
    if (window.location.hash === hash) LR.emit('route-refresh');
    else window.location.hash = hash;
  };
  LR.setTitle = function (text) {
    const el = LR.$('topbar-title');
    if (el) el.textContent = text;
    document.title = text && text !== 'LowRAM AI' ? text + ' · LowRAM AI' : 'LowRAM AI';
  };

  /** Shared page header for non-chat views. */
  LR.pageHead = function (o) {
    return h('header', { class: 'page-head' }, [
      h('div', { class: 'page-heading' }, [
        h('h1', { class: 'page-title', text: o.title }),
        o.sub ? h('p', { class: 'page-sub', text: o.sub }) : null,
      ]),
      o.actions && o.actions.length ? h('div', { class: 'page-actions' }, o.actions) : null,
    ]);
  };

  /** Model display name from the engine catalog (falls back to the id). */
  LR.modelName = function (id) {
    if (!id) return '';
    const models = LR.state.catalog && Array.isArray(LR.state.catalog.models) ? LR.state.catalog.models : [];
    const found = models.find((m) => m.id === id);
    return found && found.name ? found.name : String(id);
  };

  /**
   * Model self-heal: an install answered 409 {suggestion: {filename, size_bytes}}
   * when the catalog file is gone. Offers the suggested file; resolves with the
   * retried response, or null when declined / not applicable.
   */
  LR.offerSuggestion = async function (err, path) {
    const sug = err && err.status === 409 && err.data && err.data.suggestion;
    if (!sug || typeof sug.filename !== 'string' || !sug.filename) return null;
    const ok = await LR.confirm({
      title: LR.t('File not found – install {file} ({size}) instead?', { file: sug.filename, size: fmt.bytes(sug.size_bytes) }),
      message: LR.t('The model file is no longer available under its old name. LowRAM AI found a matching file in the same repository.'),
      confirmText: LR.t('Install instead'),
    });
    if (!ok) return null;
    return LR.post(path, { filename: sug.filename });
  };
})();
