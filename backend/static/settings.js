/* LowRAM AI Studio – Models & Settings: engine, model catalog, downloads,
 * RAM budget, preferences and build information. */
(function () {
  'use strict';

  const LR = window.LR;
  const h = LR.h;
  const icon = LR.icon;
  const fmt = LR.fmt;
  const GB = 1024 * 1024 * 1024;
  const ui = {};
  let unmountDownloads = null;
  let budgetTimer = 0;
  let memSeq = 0;
  const offs = [];

  async function saveSetting(section, values, okText) {
    try {
      const data = await LR.post('/api/settings', { section, values });
      if (LR.state.settings) LR.state.settings[section] = data.values;
      if (okText) LR.toast(okText);
      return true;
    } catch (err) {
      LR.toast('Could not save: ' + err.message, 'error');
      return false;
    }
  }
  LR.saveSetting = saveSetting;

  function section(title, sub, id) {
    const headingId = LR.uid('sec');
    const body = h('div', { class: 'card-body' });
    const el = h('section', { class: 'card', id: id || null, 'aria-labelledby': headingId }, [
      h('div', { class: 'card-head' }, [
        h('div', null, [h('h2', { class: 'card-title', id: headingId, text: title }), sub ? h('p', { class: 'card-sub', text: sub }) : null]),
      ]),
      body,
    ]);
    return { el, body, head: el.firstElementChild };
  }

  /* ------------------------------------------------------------ engine */

  function renderEngine() {
    if (!ui.engine) return;
    const body = ui.engine.body;
    if (!LR.has('engine')) {
      body.replaceChildren(h('p', { class: 'muted', text: 'The model engine is not part of this build. LowRAM AI talks to the model server configured with LOWRAM_LLM_URL.' }));
      return;
    }
    const eng = LR.state.engine;
    if (!eng) {
      body.replaceChildren(LR.skeleton(2));
      return;
    }
    const server = eng.server || {};
    const runtime = eng.runtime || {};
    const out = [];
    if (eng.external) {
      out.push(LR.callout({ tone: 'info', title: 'Using an external model server', text: 'LOWRAM_LLM_URL is set, so LowRAM AI does not start its own engine. Endpoint: ' + LR.str(eng.llm_url) }));
      body.replaceChildren(...out);
      return;
    }
    const states = {
      ready: ['ok', 'Running'],
      starting: ['warn', 'Starting…'],
      error: ['error', 'Error'],
      stopped: ['idle', 'Stopped'],
    };
    const st = states[server.state] || states.stopped;
    const name = LR.modelName(server.model_id);
    let line = 'No model is running.';
    if (server.state === 'ready') line = (name || 'The model') + ' is ready.';
    else if (server.state === 'starting') line = 'Loading ' + (name || 'the model') + ' into memory – this can take a minute.';
    else if (server.state === 'error') line = 'The engine stopped with an error.';
    out.push(h('div', { class: 'engine-row' }, [
      h('span', { class: 'dot dot-' + st[0] + (server.state === 'starting' ? ' is-pulsing' : ''), 'aria-hidden': 'true' }),
      h('div', { class: 'engine-text' }, [h('p', { class: 'engine-state', text: line }), h('p', { class: 'muted small', text: st[1] + (server.port ? ' · port ' + server.port : '') + (server.ctx_size ? ' · context ' + fmt.int(server.ctx_size) + ' tokens' : '') + (server.started_at ? ' · since ' + fmt.time(server.started_at) : '') })]),
    ]));
    const facts = h('dl', { class: 'facts' }, [
      h('div', null, [h('dt', { text: 'Runtime' }), h('dd', { text: runtime.installed ? 'llama.cpp' + (runtime.version ? ' ' + runtime.version : '') + (runtime.source ? ' (' + runtime.source + ')' : '') : 'Not installed' })]),
      h('div', null, [h('dt', { text: 'Model' }), h('dd', { text: name || '—' })]),
    ]);
    out.push(facts);
    if (server.state === 'error' && server.error) {
      out.push(LR.callout({ tone: 'error', title: 'What went wrong', text: LR.str(server.error) + ' More details are in the LowRAM AI log (the window or terminal that runs the app).' }));
    }
    const actions = [];
    if (!runtime.installed) {
      actions.push(LR.button({ text: 'Install runtime', icon: 'download', variant: 'primary', onclick: (ev) => installRuntime(ev.currentTarget) }));
    }
    const installed = installedModels();
    if (server.state === 'ready' || server.state === 'starting') {
      actions.push(LR.button({ text: 'Stop engine', icon: 'power', onclick: (ev) => stopEngine(ev.currentTarget) }));
    } else if (runtime.installed && installed.length) {
      const preferred = (LR.state.settings && LR.state.settings.engine && LR.state.settings.engine.model_id) || installed[0].id;
      actions.push(LR.button({ text: server.state === 'error' ? 'Try again' : 'Start ' + LR.modelName(preferred), icon: 'play', variant: 'primary', onclick: (ev) => startEngine(preferred, ev.currentTarget) }));
    }
    if (actions.length) out.push(h('div', { class: 'row-actions' }, actions));
    body.replaceChildren(...out);
  }

  function installedModels() {
    const cat = LR.state.catalog;
    return cat && Array.isArray(cat.models) ? cat.models.filter((m) => m.installed) : [];
  }

  async function installRuntime(btn) {
    LR.setBusy(btn, true, 'Starting download…');
    try {
      await LR.post('/api/engine/runtime/install', {});
      LR.toast('Downloading the runtime');
      LR.downloads.refresh();
    } catch (err) {
      LR.toast('Could not start the download: ' + err.message, 'error');
    } finally {
      if (btn.isConnected) LR.setBusy(btn, false);
    }
  }

  async function startEngine(modelId, btn) {
    if (btn) LR.setBusy(btn, true, 'Starting…');
    try {
      const data = await LR.post('/api/engine/start', modelId ? { model_id: modelId } : {});
      if (LR.state.engine && data.server) LR.state.engine.server = data.server;
      if (modelId) saveSetting('engine', { model_id: modelId });
      LR.toast('Starting ' + LR.modelName(modelId || (data.server && data.server.model_id)) + '…', 'info');
    } catch (err) {
      LR.toast('Could not start the model: ' + err.message, 'error');
    } finally {
      if (btn && btn.isConnected) LR.setBusy(btn, false);
      LR.refreshStatus();
      renderEngine();
      renderModels();
    }
  }
  LR.startEngine = startEngine;

  async function stopEngine(btn) {
    LR.setBusy(btn, true, 'Stopping…');
    try {
      const data = await LR.post('/api/engine/stop', {});
      if (LR.state.engine && data.server) LR.state.engine.server = data.server;
      LR.toast('Engine stopped', 'info');
    } catch (err) {
      LR.toast('Could not stop the engine: ' + err.message, 'error');
    } finally {
      LR.refreshStatus();
      renderEngine();
      renderModels();
    }
  }

  /* ------------------------------------------------------------ models */

  function downloadFor(model) {
    return LR.downloads.list.find((d) => LR.downloads.isActive(d) && (d.kind === 'model' || d.kind === 'media-model') &&
      (String(d.id).includes(model.id) || (d.label && model.name && String(d.label).includes(model.name))));
  }
  LR.downloadFor = downloadFor;

  /** One catalog card; shared with the media view (kind: 'engine' | 'media'). */
  function modelCard(m, o) {
    const opts = o || {};
    const totalRam = opts.ramTotal || 0;
    const tooBig = totalRam && LR.isNum(m.min_ram_gb) && m.min_ram_gb * GB > totalRam;
    const active = Boolean(opts.activeId && opts.activeId === m.id);
    const badges = [];
    if (m.recommended) badges.push(LR.badge('Recommended', 'accent'));
    if (active) badges.push(LR.badge(opts.activeLabel || 'In use', 'ok'));
    else if (m.installed) badges.push(LR.badge('Installed', 'neutral'));
    if (m.purpose === 'code') badges.push(LR.badge('Code', 'info'));
    const facts = h('dl', { class: 'model-facts' }, [
      h('div', null, [h('dt', { text: 'Download' }), h('dd', { text: LR.isNum(m.size_bytes) ? fmt.bytes(m.size_bytes) : '—' })]),
      h('div', null, [h('dt', { text: 'Needs' }), h('dd', { text: LR.isNum(m.min_ram_gb) ? fmt.ramGb(m.min_ram_gb) + ' RAM' : '—' })]),
      m.params || m.quant ? h('div', null, [h('dt', { text: 'Size' }), h('dd', { text: [m.params, m.quant].filter(Boolean).join(' · ') })]) : null,
    ]);
    const actions = h('div', { class: 'model-actions' });
    const progressSlot = h('div', { class: 'model-progress' });
    const dl = downloadFor(m);
    if (dl) {
      const r = LR.downloads.row(dl, { label: 'Downloading ' + (m.name || m.id) });
      progressSlot.append(r.el);
    } else if (!m.installed) {
      actions.append(LR.button({ text: 'Install', icon: 'download', variant: tooBig ? 'secondary' : 'primary', size: 'sm', onclick: (ev) => opts.onInstall(m, ev.currentTarget) }));
    } else {
      if (!active && opts.onUse) actions.append(LR.button({ text: opts.useLabel || 'Use this model', icon: 'play', variant: 'primary', size: 'sm', onclick: (ev) => opts.onUse(m, ev.currentTarget) }));
      if (opts.extra) opts.extra(m, actions);
      if (opts.onDelete) actions.append(LR.button({ text: 'Delete', icon: 'trash', variant: 'ghost', size: 'sm', disabled: active, title: active ? 'Stop the model before deleting it' : null, onclick: (ev) => opts.onDelete(m, ev.currentTarget) }));
    }
    return h('article', { class: 'model-card' + (m.recommended ? ' is-recommended' : '') + (active ? ' is-active' : ''), 'aria-label': m.name || m.id }, [
      h('div', { class: 'model-head' }, [h('h3', { class: 'model-name', text: m.name || m.id }), h('div', { class: 'badges' }, badges)]),
      h('p', { class: 'model-family', text: [m.family, m.license].filter(Boolean).join(' · ') }),
      m.description ? h('p', { class: 'model-desc', text: m.description }) : null,
      facts,
      tooBig ? h('p', { class: 'model-warn' }, [icon('alert'), h('span', { text: 'Needs more memory than this computer has – it may be very slow or fail.' })]) : null,
      progressSlot,
      actions.childElementCount ? actions : null,
    ]);
  }
  LR.modelCard = modelCard;

  function renderModels() {
    if (!ui.models) return;
    const body = ui.models.body;
    if (!LR.has('engine')) {
      ui.models.el.hidden = true;
      return;
    }
    ui.models.el.hidden = false;
    const cat = LR.state.catalog;
    if (!cat) {
      body.replaceChildren(h('div', { class: 'model-grid' }, [LR.skeleton(5, 'card-skeleton'), LR.skeleton(5, 'card-skeleton')]));
      return;
    }
    const models = Array.isArray(cat.models) ? [...cat.models] : [];
    models.sort((a, b) => (b.recommended - a.recommended) || (b.installed - a.installed) || ((a.size_bytes || 0) - (b.size_bytes || 0)));
    const eng = LR.state.engine;
    const server = eng && eng.server ? eng.server : {};
    const activeId = server.state === 'ready' || server.state === 'starting' ? server.model_id : null;
    const codeId = LR.state.settings && LR.state.settings.engine ? LR.state.settings.engine.code_model_id : null;
    if (!models.length) {
      body.replaceChildren(LR.empty({ icon: 'cpu', title: 'No models in the catalog', text: 'This build does not list any downloadable models.' }));
      return;
    }
    body.replaceChildren(h('div', { class: 'model-grid' }, models.map((m) => modelCard(m, {
      ramTotal: cat.ram_total_bytes,
      activeId,
      onInstall: installModel,
      onUse: (model, btn) => startEngine(model.id, btn),
      onDelete: deleteModel,
      extra: (model, actions) => {
        if (model.purpose === 'code') {
          const isCode = codeId === model.id;
          actions.append(LR.button({
            text: isCode ? 'Code mode default' : 'Use for Code mode',
            icon: isCode ? 'check' : 'code',
            size: 'sm',
            variant: 'ghost',
            disabled: isCode,
            onclick: async () => {
              if (await saveSetting('engine', { code_model_id: model.id }, 'Code mode will use ' + (model.name || model.id))) renderModels();
            },
          }));
        }
      },
    }))));
  }

  async function installModel(m, btn) {
    LR.setBusy(btn, true, 'Starting…');
    try {
      await LR.post('/api/engine/models/' + encodeURIComponent(m.id) + '/install', {});
      LR.toast('Downloading ' + (m.name || m.id));
      await LR.downloads.refresh();
    } catch (err) {
      LR.toast('Could not start the download: ' + err.message, 'error');
      if (btn.isConnected) LR.setBusy(btn, false);
    }
  }

  async function deleteModel(m) {
    const ok = await LR.confirm({
      title: 'Delete ' + (m.name || m.id) + '?',
      message: 'This frees ' + fmt.bytes(m.size_bytes) + ' of disk space. You can download it again later.',
      confirmText: 'Delete model',
      danger: true,
    });
    if (!ok) return;
    try {
      await LR.post('/api/engine/models/' + encodeURIComponent(m.id) + '/delete', {});
      LR.toast((m.name || m.id) + ' deleted');
    } catch (err) {
      LR.toast('Could not delete: ' + err.message, 'error');
    } finally {
      await LR.loadCatalog().catch(() => {});
    }
  }

  /* ------------------------------------------------------------ memory */

  function renderMemory(mem) {
    if (!ui.mem || !mem) return;
    const total = LR.isNum(mem.total_bytes) && mem.total_bytes > 0 ? mem.total_bytes : 0;
    ui.memTotal.textContent = fmt.gb(mem.total_bytes);
    ui.memAvail.textContent = fmt.gb(mem.available_bytes);
    ui.memUsed.textContent = LR.isNum(mem.used_percent) ? Math.round(mem.used_percent) + '%' : '—';
    const pct = (n) => (total && LR.isNum(n) ? LR.clamp((n / total) * 100, 0, 100) : 0);
    ui.gauge.style.setProperty('--avail', pct(mem.available_bytes).toFixed(2) + '%');
    ui.gauge.style.setProperty('--budget', pct(mem.budget_bytes).toFixed(2) + '%');
    ui.gauge.style.setProperty('--reserve', pct(mem.min_free_bytes).toFixed(2) + '%');
    ui.gauge.classList.toggle('is-low', mem.can_run === false);
    ui.gauge.setAttribute('aria-valuenow', String(Math.round(pct(mem.available_bytes))));
    ui.gauge.setAttribute('aria-valuetext', fmt.gb(mem.available_bytes) + ' available of ' + fmt.gb(mem.total_bytes) + '; planning budget ' + fmt.gb(mem.budget_bytes) + '; reserve ' + fmt.gb(mem.min_free_bytes));
    ui.legendBudget.textContent = fmt.gb(mem.budget_bytes);
    ui.legendReserve.textContent = fmt.gb(mem.min_free_bytes);
    ui.planContext.textContent = LR.isNum(mem.context_chars) ? fmt.int(mem.context_chars) + ' characters' : '—';
    ui.planTokens.textContent = fmt.int(mem.max_tokens);
    ui.memReason.textContent = mem.can_run === false ? LR.str(mem.reason) : '';
    ui.memReason.hidden = mem.can_run !== false;
  }

  function buildMemory() {
    const sec = section('Memory', 'The planning budget sizes the prompt and the answer length. It is a scheduling hint, not a hard memory cap.');
    ui.mem = sec;
    ui.memTotal = h('dd', { text: '—' });
    ui.memAvail = h('dd', { text: '—' });
    ui.memUsed = h('dd', { text: '—' });
    ui.gauge = h('div', { class: 'gauge', role: 'meter', 'aria-label': 'Available memory compared with the planning budget', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': '0' }, [
      h('span', { class: 'gauge-fill' }), h('span', { class: 'gauge-reserve' }), h('span', { class: 'gauge-marker' }),
    ]);
    ui.legendBudget = h('strong', { text: '—' });
    ui.legendReserve = h('strong', { text: '—' });
    ui.memReason = h('p', { class: 'callout-inline is-warn', hidden: true });
    const id = LR.uid('budget');
    ui.range = h('input', { class: 'range', type: 'range', id, min: '20', max: '80', step: '5', value: String(LR.budgetPercent()), 'aria-describedby': id + '-help' });
    ui.rangeOut = h('output', { class: 'range-output', for: id, text: LR.budgetPercent() + '%' });
    ui.planContext = h('dd', { text: '—' });
    ui.planTokens = h('dd', { text: '—' });
    ui.range.addEventListener('input', onBudgetInput);
    sec.body.append(
      h('dl', { class: 'stats' }, [
        h('div', { class: 'stat' }, [h('dt', { text: 'Total' }), ui.memTotal]),
        h('div', { class: 'stat' }, [h('dt', { text: 'Available' }), ui.memAvail]),
        h('div', { class: 'stat' }, [h('dt', { text: 'In use' }), ui.memUsed]),
      ]),
      ui.gauge,
      h('p', { class: 'gauge-legend' }, [
        h('span', { class: 'legend-item' }, [h('span', { class: 'legend-swatch legend-avail', 'aria-hidden': 'true' }), 'Available']),
        h('span', { class: 'legend-item' }, [h('span', { class: 'legend-swatch legend-budget', 'aria-hidden': 'true' }), 'Budget ', ui.legendBudget]),
        h('span', { class: 'legend-item' }, [h('span', { class: 'legend-swatch legend-reserve', 'aria-hidden': 'true' }), 'Reserve ', ui.legendReserve]),
      ]),
      ui.memReason,
      h('div', { class: 'range-field' }, [
        h('div', { class: 'range-head' }, [h('label', { class: 'label', for: id, text: 'Planning budget' }), ui.rangeOut]),
        ui.range,
        h('div', { class: 'range-scale', 'aria-hidden': 'true' }, [h('span', { text: '20%' }), h('span', { text: '50%' }), h('span', { text: '80%' })]),
        h('p', { class: 'help', id: id + '-help', text: 'Share of total RAM LowRAM AI plans for (20–80 %). Higher means longer context and answers; lower leaves more room for other apps.' }),
      ]),
      h('dl', { class: 'stats stats-compact' }, [
        h('div', { class: 'stat' }, [h('dt', { text: 'Context' }), ui.planContext]),
        h('div', { class: 'stat' }, [h('dt', { text: 'Max answer tokens' }), ui.planTokens]),
      ]),
    );
    return sec.el;
  }

  function onBudgetInput() {
    const value = Number(ui.range.value);
    ui.rangeOut.textContent = value + '%';
    ui.range.setAttribute('aria-valuetext', value + ' percent of total RAM');
    window.clearTimeout(budgetTimer);
    budgetTimer = window.setTimeout(async () => {
      const seq = ++memSeq;
      if (await saveSetting('budget_percent', value)) {
        if (LR.state.settings) LR.state.settings.budget_percent = value;
        LR.announce('Planning budget saved: ' + value + ' percent');
      }
      try {
        const mem = await LR.get('/api/memory?percent=' + value);
        if (seq === memSeq) {
          LR.state.memory = mem;
          renderMemory(mem);
        }
      } catch (_) { /* keep old numbers */ }
    }, 350);
  }

  /* ------------------------------------------------------------ preferences */

  function buildPrefs() {
    const sec = section('Preferences');
    ui.prefs = sec;
    const s = LR.state.settings || {};
    const engine = s.engine || {};
    const media = s.media || {};
    ui.autoStart = LR.switchRow({
      label: 'Start the model when LowRAM AI opens',
      description: 'Loads your chosen model in the background so the first answer is quicker.',
      checked: engine.auto_start !== false,
      onChange: (on, input) => saveSetting('engine', { auto_start: on }, on ? 'Auto-start on' : 'Auto-start off').then((ok) => { if (!ok) input.checked = !on; }),
    });
    ui.unload = LR.switchRow({
      label: 'Unload the chat model during image and video jobs',
      description: 'Frees memory for image generation; the chat model restarts afterwards.',
      checked: media.unload_llm !== false,
      onChange: (on, input) => saveSetting('media', { unload_llm: on }, 'Saved').then((ok) => { if (!ok) input.checked = !on; }),
    });
    const theme = (s.ui && s.ui.theme) || LR.store.get('lowram.theme') || 'system';
    ui.theme = LR.segmented({
      label: 'Theme',
      value: theme,
      options: [
        { value: 'system', label: 'System', icon: 'monitor' },
        { value: 'light', label: 'Light', icon: 'sun' },
        { value: 'dark', label: 'Dark', icon: 'moon' },
      ],
      onChange: async (value) => {
        LR.setTheme(value);
        await saveSetting('ui', { theme: value }, 'Theme saved');
      },
    });
    const themeRow = h('div', { class: 'switch-row' }, [
      h('div', { class: 'switch-text' }, [h('p', { class: 'switch-label', text: 'Appearance' }), h('p', { class: 'switch-desc', text: 'Follow your system, or always use light or dark.' })]),
      ui.theme.el,
    ]);
    sec.body.append(ui.autoStart.el, ui.unload.el, themeRow);
    return sec.el;
  }

  function syncPrefs() {
    const s = LR.state.settings;
    if (!s || !ui.autoStart) return;
    ui.autoStart.input.checked = !s.engine || s.engine.auto_start !== false;
    ui.unload.input.checked = !s.media || s.media.unload_llm !== false;
    ui.autoStart.el.hidden = !LR.has('engine');
    ui.unload.el.hidden = !LR.has('media');
    ui.theme.set((s.ui && s.ui.theme) || 'system');
    if (LR.isNum(s.budget_percent) && document.activeElement !== ui.range) {
      ui.range.value = String(s.budget_percent);
      ui.rangeOut.textContent = s.budget_percent + '%';
    }
  }

  function renderAbout() {
    if (!ui.about) return;
    const st = LR.state.status || {};
    const feats = LR.state.features ? [...LR.state.features] : [];
    const names = { chat: 'Chat', api_coding: 'Coding projects', engine: 'Model engine', media: 'Images & video', cluster: 'Devices' };
    ui.about.body.replaceChildren(
      h('dl', { class: 'facts' }, [
        h('div', null, [h('dt', { text: 'Version' }), h('dd', { text: st.version ? 'LowRAM AI ' + st.version : '—' })]),
        h('div', null, [h('dt', { text: 'Modules' }), h('dd', { class: 'badges' }, Object.keys(names).map((k) => LR.badge(names[k] + (feats.includes(k) ? '' : ' – not in this build'), feats.includes(k) ? 'ok' : 'neutral')))]),
      ]),
      Array.isArray(st.startup_errors) && st.startup_errors.length
        ? LR.callout({ tone: 'warn', title: 'Startup problems', text: st.startup_errors.join(' · ') })
        : null,
      h('p', { class: 'muted small' }, [icon('shield'), ' Everything runs on this computer. Chats, files and models never leave it unless you add helper devices on your own network.']),
    );
  }

  /* ------------------------------------------------------------ view */

  function create() {
    const root = h('section', { class: 'view view-page', 'aria-labelledby': 'settings-title' });
    const page = h('div', { class: 'page' });
    const head = LR.pageHead({ title: 'Models & Settings', sub: 'Choose which model runs on this computer and how much memory LowRAM AI may plan for.' });
    head.querySelector('h1').id = 'settings-title';
    ui.engine = section('Model engine', 'The local server that runs your chat model.');
    ui.models = section('Models', 'Smaller models are faster and need less memory; larger ones answer better.');
    ui.downloads = section('Downloads');
    ui.downloadsList = h('div', { class: 'dl-list' });
    ui.downloads.body.append(ui.downloadsList);
    ui.about = section('About this build');
    page.append(head, ui.engine.el, ui.models.el, ui.downloads.el, buildMemory(), buildPrefs(), ui.about.el);
    root.append(page);
    return root;
  }

  LR.registerView('settings', {
    title: 'Models & Settings',
    create,
    show() {
      const engineOk = LR.has('engine');
      ui.engine.el.hidden = false;
      ui.downloads.el.hidden = !engineOk && !LR.has('media');
      renderEngine();
      renderModels();
      renderAbout();
      syncPrefs();
      renderMemory(LR.state.memory);
      if (engineOk) LR.loadCatalog().catch(() => {});
      if (!unmountDownloads && !ui.downloads.el.hidden) {
        unmountDownloads = LR.downloads.mount(ui.downloadsList, null, { empty: h('p', { class: 'muted', text: 'No downloads yet. Models you install show their progress here.' }) });
      }
      offs.push(
        LR.on('engine', renderEngine),
        LR.on('catalog', () => { renderModels(); renderEngine(); }),
        LR.on('memory', (m) => { if (document.activeElement !== ui.range) renderMemory(m); }),
        LR.on('settings', syncPrefs),
        LR.on('status', renderAbout),
        LR.downloads.subscribe((list) => {
          const sig = list.filter(LR.downloads.isActive).map((d) => d.id).join(',');
          if (sig !== ui.dlSig) {
            const finished = ui.dlSig && ui.dlSig.length > sig.length;
            ui.dlSig = sig;
            if (finished) LR.loadCatalog().catch(() => {});
            else renderModels();
          }
        }),
      );
    },
    hide() {
      while (offs.length) offs.pop()();
      if (unmountDownloads) { unmountDownloads(); unmountDownloads = null; }
    },
  });
})();
