/* LowRAM AI Studio – application shell: routing, sidebar / drawer, status
 * pill, banners (backend down, stale token, startup errors), theme and the
 * first-run redirect. Views live in chat.js, code.js, media.js, tasks.js,
 * devices.js, settings.js and setup.js. */
(function () {
  'use strict';

  const LR = window.LR;
  const h = LR.h;
  const icon = LR.icon;
  const fmt = LR.fmt;
  const FEATURE_OF = { chat: 'chat', code: 'api_coding', media: 'media', devices: 'cluster' };
  const app = { route: null, params: [], mounted: new Map(), drawer: false, tokenRejected: false, startupDismissed: false, retryAt: 0 };
  LR.currentRoute = () => app.route;

  /* ------------------------------------------------------------ theme */

  LR.setTheme = function (theme) {
    const t = theme === 'light' || theme === 'dark' ? theme : 'system';
    if (t === 'system') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', t);
    LR.store.set('lowram.theme', t);
    LR.emit('theme', t);
  };

  LR.loadSettings = async function () {
    const data = await LR.get('/api/settings');
    LR.state.settings = data.settings || {};
    const ui = LR.state.settings.ui || {};
    LR.setTheme(ui.theme || 'system');
    if (typeof ui.language === 'string' && LR.applyLanguagePref) LR.applyLanguagePref(ui.language);
    LR.emit('settings', LR.state.settings);
    return LR.state.settings;
  };

  LR.loadCatalog = async function () {
    if (!LR.has('engine')) return null;
    const data = await LR.get('/api/engine/catalog');
    LR.state.catalog = data;
    LR.emit('catalog', data);
    return data;
  };

  LR.budgetPercent = () => {
    const s = LR.state.settings;
    return s && LR.isNum(s.budget_percent) ? s.budget_percent : 40;
  };

  /* ------------------------------------------------------------ routing */

  function parseHash() {
    const raw = window.location.hash.replace(/^#\/?/, '');
    const parts = raw.split('/').filter(Boolean).map((p) => {
      try { return decodeURIComponent(p); } catch (_) { return p; }
    });
    let name = parts[0] || 'chat';
    if (!LR.views.has(name)) name = 'chat';
    return { name, params: parts.slice(1) };
  }

  function navigate() {
    const { name, params } = parseHash();
    const prev = app.route;
    if (prev && prev !== name) {
      const prevDef = LR.views.get(prev);
      if (prevDef.hide) prevDef.hide();
      const prevEl = app.mounted.get(prev);
      if (prevEl) prevEl.hidden = true;
    }
    const def = LR.views.get(name);
    let el = app.mounted.get(name);
    if (!el) {
      el = def.create();
      app.mounted.set(name, el);
      LR.$('main').append(el);
    }
    el.hidden = false;
    app.route = name;
    app.params = params;
    document.body.dataset.route = name;
    if (name !== 'chat') LR.setTitle(def.title);
    def.show(params);
    updateNav();
    closeDrawer(false);
    renderBanners();
    if (prev && prev !== name && name !== 'chat') {
      const heading = el.querySelector('h1');
      if (heading) {
        heading.setAttribute('tabindex', '-1');
        heading.focus({ preventScroll: true });
      }
    }
  }

  LR.updateNav = () => updateNav();
  function updateNav() {
    for (const link of document.querySelectorAll('.nav-link')) {
      const route = link.dataset.route;
      if (route === app.route || (app.route === 'setup' && route === 'settings')) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
      const feature = FEATURE_OF[route];
      const missing = Boolean(feature && LR.state.features && !LR.has(feature));
      if (route === 'knowledge' || route === 'diagnostics') link.parentElement.hidden = LR[route + 'Available'] === false || !LR.mayHave(route);
      link.classList.toggle('is-unavailable', missing);
      link.title = missing ? LR.t('Not available in this build') : '';
    }
    const chatOk = !LR.state.features || LR.has('chat');
    LR.$('new-chat').hidden = !chatOk;
    LR.$('history').hidden = !chatOk;
  }

  /* ------------------------------------------------------------ drawer (mobile) */

  const mobile = () => window.matchMedia('(max-width: 899px)').matches;

  function openDrawer() {
    if (!mobile()) return;
    app.drawer = true;
    document.body.classList.add('drawer-open');
    LR.$('scrim').hidden = false;
    LR.$('menu-btn').setAttribute('aria-expanded', 'true');
    document.querySelector('.main-col').inert = true;
    window.requestAnimationFrame(() => LR.$('sidebar-close').focus());
  }

  function closeDrawer(returnFocus) {
    if (!app.drawer) return;
    app.drawer = false;
    document.body.classList.remove('drawer-open');
    LR.$('scrim').hidden = true;
    LR.$('menu-btn').setAttribute('aria-expanded', 'false');
    document.querySelector('.main-col').inert = false;
    if (returnFocus) LR.$('menu-btn').focus();
  }

  /* ------------------------------------------------------------ status pill */

  function engineSummary() {
    const eng = LR.state.engine;
    if (!LR.has('engine') || !eng) return { tone: 'ok', text: LR.t('Local model') };
    if (eng.external) return { tone: 'ok', text: LR.t('External model') };
    const server = eng.server || {};
    const name = LR.modelName(server.model_id);
    if (eng.paused) return { tone: 'warn', text: LR.t('Paused for image generation') };
    switch (server.state) {
      case 'ready': return { tone: 'ok', text: name || LR.t('Model ready') };
      case 'starting': return { tone: 'warn', text: LR.t('Starting ') + (name || LR.t('the model')) + '…', pulse: true };
      case 'error': return { tone: 'error', text: LR.t('Model error') };
      default: {
        if (eng.unloaded_idle || server.unloaded_idle) return { tone: 'idle', text: LR.t('Model paused (loads with your next question)') };
        const anyInstalled = LR.state.catalog && Array.isArray(LR.state.catalog.models) && LR.state.catalog.models.some((m) => m.installed);
        return { tone: 'idle', text: eng.runtime && eng.runtime.installed && anyInstalled ? LR.t('Model stopped') : LR.t('Set up a model') };
      }
    }
  }

  const PRESSURE_TEXT = {
    ok: LR.t('Memory is fine'),
    warn: LR.t('Memory is tight – big tasks wait automatically'),
    critical: LR.t('Memory is very tight – big tasks wait automatically; closing other apps helps'),
  };

  const diagTarget = () => (LR.views.has('diagnostics') && LR.diagnosticsAvailable !== false ? '#/diagnostics' : '#/settings');

  function renderPill() {
    const dot = LR.$('status-dot');
    const text = LR.$('status-text');
    const ram = LR.$('status-ram');
    const pill = LR.$('status-pill');
    if (LR.isBackendDown()) {
      dot.className = 'dot dot-error';
      text.textContent = LR.t('Offline');
      ram.textContent = '';
      pill.setAttribute('aria-label', LR.t('LowRAM AI is offline. Open Models and Settings'));
      pill.dataset.target = diagTarget();
      return;
    }
    const s = engineSummary();
    const status = LR.state.status;
    const busy = status && status.busy ? String(status.busy) : '';
    dot.className = 'dot dot-' + (busy ? 'busy' : s.tone) + (s.pulse || busy ? ' is-pulsing' : '');
    text.textContent = s.text;
    const mem = LR.state.memory;
    const free = mem && LR.isNum(mem.available_bytes) ? fmt.gb(mem.available_bytes) + LR.t(' free') : '';
    // RAM pressure from the backend sampler (v0.4): ok / warn / critical.
    const level = mem && mem.pressure && ['ok', 'warn', 'critical'].includes(mem.pressure.level) ? mem.pressure.level : null;
    const pressureText = level ? PRESSURE_TEXT[level] : '';
    ram.replaceChildren(...[
      level && !busy ? h('span', { class: 'pressure-dot is-' + level, 'aria-hidden': 'true' }) : null,
      busy ? LR.t('Busy: ') + busy : free,
    ].filter(Boolean));
    ram.classList.toggle('is-busy', Boolean(busy));
    ram.classList.toggle('is-pressure', level === 'warn' || level === 'critical');
    pill.title = [s.text, free ? 'RAM ' + free : '', level && level !== 'ok' ? pressureText : '', busy ? LR.t('Busy: ') + busy : ''].filter(Boolean).join(' · ');
    pill.dataset.target = s.tone === 'error' ? diagTarget() : '#/settings';
    pill.setAttribute('aria-label', LR.t('Status: ') + pill.title + (pill.dataset.target === '#/diagnostics' ? LR.t('. Open Diagnostics') : LR.t('. Open Models and Settings')));
  }

  /* ------------------------------------------------------------ banners */

  LR.banner = (key, o) => banner(key, o);
  function banner(key, o) {
    const region = LR.$('banners');
    let el = region.querySelector('[data-key="' + key + '"]');
    if (!o) {
      if (el) el.remove();
      return;
    }
    const content = h('div', { class: 'banner-inner' }, [
      icon(o.icon || 'alert'),
      h('p', { class: 'banner-text' }, [h('strong', { text: o.title }), ' ', o.text ? h('span', { text: o.text }) : null]),
      o.actions ? h('div', { class: 'banner-actions' }, o.actions) : null,
    ]);
    if (!el) {
      el = h('div', { class: 'banner banner-' + (o.tone || 'warn'), dataset: { key }, role: o.tone === 'error' ? 'alert' : 'status' });
      region.append(el);
    }
    el.className = 'banner banner-' + (o.tone || 'warn');
    el.replaceChildren(content);
  }

  function renderBanners() {
    if (app.tokenRejected) {
      banner('token', {
        tone: 'error',
        title: LR.t('This page is out of date.'),
        text: LR.t('LowRAM AI was restarted, so this tab needs a reload to continue.'),
        actions: [LR.button({ text: LR.t('Reload'), icon: 'retry', size: 'sm', variant: 'primary', onclick: () => window.location.reload() })],
      });
    } else banner('token', null);

    if (LR.isBackendDown()) {
      const secs = Math.max(0, Math.ceil((app.retryAt - Date.now()) / 1000));
      banner('down', {
        tone: 'error',
        icon: 'wifi',
        title: LR.t('LowRAM AI is not reachable.'),
        text: LR.t('Is the app still running? Retrying automatically') + (secs ? ' in ' + secs + ' s' : '') + '…',
        actions: [
          LR.button({ text: LR.t('Retry now'), size: 'sm', onclick: () => statusPoller.now() }),
          diagTarget() === '#/diagnostics' ? LR.button({ text: LR.t('Open Diagnostics'), size: 'sm', variant: 'ghost', onclick: () => LR.go('#/diagnostics') }) : null,
        ].filter(Boolean),
      });
    } else banner('down', null);

    const errors = LR.state.status && Array.isArray(LR.state.status.startup_errors) ? LR.state.status.startup_errors : [];
    if (errors.length && !app.startupDismissed) {
      banner('startup', {
        tone: 'warn',
        title: LR.t('Some parts did not start:'),
        text: errors.join(' · '),
        actions: [
          diagTarget() === '#/diagnostics' ? LR.button({ text: LR.t('Open Diagnostics'), size: 'sm', onclick: () => LR.go('#/diagnostics') }) : null,LR.button({ text: LR.t('Dismiss'), size: 'sm', variant: 'ghost', onclick: () => { app.startupDismissed = true; renderBanners(); } })],
      });
    } else banner('startup', null);

    // Pairing requests from other computers: visible on every page (the Devices page shows them inline).
    const c = LR.state.cluster;
    const requests = c && Array.isArray(c.requests) ? c.requests : [];
    const devicesLink = document.querySelector('.nav-link[data-route="devices"]');
    let badge = devicesLink.querySelector('.nav-badge');
    if (requests.length && !badge) {
      badge = h('span', { class: 'nav-badge' });
      devicesLink.append(badge);
    }
    if (badge) {
      badge.textContent = requests.length ? String(requests.length) : '';
      badge.hidden = !requests.length;
      badge.setAttribute('aria-label', requests.length === 1 ? LR.t('1 pairing request') : LR.t('{n} pairing requests', { n: requests.length }));
    }
    const r = requests[0];
    if (r && app.route !== 'devices') {
      const key = 'pair-' + r.id + '-' + (r.verification_code || '');
      if (app.pairKey !== key) {
        app.pairKey = key;
        banner('pair', {
          tone: 'accent',
          icon: 'devices',
          title: LR.str(r.name, LR.t('A computer')) + LR.t(' wants to help.'),
          text: r.verification_code ? LR.t('Allow it only if it shows the number ') + LR.str(r.verification_code) + '.' : LR.t('Waiting for its verification number…'),
          actions: [
            LR.button({ text: LR.t('Deny'), size: 'sm', variant: 'ghost', onclick: (ev) => LR.cluster.answerRequest(r, false, ev.currentTarget) }),
            LR.button({ text: LR.t('Allow'), icon: 'check', size: 'sm', variant: 'primary', disabled: !r.verification_code, onclick: (ev) => LR.cluster.answerRequest(r, true, ev.currentTarget) }),
          ],
        });
      }
    } else {
      app.pairKey = null;
      banner('pair', null);
    }
  }

  /* ------------------------------------------------------------ polling */

  function applyFeatures(list) {
    const next = new Set(Array.isArray(list) ? list.map(String) : []);
    const prev = LR.state.features;
    const changed = !prev || prev.size !== next.size || [...next].some((f) => !prev.has(f));
    LR.state.features = next;
    if (changed) {
      updateNav();
      LR.emit('features', next);
    }
    return changed;
  }

  async function refreshStatus() {
    try {
      const status = await LR.get('/api/status');
      LR.state.status = status;
      const changed = applyFeatures(status.features);
      const v = LR.$('app-version');
      if (v && status.version) v.textContent = 'v' + status.version;
      const jobs = [
        LR.get('/api/memory?percent=' + encodeURIComponent(LR.budgetPercent())).then((m) => { LR.state.memory = m; LR.emit('memory', m); }).catch(() => {}),
      ];
      if (LR.has('cluster')) {
        jobs.push(LR.get('/api/cluster').then((c) => { LR.state.cluster = c.cluster || c; }).catch(() => {}));
      }
      if (LR.has('engine')) {
        jobs.push(LR.get('/api/engine/status').then((e) => { LR.state.engine = e; LR.emit('engine', e); }).catch(() => {}));
        if (!LR.state.catalog) jobs.push(LR.loadCatalog().catch(() => {}));
      }
      await Promise.all(jobs);
      LR.emit('status', status);
      if (changed && app.route) navigate();
    } catch (_) { /* banner shows the outage */ }
    renderPill();
    const eng = LR.state.engine;
    const delay = LR.isBackendDown() ? 3000
      : (LR.state.status && LR.state.status.busy) || (eng && eng.server && eng.server.state === 'starting') ? 2000 : 5000;
    app.retryAt = Date.now() + delay;
    renderBanners();
    return delay;
  }
  const statusPoller = LR.poller(refreshStatus, 5000);
  LR.refreshStatus = () => statusPoller.now();

  /* ------------------------------------------------------------ first run */

  async function needsSetup() {
    if (!LR.has('engine') || LR.store.get('lowram.setupSkipped') === '1') return false;
    try {
      const eng = LR.state.engine || await LR.get('/api/engine/status');
      if (eng.external) return false;
      if (eng.server && eng.server.state === 'ready') return false;
      const cat = LR.state.catalog || await LR.loadCatalog();
      const installed = cat && Array.isArray(cat.models) && cat.models.some((m) => m.installed);
      return !(eng.runtime && eng.runtime.installed) || !installed;
    } catch (_) {
      return false;
    }
  }

  /* ------------------------------------------------------------ boot */

  async function boot() {
    LR.chat.mountHistory(LR.$('history-body'));

    LR.$('menu-btn').addEventListener('click', openDrawer);
    LR.$('sidebar-close').addEventListener('click', () => closeDrawer(true));
    LR.$('scrim').addEventListener('click', () => closeDrawer(true));
    LR.$('status-pill').addEventListener('click', () => LR.go(LR.$('status-pill').dataset.target || '#/settings'));
    LR.$('new-chat').addEventListener('click', (ev) => {
      if (window.location.hash === '#/chat/new') {
        ev.preventDefault();
        navigate();
      }
    });
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && app.drawer) closeDrawer(true);
      if ((ev.metaKey || ev.ctrlKey) && ev.shiftKey && (ev.key === 'o' || ev.key === 'O')) {
        ev.preventDefault();
        LR.go('#/chat/new');
      }
    });
    window.addEventListener('resize', () => { if (!mobile()) closeDrawer(false); });
    window.addEventListener('hashchange', navigate);
    LR.on('route-refresh', navigate);
    LR.on('status-refresh', () => statusPoller.now());
    LR.on('backend', ({ down }) => {
      renderPill();
      renderBanners();
      if (!down) statusPoller.now();
    });
    LR.on('token-rejected', () => {
      app.tokenRejected = true;
      renderBanners();
    });
    window.setInterval(() => { if (LR.isBackendDown()) renderBanners(); }, 1000);

    LR.$('main').append(h('div', { class: 'boot' }, LR.skeleton(3)));
    await refreshStatus();
    try { await LR.loadSettings(); } catch (_) { /* defaults */ }
    const boot = LR.$('main').querySelector('.boot');
    if (boot) boot.remove();

    const hash = window.location.hash;
    if ((!hash || hash === '#' || hash === '#/' || hash === '#/chat') && (await needsSetup())) {
      window.history.replaceState(null, '', '#/setup');
    }
    navigate();
    statusPoller.start();
    LR.chat.refreshList();
    LR.on('features', () => LR.chat.refreshList());
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
