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
      LR.toast(LR.t('Could not save: ') + err.message, 'error');
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
      body.replaceChildren(h('p', { class: 'muted', text: LR.t('The model engine is not part of this build. LowRAM AI talks to the model server configured with LOWRAM_LLM_URL.') }));
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
      out.push(LR.callout({ tone: 'info', title: LR.t('Using an external model server'), text: LR.t('LOWRAM_LLM_URL is set, so LowRAM AI does not start its own engine. Endpoint: ') + LR.str(eng.llm_url) }));
      body.replaceChildren(...out);
      return;
    }
    const states = {
      ready: ['ok', LR.t('Running')],
      starting: ['warn', LR.t('Starting…')],
      error: ['error', 'Error'],
      stopped: ['idle', LR.t('Stopped')],
    };
    const st = states[server.state] || states.stopped;
    const name = LR.modelName(server.model_id);
    let line = LR.t('No model is running.');
    if (server.state === 'ready') line = (name || LR.t('The model')) + LR.t(' is ready.');
    else if (server.state === 'starting') line = LR.t('Loading ') + (name || LR.t('the model')) + LR.t(' into memory – this can take a minute.');
    else if (server.state === 'error') line = LR.t('The engine stopped with an error.');
    if (eng.paused) line = LR.t('Paused while an image or video is generated – the model restarts by itself afterwards.');
    out.push(h('div', { class: 'engine-row' }, [
      h('span', { class: 'dot dot-' + st[0] + (server.state === 'starting' ? ' is-pulsing' : ''), 'aria-hidden': 'true' }),
      h('div', { class: 'engine-text' }, [h('p', { class: 'engine-state', text: line }), h('p', { class: 'muted small', text: st[1] + (server.port ? LR.t(' · port ') + server.port : '') + (server.ctx_size ? LR.t(' · context ') + fmt.int(server.ctx_size) + ' tokens' : '') + (server.started_at ? LR.t(' · since ') + fmt.time(server.started_at) : '') })]),
    ]));
    const facts = h('dl', { class: 'facts' }, [
      h('div', null, [h('dt', { text: LR.t('Runtime') }), h('dd', { text: runtime.installed ? 'llama.cpp' + (runtime.version ? ' ' + runtime.version : '') + (runtime.source ? ' (' + runtime.source + ')' : '') : LR.t('Not installed') })]),
      h('div', null, [h('dt', { text: LR.t('Model') }), h('dd', { text: name || '—' })]),
    ]);
    out.push(facts);
    if (server.state === 'error' && server.error) {
      out.push(LR.callout({ tone: 'error', title: LR.t('What went wrong'), text: LR.str(server.error) + LR.t(' More details are in the LowRAM AI log (the window or terminal that runs the app).') }));
    }
    const actions = [];
    if (!runtime.installed) {
      actions.push(LR.button({ text: LR.t('Install runtime'), icon: 'download', variant: 'primary', onclick: (ev) => installRuntime(ev.currentTarget) }));
    }
    const installed = installedModels();
    if (server.state === 'ready' || server.state === 'starting') {
      actions.push(LR.button({ text: LR.t('Stop engine'), icon: 'power', onclick: (ev) => stopEngine(ev.currentTarget) }));
    } else if (runtime.installed && installed.length) {
      const preferred = (LR.state.settings && LR.state.settings.engine && LR.state.settings.engine.model_id) || installed[0].id;
      actions.push(LR.button({ text: server.state === 'error' ? LR.t('Try again') : LR.t('Start ') + LR.modelName(preferred), icon: 'play', variant: 'primary', onclick: (ev) => startEngine(preferred, ev.currentTarget) }));
    }
    if (actions.length) out.push(h('div', { class: 'row-actions' }, actions));
    body.replaceChildren(...out);
  }

  function installedModels() {
    const cat = LR.state.catalog;
    return cat && Array.isArray(cat.models) ? cat.models.filter((m) => m.installed) : [];
  }

  async function installRuntime(btn) {
    LR.setBusy(btn, true, LR.t('Starting download…'));
    try {
      await LR.post('/api/engine/runtime/install', {});
      LR.toast(LR.t('Downloading the runtime'));
      LR.downloads.refresh();
    } catch (err) {
      LR.toast(LR.t('Could not start the download: ') + err.message, 'error');
    } finally {
      if (btn.isConnected) LR.setBusy(btn, false);
    }
  }

  async function startEngine(modelId, btn) {
    if (btn) LR.setBusy(btn, true, LR.t('Starting…'));
    try {
      const data = await LR.post('/api/engine/start', modelId ? { model_id: modelId } : {});
      if (LR.state.engine && data.server) LR.state.engine.server = data.server;
      if (modelId) saveSetting('engine', { model_id: modelId });
      LR.toast(LR.t('Starting ') + LR.modelName(modelId || (data.server && data.server.model_id)) + '…', 'info');
    } catch (err) {
      LR.toast(LR.t('Could not start the model: ') + err.message, 'error');
    } finally {
      if (btn && btn.isConnected) LR.setBusy(btn, false);
      LR.refreshStatus();
      renderEngine();
      renderModels();
    }
  }
  LR.startEngine = startEngine;

  async function stopEngine(btn) {
    LR.setBusy(btn, true, LR.t('Stopping…'));
    try {
      const data = await LR.post('/api/engine/stop', {});
      if (LR.state.engine && data.server) LR.state.engine.server = data.server;
      LR.toast(LR.t('Engine stopped'), 'info');
    } catch (err) {
      LR.toast(LR.t('Could not stop the engine: ') + err.message, 'error');
    } finally {
      LR.refreshStatus();
      renderEngine();
      renderModels();
    }
  }

  /* ------------------------------------------------------------ models */

  function downloadFor(model) {
    if (Array.isArray(model.download_ids) && model.download_ids.length) {
      const own = LR.downloads.list.find((d) => LR.downloads.isActive(d) && model.download_ids.includes(d.id));
      if (own) return own;
    }
    return LR.downloads.list.find((d) => LR.downloads.isActive(d) && (d.kind === 'model' || d.kind === 'media-model') &&
      (String(d.id).includes(model.id) || (d.label && model.name && String(d.label).includes(model.name))));
  }
  LR.downloadFor = downloadFor;

  /** One catalog card; shared with the media view (kind: 'engine' | 'media'). */
  function modelCard(m, o) {
    const opts = o || {};
    const totalRam = opts.ramTotal || 0;
    const tooBig = m.fits_ram === false || (totalRam && LR.isNum(m.min_ram_gb) && m.min_ram_gb * GB > totalRam);
    const active = Boolean(opts.activeId && opts.activeId === m.id);
    const unavailable = m.available === false;
    const badges = [];
    if (m.source === 'custom') badges.push(LR.badge(LR.t('Your model'), 'info'));
    else if (m.source === 'local') badges.push(LR.badge(LR.t('Local file'), 'neutral'));
    else if (m.recommended) badges.push(LR.badge(LR.t('Recommended'), 'accent'));
    if (unavailable) badges.push(h('span', { class: 'badge badge-warn' }, [icon('alert'), h('span', { text: LR.t('Drive not connected') })]));
    if (active) badges.push(LR.badge(opts.activeLabel || LR.t('In use'), 'ok'));
    else if (m.installed) badges.push(LR.badge(LR.t('Installed'), 'neutral'));
    if (m.purpose === 'code') badges.push(LR.badge('Code', 'info'));
    const facts = h('dl', { class: 'model-facts' }, [
      h('div', null, [h('dt', { text: LR.t('Download') }), h('dd', { text: LR.isNum(m.size_bytes) ? fmt.bytes(m.size_bytes) : '—' })]),
      h('div', null, [h('dt', { text: LR.t('Needs') }), h('dd', { text: LR.isNum(m.min_ram_gb) ? fmt.ramGb(m.min_ram_gb) + ' RAM' : '—' })]),
      m.params || m.quant ? h('div', null, [h('dt', { text: LR.t('Size') }), h('dd', { text: [m.params, m.quant].filter(Boolean).join(' · ') })]) : null,
    ]);
    const actions = h('div', { class: 'model-actions' });
    const progressSlot = h('div', { class: 'model-progress' });
    const dl = downloadFor(m);
    if (dl) {
      const r = LR.downloads.row(dl, { label: LR.t('Downloading ') + (m.name || m.id) });
      progressSlot.append(r.el);
    } else if (!m.installed) {
      actions.append(LR.button({ text: LR.t('Install'), icon: 'download', variant: tooBig ? 'secondary' : 'primary', size: 'sm', onclick: (ev) => opts.onInstall(m, ev.currentTarget) }));
    } else {
      if (!active && opts.onUse) actions.append(LR.button({ text: opts.useLabel || LR.t('Use this model'), icon: 'play', variant: 'primary', size: 'sm', disabled: unavailable, title: unavailable ? LR.t('Connect the drive first') : null, onclick: (ev) => opts.onUse(m, ev.currentTarget) }));
      if (opts.extra) opts.extra(m, actions);
      if (opts.onDelete) actions.append(LR.button({ text: m.source === 'local' ? LR.t('Remove') : LR.t('Delete'), icon: 'trash', variant: 'ghost', size: 'sm', disabled: active, title: active ? LR.t('Stop the model before deleting it') : null, onclick: (ev) => opts.onDelete(m, ev.currentTarget) }));
    }
    return h('article', { class: 'model-card' + (m.recommended ? ' is-recommended' : '') + (active ? ' is-active' : ''), 'aria-label': m.name || m.id }, [
      h('div', { class: 'model-head' }, [h('h3', { class: 'model-name', text: m.name || m.id }), h('div', { class: 'badges' }, badges)]),
      h('p', { class: 'model-family', text: [m.family, m.license].filter(Boolean).join(' · ') }),
      m.description ? h('p', { class: 'model-desc', text: m.description }) : null,
      facts,
      tooBig ? h('p', { class: 'model-warn' }, [icon('alert'), h('span', { text: LR.t('Needs more memory than this computer has – it may be very slow or fail.') })]) : null,
      m.warning ? h('p', { class: 'model-warn' }, [icon('clock'), h('span', { text: LR.str(m.warning) })]) : null,
      unavailable ? h('p', { class: 'model-warn' }, [icon('folder'), h('span', { text: LR.t('The file is on a drive that is not connected') + (m.path ? ': ' + m.path : '.') })]) : null,
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
      body.replaceChildren(LR.empty({ icon: 'cpu', title: LR.t('No models in the catalog'), text: LR.t('This build does not list any downloadable models.') }));
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
            text: isCode ? LR.t('Code mode default') : LR.t('Use for Code mode'),
            icon: isCode ? 'check' : 'code',
            size: 'sm',
            variant: 'ghost',
            disabled: isCode,
            onclick: async () => {
              if (await saveSetting('engine', { code_model_id: model.id }, LR.t('Code mode will use ') + (model.name || model.id))) renderModels();
            },
          }));
        }
      },
    }))));
  }

  async function installModel(m, btn) {
    LR.setBusy(btn, true, LR.t('Starting…'));
    const path = '/api/engine/models/' + encodeURIComponent(m.id) + '/install';
    try {
      try {
        await LR.post(path, {});
      } catch (err) {
        if (!(await LR.offerSuggestion(err, path))) throw err;
      }
      LR.toast(LR.t('Downloading ') + (m.name || m.id));
      await LR.downloads.refresh();
    } catch (err) {
      LR.toast(LR.t('Could not start the download: ') + err.message, 'error');
      if (btn.isConnected) LR.setBusy(btn, false);
    }
  }

  async function deleteModel(m) {
    const local = m.source === 'local';
    const ok = await LR.confirm({
      title: (local ? LR.t('Remove ') : LR.t('Delete ')) + (m.name || m.id) + '?',
      message: local ? LR.t('LowRAM AI forgets this model. The file itself stays where it is.') : LR.t('This frees ') + fmt.bytes(m.size_bytes) + LR.t(' of disk space. You can download it again later.'),
      confirmText: local ? LR.t('Remove') : LR.t('Delete model'),
      danger: !local,
    });
    if (!ok) return;
    try {
      await LR.post('/api/engine/models/' + encodeURIComponent(m.id) + '/delete', {});
      LR.toast((m.name || m.id) + LR.t(' deleted'));
    } catch (err) {
      LR.toast(LR.t('Could not delete: ') + err.message, 'error');
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
    ui.gauge.setAttribute('aria-valuetext', fmt.gb(mem.available_bytes) + LR.t(' available of ') + fmt.gb(mem.total_bytes) + LR.t('; planning budget ') + fmt.gb(mem.budget_bytes) + LR.t('; reserve ') + fmt.gb(mem.min_free_bytes));
    ui.legendBudget.textContent = fmt.gb(mem.budget_bytes);
    ui.legendReserve.textContent = fmt.gb(mem.min_free_bytes);
    ui.planContext.textContent = LR.isNum(mem.context_chars) ? fmt.int(mem.context_chars) + LR.t(' characters') : '—';
    ui.planTokens.textContent = fmt.int(mem.max_tokens);
    ui.memReason.textContent = mem.can_run === false ? LR.str(mem.reason) : '';
    ui.memReason.hidden = mem.can_run !== false;
  }

  function buildMemory() {
    const sec = section(LR.t('Memory'), LR.t('The planning budget sizes the prompt and the answer length. It is a scheduling hint, not a hard memory cap.'));
    ui.mem = sec;
    ui.memTotal = h('dd', { text: '—' });
    ui.memAvail = h('dd', { text: '—' });
    ui.memUsed = h('dd', { text: '—' });
    ui.gauge = h('div', { class: 'gauge', role: 'meter', 'aria-label': LR.t('Available memory compared with the planning budget'), 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': '0' }, [
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
        h('div', { class: 'stat' }, [h('dt', { text: LR.t('Available') }), ui.memAvail]),
        h('div', { class: 'stat' }, [h('dt', { text: LR.t('In use') }), ui.memUsed]),
      ]),
      ui.gauge,
      h('p', { class: 'gauge-legend' }, [
        h('span', { class: 'legend-item' }, [h('span', { class: 'legend-swatch legend-avail', 'aria-hidden': 'true' }), LR.t('Available')]),
        h('span', { class: 'legend-item' }, [h('span', { class: 'legend-swatch legend-budget', 'aria-hidden': 'true' }), 'Budget ', ui.legendBudget]),
        h('span', { class: 'legend-item' }, [h('span', { class: 'legend-swatch legend-reserve', 'aria-hidden': 'true' }), 'Reserve ', ui.legendReserve]),
      ]),
      ui.memReason,
      h('div', { class: 'range-field' }, [
        h('div', { class: 'range-head' }, [h('label', { class: 'label', for: id, text: LR.t('Planning budget') }), ui.rangeOut]),
        ui.range,
        h('div', { class: 'range-scale', 'aria-hidden': 'true' }, [h('span', { text: '20%' }), h('span', { text: '50%' }), h('span', { text: '80%' })]),
        h('p', { class: 'help', id: id + '-help', text: LR.t('Share of total RAM LowRAM AI plans for (20–80 %). Higher means longer context and answers; lower leaves more room for other apps.') }),
      ]),
      h('dl', { class: 'stats stats-compact' }, [
        h('div', { class: 'stat' }, [h('dt', { text: 'Context' }), ui.planContext]),
        h('div', { class: 'stat' }, [h('dt', { text: LR.t('Max answer tokens') }), ui.planTokens]),
      ]),
    );
    return sec.el;
  }

  function onBudgetInput() {
    const value = Number(ui.range.value);
    ui.rangeOut.textContent = value + '%';
    ui.range.setAttribute('aria-valuetext', value + LR.t(' percent of total RAM'));
    window.clearTimeout(budgetTimer);
    budgetTimer = window.setTimeout(async () => {
      const seq = ++memSeq;
      if (await saveSetting('budget_percent', value)) {
        if (LR.state.settings) LR.state.settings.budget_percent = value;
        LR.announce(LR.t('Planning budget saved: ') + value + LR.t(' percent'));
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
    const sec = section(LR.t('Preferences'));
    ui.prefs = sec;
    const s = LR.state.settings || {};
    const engine = s.engine || {};
    const media = s.media || {};
    ui.autoStart = LR.switchRow({
      label: LR.t('Start the model when LowRAM AI opens'),
      description: LR.t('Loads your chosen model in the background so the first answer is quicker.'),
      checked: engine.auto_start !== false,
      onChange: (on, input) => saveSetting('engine', { auto_start: on }, on ? LR.t('Auto-start on') : LR.t('Auto-start off')).then((ok) => { if (!ok) input.checked = !on; }),
    });
    ui.unload = LR.switchRow({
      label: LR.t('Unload the chat model during image and video jobs'),
      description: LR.t('Frees memory for image generation; the chat model restarts afterwards.'),
      checked: media.unload_llm !== false,
      onChange: (on, input) => saveSetting('media', { unload_llm: on }, LR.t('Saved')).then((ok) => { if (!ok) input.checked = !on; }),
    });
    const theme = (s.ui && s.ui.theme) || LR.store.get('lowram.theme') || 'system';
    ui.theme = LR.segmented({
      label: LR.t('Theme'),
      value: theme,
      options: [
        { value: 'system', label: 'System', icon: 'monitor' },
        { value: 'light', label: LR.t('Light'), icon: 'sun' },
        { value: 'dark', label: LR.t('Dark'), icon: 'moon' },
      ],
      onChange: async (value) => {
        LR.setTheme(value);
        await saveSetting('ui', { theme: value }, LR.t('Theme saved'));
      },
    });
    const themeRow = h('div', { class: 'switch-row' }, [
      h('div', { class: 'switch-text' }, [h('p', { class: 'switch-label', text: LR.t('Appearance') }), h('p', { class: 'switch-desc', text: LR.t('Follow your system, or always use light or dark.') })]),
      ui.theme.el,
    ]);
    const segRow = (label, desc, seg) => h('div', { class: 'switch-row' }, [
      h('div', { class: 'switch-text' }, [h('p', { class: 'switch-label', text: label }), desc ? h('p', { class: 'switch-desc', text: desc }) : null]),
      seg.el,
    ]);
    ui.language = LR.segmented({
      label: LR.t('Language'),
      value: LR.langPref || 'auto',
      options: [
        { value: 'auto', label: LR.t('Automatic') },
        { value: 'de', label: 'Deutsch' },
        { value: 'en', label: 'English' },
      ],
      onChange: async (value) => {
        const uiS = (LR.state.settings && LR.state.settings.ui) || {};
        if ('language' in uiS) await saveSetting('ui', { language: value });
        LR.toast(LR.t('Language saved – reloading…'));
        window.setTimeout(() => LR.applyLanguagePref(value), 600);
      },
    });
    ui.purpose = LR.segmented({
      label: LR.t('Main use'),
      value: LR.store.get('lowram.purpose') || 'general',
      options: [
        { value: 'code', label: LR.t('Programming'), icon: 'code' },
        { value: 'chat', label: LR.t('Writing'), icon: 'pencil' },
        { value: 'general', label: LR.t('Everything'), icon: 'sparkle' },
      ],
      onChange: (value) => {
        LR.store.set('lowram.purpose', value);
        const uiS = (LR.state.settings && LR.state.settings.ui) || {};
        if ('purpose' in uiS) saveSetting('ui', { purpose: value }, LR.t('Saved'));
        else LR.toast(LR.t('Saved'));
      },
    });
    ui.updates = LR.switchRow({
      label: LR.t('Check for updates'),
      description: LR.t('Looks for a new LowRAM AI version on GitHub once per start. Nothing else is sent.'),
      checked: true,
      onChange: (on, input) => saveSetting('ui', { check_updates: on }, LR.t('Saved')).then((ok) => { if (!ok) input.checked = !on; }),
    });
    ui.trayRow = LR.switchRow({
      label: LR.t('Keep LowRAM AI in the menu bar'),
      description: LR.t('Closing the window keeps LowRAM AI running in the menu bar (tray on Windows), so long jobs continue.'),
      checked: true,
      onChange: (on, input) => saveSetting('desktop', { keep_running_in_menu_bar: on }, LR.t('Saved')).then((ok) => { if (!ok) input.checked = !on; }),
    });
    ui.languageRow = segRow(LR.t('Language'), LR.t('Automatic follows your browser language (German if it is neither German nor English).'), ui.language);
    ui.purposeRow = segRow(LR.t('Main use'), LR.t('Used to recommend models. You can always pick any model yourself.'), ui.purpose);
    sec.body.append(ui.languageRow, ui.purposeRow, ui.autoStart.el, ui.unload.el, themeRow, ui.updates.el, ui.trayRow.el);
    return sec.el;
  }

  /** settings.desktop.keep_running_in_menu_bar (only shown when the backend has it). */
  const hasTray = () => {
    const d = LR.state.settings && LR.state.settings.desktop;
    return Boolean(d && typeof d === 'object' && 'keep_running_in_menu_bar' in d);
  };

  function syncPrefs() {
    const s = LR.state.settings;
    if (!s || !ui.autoStart) return;
    ui.autoStart.input.checked = !s.engine || s.engine.auto_start !== false;
    ui.unload.input.checked = !s.media || s.media.unload_llm !== false;
    ui.autoStart.el.hidden = !LR.has('engine');
    ui.unload.el.hidden = !LR.has('media');
    ui.theme.set((s.ui && s.ui.theme) || 'system');
    const uiS = s.ui || {};
    ui.language.set(uiS.language === 'de' || uiS.language === 'en' ? uiS.language : (LR.langPref || 'auto'));
    if (uiS.purpose) ui.purpose.set(uiS.purpose);
    ui.updates.el.hidden = !('check_updates' in uiS);
    ui.updates.input.checked = uiS.check_updates !== false;
    ui.trayRow.el.hidden = !hasTray();
    if (hasTray()) ui.trayRow.input.checked = s.desktop.keep_running_in_menu_bar !== false;
    if (LR.isNum(s.budget_percent) && document.activeElement !== ui.range) {
      ui.range.value = String(s.budget_percent);
      ui.rangeOut.textContent = s.budget_percent + '%';
    }
  }

  function renderAbout() {
    if (!ui.about) return;
    const st = LR.state.status || {};
    const feats = LR.state.features ? [...LR.state.features] : [];
    const names = { chat: 'Chat', api_coding: LR.t('Coding projects'), engine: LR.t('Model engine'), media: LR.t('Images & video'), cluster: LR.t('Devices') };
    ui.about.body.replaceChildren(...[
      h('dl', { class: 'facts' }, [
        h('div', null, [h('dt', { text: 'Version' }), h('dd', { text: st.version ? 'LowRAM AI ' + st.version : '—' })]),
        h('div', null, [h('dt', { text: LR.t('Modules') }), h('dd', { class: 'badges' }, Object.keys(names).map((k) => LR.badge(names[k] + (feats.includes(k) ? '' : LR.t(' – not in this build')), feats.includes(k) ? 'ok' : 'neutral')))]),
      ]),
      Array.isArray(st.startup_errors) && st.startup_errors.length
        ? LR.callout({ tone: 'warn', title: LR.t('Startup problems'), text: st.startup_errors.join(' · ') })
        : null,
      h('p', { class: 'muted small' }, [icon('shield'), LR.t(' Everything runs on this computer. Chats, files and models never leave it unless you add helper devices on your own network.')]),
    ].filter(Boolean));
  }

  /* ------------------------------------------------------------ own models (v0.3.1) */

  const desktop = () => (window.pywebview && window.pywebview.api) || null;
  const hasOwnModels = () => {
    const cat = LR.state.catalog;
    return Boolean(cat && Array.isArray(cat.models) && cat.models.some((m) => 'source' in m));
  };

  function pathField(label, placeholder, picker, pickerTitle) {
    const input = h('input', { class: 'input mono', type: 'text', placeholder, spellcheck: 'false', autocomplete: 'off' });
    const field = LR.field({ label, control: input });
    const row = h('div', { class: 'input-row' });
    input.replaceWith(row);
    row.append(input);
    const api = desktop();
    if (api && typeof api[picker] === 'function') {
      const choose = LR.button({ text: LR.t('Choose…'), icon: 'folder', variant: 'secondary', onclick: async () => {
        try {
          const picked = await api[picker](pickerTitle);
          if (picked) input.value = String(picked);
        } catch (_) { /* dialog closed */ }
      } });
      row.append(choose);
    }
    return { field, input, row };
  }

  function buildOwnModels() {
    const sec = section(LR.t('Add your own model'), LR.t('Any GGUF model from Hugging Face, or a model file you already have.'));
    ui.own = sec;
    const source = h('input', { class: 'input', type: 'text', placeholder: LR.t('e.g. bartowski/Llama-3.2-3B-Instruct-GGUF or a Hugging Face link'), spellcheck: 'false', autocomplete: 'off' });
    const fSource = LR.field({ label: LR.t('Hugging Face model'), control: source, hint: LR.t('Paste the name or the link. LowRAM AI lists the files and recommends one that fits your memory.') });
    const find = LR.button({ text: LR.t('Find files'), icon: 'eye', variant: 'primary', type: 'submit' });
    const results = h('div', { class: 'resolve-results', 'aria-live': 'polite' });
    const sourceRow = h('div', { class: 'input-row' });
    source.replaceWith(sourceRow);
    sourceRow.append(source, find);
    const form = h('form', { class: 'form own-form', novalidate: true }, [fSource.el, results]);
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      fSource.setError('');
      const value = source.value.trim();
      if (!value) {
        fSource.setError(LR.t('Paste a model name like owner/model-GGUF or a link.'));
        source.focus();
        return;
      }
      LR.setBusy(find, true, LR.t('Looking…'));
      results.replaceChildren(LR.skeleton(3));
      try {
        const data = await LR.post('/api/engine/custom/resolve', { source: value });
        renderResolved(results, data);
      } catch (err) {
        results.replaceChildren();
        fSource.setError(err.message);
      } finally {
        LR.setBusy(find, false);
      }
    });

    const local = pathField(LR.t('Use a model file I already have'), '/Volumes/External/models/my-model.gguf', 'pick_file', LR.t('Choose a GGUF model file'));
    const add = LR.button({ text: LR.t('Add file'), icon: 'plus', variant: 'secondary', type: 'submit' });
    local.row.append(add);
    const localForm = h('form', { class: 'form own-form', novalidate: true }, [
      local.field.el,
      h('p', { class: 'help', text: LR.t('The file stays where it is (an external drive works). LowRAM AI only remembers where to find it.') }),
    ]);
    localForm.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      local.field.setError('');
      const path = local.input.value.trim();
      if (!/\.gguf$/i.test(path) || !/^(\/|~|[A-Za-z]:[\\/])/.test(path)) {
        local.field.setError(LR.t('Enter the full path of a .gguf file.'));
        local.input.focus();
        return;
      }
      LR.setBusy(add, true, LR.t('Adding…'));
      try {
        const data = await LR.post('/api/engine/custom/import', { path });
        LR.toast(LR.t('Added ') + LR.str(data.model && data.model.name, LR.t('your model')));
        local.input.value = '';
        await LR.loadCatalog().catch(() => {});
      } catch (err) {
        local.field.setError(err.message);
      } finally {
        LR.setBusy(add, false);
      }
    });
    sec.body.append(form, h('div', { class: 'or-rule', 'aria-hidden': 'true' }, h('span', { text: 'or' })), localForm);
    return sec.el;
  }

  function renderResolved(container, data, onStarted) {
    const files = Array.isArray(data.files) ? data.files : [];
    if (!files.length) {
      container.replaceChildren(LR.callout({ tone: 'info', title: LR.t('No GGUF files found'), text: LR.t('This repository has no GGUF model files. Look for a repository whose name ends in “GGUF”.') }));
      return;
    }
    const sorted = [...files].sort((a, b) => (b.recommended - a.recommended) || ((a.size_bytes || 0) - (b.size_bytes || 0)));
    container.replaceChildren(
      h('p', { class: 'muted small', text: LR.str(data.repo) + (data.revision ? ' · ' + String(data.revision).slice(0, 12) : '') + ' · ' + fmt.plural(files.length, 'file') }),
      h('ul', { class: 'file-list' }, sorted.map((f) => h('li', { class: 'file-row' + (f.recommended ? ' is-recommended' : '') }, [
        h('div', { class: 'file-main' }, [
          h('p', { class: 'file-name', text: LR.str(f.filename) }),
          h('p', { class: 'badges' }, [
            f.quant ? LR.badge(f.quant, 'neutral') : null,
            LR.badge(fmt.bytes(f.size_bytes), 'neutral'),
            f.fits_ram === false ? LR.badge(LR.t('Too big for this computer'), 'error') : LR.badge(LR.t('Fits your RAM'), 'ok'),
            f.recommended ? LR.badge(LR.t('Recommended'), 'accent') : null,
          ]),
        ]),
        LR.button({ text: LR.t('Install'), icon: 'download', size: 'sm', variant: f.recommended ? 'primary' : 'secondary', onclick: (ev) => installCustom(data.repo, f, ev.currentTarget, onStarted) }),
      ]))),
    );
  }

  async function installCustom(repo, file, btn, onStarted) {
    LR.setBusy(btn, true, LR.t('Starting…'));
    try {
      await LR.post('/api/engine/custom/install', { repo, filename: file.filename });
      LR.toast(LR.t('Downloading ') + file.filename);
      LR.downloads.refresh();
      btn.replaceChildren(icon('check'), h('span', { text: LR.t('Downloading') }));
      if (onStarted) onStarted();
    } catch (err) {
      LR.toast(LR.t('Could not start the download: ') + err.message, 'error');
      LR.setBusy(btn, false);
    }
  }

  LR.renderResolvedFiles = renderResolved;

  /* ------------------------------------------------------------ storage (v0.3.1) */

  let storageTimer = 0;

  function buildStorage() {
    const sec = section(LR.t('Storage'), LR.t('Where model files are kept. An external drive works too.'));
    ui.storage = sec;
    sec.body.append(LR.skeleton(3));
    return sec.el;
  }

  async function loadStorage() {
    if (!ui.storage || ui.storage.el.hidden) return;
    try {
      const data = await LR.get('/api/engine/storage');
      renderStorage(data.storage || data);
    } catch (err) {
      ui.storage.body.replaceChildren(LR.callout({ tone: 'error', title: LR.t('Could not read the storage settings'), text: err.message }));
    }
  }

  function renderStorage(sto) {
    window.clearTimeout(storageTimer);
    const out = [];
    out.push(h('div', { class: 'storage-row' }, [
      h('span', { class: 'machine-icon', 'aria-hidden': 'true' }, icon('folder')),
      h('div', { class: 'storage-main' }, [
        h('p', { class: 'storage-path' }, [h('code', { text: LR.str(sto.models_dir) }), LR.badge(sto.custom ? LR.t('Custom folder') : 'Default', sto.custom ? 'info' : 'neutral')]),
        LR.isNum(sto.total_bytes) && sto.total_bytes > 0 ? (() => {
          const bar = LR.progress({ size: 'sm', label: LR.t('Disk space used'), value: 1 - (sto.free_bytes || 0) / sto.total_bytes });
          return h('div', { class: 'storage-bar' }, [bar.el, h('p', { class: 'muted small', text: fmt.bytes(sto.free_bytes) + LR.t(' free of ') + fmt.bytes(sto.total_bytes) })]);
        })() : null,
      ]),
    ]));
    if (sto.available === false) out.push(LR.callout({ tone: 'warn', title: LR.t('Drive not connected'), text: LR.t('The models folder is not reachable. Plug the drive in again, or choose another folder.') }));
    else if (sto.writable === false) out.push(LR.callout({ tone: 'warn', title: LR.t('Read-only folder'), text: LR.t('LowRAM AI cannot save new models here. Choose another folder.') }));
    const mv = sto.moving;
    if (mv && mv.state && mv.state !== 'done') {
      if (mv.state === 'failed') out.push(LR.callout({ tone: 'error', title: LR.t('Moving the models failed'), text: LR.str(mv.error, LR.t('Unknown error.')) + LR.t(' Your files were not deleted.') }));
      else {
        const bar = LR.progress({ label: LR.t('Moving models'), value: mv.bytes_total ? mv.bytes_done / mv.bytes_total : null });
        out.push(h('div', { class: 'dl-row is-active' }, [
          h('div', { class: 'dl-head' }, [LR.spinner(), h('span', { class: 'dl-label', text: LR.t('Moving your models…') })]),
          bar.el,
          h('p', { class: 'dl-meta', text: mv.bytes_total ? fmt.bytes(mv.bytes_done) + LR.t(' of ') + fmt.bytes(mv.bytes_total) : LR.t('Preparing…') }),
        ]));
        storageTimer = window.setTimeout(loadStorage, 1000);
      }
    }
    const change = pathField(LR.t('New models folder'), LR.t('/Volumes/External/LowRAM models'), 'pick_folder', LR.t('Choose a folder for models'));
    change.input.value = sto.custom ? LR.str(sto.models_dir) : '';
    const moveId = LR.uid('move');
    const move = h('input', { type: 'checkbox', class: 'checkbox', id: moveId, checked: true });
    const save = LR.button({ text: LR.t('Use this folder'), variant: 'primary', type: 'submit' });
    const form = h('form', { class: 'form storage-form', novalidate: true, hidden: true }, [
      change.field.el,
      h('label', { class: 'check-row', for: moveId }, [move, h('span', { text: LR.t('Move my existing models there') })]),
      h('p', { class: 'help', text: LR.t('The engine stops while models are moved. The folder must already exist.') }),
      h('div', { class: 'form-actions' }, [save, LR.button({ text: LR.t('Cancel'), variant: 'ghost', onclick: () => { form.hidden = true; toggle.hidden = false; } })]),
    ]);
    const submit = async (dir) => {
      change.field.setError('');
      if (dir !== null && !/^(\/|~|[A-Za-z]:[\\/])/.test(dir)) {
        change.field.setError(LR.t('Enter the full path of an existing folder.'));
        change.input.focus();
        return;
      }
      LR.setBusy(save, true, LR.t('Saving…'));
      try {
        const data = await LR.post('/api/engine/storage', { models_dir: dir, move_existing: move.checked });
        LR.toast(dir === null ? LR.t('Using the default folder') : LR.t('Models folder changed'));
        renderStorage(data.storage || data);
        LR.loadCatalog().catch(() => {});
        LR.refreshStatus();
      } catch (err) {
        change.field.setError(err.message);
        LR.setBusy(save, false);
      }
    };
    form.addEventListener('submit', (ev) => { ev.preventDefault(); submit(change.input.value.trim()); });
    const toggle = LR.button({ text: LR.t('Change folder…'), icon: 'folder', variant: 'secondary', onclick: () => { form.hidden = false; toggle.hidden = true; change.input.focus(); } });
    const actions = [toggle];
    if (sto.custom) actions.push(LR.button({ text: LR.t('Reset to default'), variant: 'ghost', onclick: async () => {
      const ok = await LR.confirm({ title: LR.t('Use the default folder again?'), message: LR.t('Your models will be moved back to ') + LR.str(sto.default_dir, LR.t('the default folder')) + '.', confirmText: LR.t('Reset') });
      if (ok) {
        move.checked = true;
        submit(null);
      }
    } }));
    out.push(h('div', { class: 'row-actions' }, actions), form);
    ui.storage.body.replaceChildren(...out);
  }

  function syncExtras() {
    const on = LR.has('engine') && hasOwnModels();
    if (ui.own) ui.own.el.hidden = !on;
    if (ui.storage) {
      const wasHidden = ui.storage.el.hidden;
      ui.storage.el.hidden = !on;
      if (on && wasHidden) loadStorage();
    }
  }

  /* ------------------------------------------------------------ custom instructions (v0.3.2) */

  const MAX_INSTRUCTIONS = 4000;

  function buildInstructions() {
    const sec = section(LR.t('Custom instructions'), LR.t('Added to every conversation – tell LowRAM AI how you like your answers.'));
    ui.instr = sec;
    const ta = h('textarea', {
      class: 'input textarea instr-input',
      rows: '4',
      maxlength: String(MAX_INSTRUCTIONS),
      placeholder: LR.t('Antworte immer auf Deutsch und fasse dich kurz.\nOr: Always answer in English, with short paragraphs and an example.'),
    });
    const field = LR.field({ label: LR.t('Instructions'), control: ta, hint: LR.t('Up to 4000 characters. Used for every new answer in all chats.') });
    const counter = h('span', { class: 'counter' });
    const status = h('span', { class: 'muted small', role: 'status' });
    const save = LR.button({ text: LR.t('Save instructions'), variant: 'primary', type: 'submit' });
    const form = h('form', { class: 'form', novalidate: true }, [field.el, h('div', { class: 'form-actions' }, [save, status, h('span', { class: 'spacer' }), counter])]);
    const update = () => {
      counter.textContent = fmt.int(ta.value.length) + ' / ' + fmt.int(MAX_INSTRUCTIONS);
      counter.classList.toggle('is-warn', ta.value.length > MAX_INSTRUCTIONS - 200);
    };
    ta.addEventListener('input', () => {
      update();
      status.textContent = ta.value.trim() !== (ui.instrSaved || '') ? LR.t('Not saved yet') : '';
    });
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      LR.setBusy(save, true, LR.t('Saving…'));
      const value = ta.value.trim().slice(0, MAX_INSTRUCTIONS);
      const ok = await saveSetting('chat', { instructions: value }, value ? LR.t('Instructions saved') : LR.t('Instructions cleared'));
      LR.setBusy(save, false);
      if (ok) {
        ui.instrSaved = value;
        ta.value = value;
        update();
        status.textContent = LR.t('Saved');
      }
    });
    ui.instrTa = ta;
    ui.instrUpdate = update;
    update();
    sec.body.append(form);
    return sec.el;
  }

  function syncInstructions() {
    if (!ui.instr) return;
    const s = LR.state.settings;
    const has = Boolean(s && s.chat && typeof s.chat === 'object');
    ui.instr.el.hidden = !has || !LR.has('chat');
    if (has && document.activeElement !== ui.instrTa && ui.instrTa.value.trim() === (ui.instrSaved || '')) {
      ui.instrSaved = LR.str(s.chat.instructions);
      ui.instrTa.value = ui.instrSaved;
      ui.instrUpdate();
    }
  }

  /* ------------------------------------------------------------ MacBook & power (v0.3.2) */

  let powerSupported = null;
  let powerTimer = 0;

  function buildPower() {
    const sec = section(LR.t('MacBook & power'), LR.t('Long jobs keep your Mac awake and can pause when the battery runs low.'));
    ui.power = sec;
    sec.el.hidden = true;
    return sec.el;
  }

  async function loadPower() {
    window.clearTimeout(powerTimer);
    if (!ui.power || powerSupported === false) return;
    const s = LR.state.settings;
    if (!LR.has('power') && !(s && s.power)) {
      ui.power.el.hidden = true;
      return;
    }
    try {
      const data = await LR.get('/api/power');
      powerSupported = true;
      ui.power.el.hidden = false;
      renderPower(data);
    } catch (err) {
      if (err.status === 404) {
        powerSupported = false; // this build has no power module
        ui.power.el.hidden = true;
        return;
      }
    }
    powerTimer = window.setTimeout(loadPower, 15000);
  }

  function renderPower(p) {
    const sig = JSON.stringify(p);
    if (sig === ui.powerSig || (ui.powerRange && document.activeElement === ui.powerRange)) return;
    ui.powerSig = sig;
    const bat = p.battery || {};
    const awake = p.keep_awake || {};
    const cfg = p.settings || (LR.state.settings && LR.state.settings.power) || {};
    const out = [];
    if (bat.available && LR.isNum(bat.percent)) {
      const bar = LR.progress({ size: 'sm', label: LR.t('Battery level'), value: bat.percent / 100 });
      bar.el.classList.toggle('is-error', !bat.plugged && bat.percent < 20);
      bar.el.classList.toggle('is-done', Boolean(bat.plugged));
      out.push(h('div', { class: 'battery-row' }, [
        h('span', { class: 'machine-icon', 'aria-hidden': 'true' }, icon(bat.plugged ? 'bolt' : 'battery')),
        h('div', { class: 'battery-main' }, [
          h('p', { class: 'strong', text: Math.round(bat.percent) + ' % · ' + (bat.plugged ? LR.t('Plugged in') : LR.t('On battery')) }),
          bar.el,
        ]),
      ]));
    }
    const active = Array.isArray(awake.active) ? awake.active.filter(Boolean) : [];
    if (active.length) {
      out.push(h('p', { class: 'awake-note' }, [LR.dot('ok'), h('span', { text: LR.t('Keeping your Mac awake: ') + active.join(', ') })]));
    }
    out.push(LR.switchRow({
      label: LR.t('Keep my Mac awake during long jobs'),
      description: LR.t('Prevents sleep while a task loop, image or video job runs – the display may still turn off.'),
      checked: cfg.keep_awake !== false,
      onChange: (on, input) => saveSetting('power', { keep_awake: on }, on ? LR.t('Will keep your Mac awake') : LR.t('Your Mac may sleep during jobs')).then((ok) => { if (!ok) input.checked = !on; ui.powerSig = ''; }),
    }).el);
    out.push(LR.switchRow({
      label: LR.t('Notify me when long jobs finish'),
      description: LR.t('A system notification when a task loop, an image or video, or a merged result is ready.'),
      checked: cfg.notifications !== false,
      onChange: (on, input) => saveSetting('power', { notifications: on }, on ? LR.t('Notifications on') : LR.t('Notifications off')).then((ok) => { if (!ok) input.checked = !on; ui.powerSig = ''; }),
    }).el);
    const id = LR.uid('battery');
    const value = LR.isNum(cfg.battery_min_percent) ? cfg.battery_min_percent : 20;
    const range = h('input', { class: 'range', type: 'range', id, min: '0', max: '50', step: '5', value: String(value), 'aria-describedby': id + '-help' });
    const output = h('output', { class: 'range-output', for: id });
    const label = (v) => (v === 0 ? LR.t('Never') : LR.t('Below ') + v + ' %');
    const show = () => {
      const v = Number(range.value);
      output.textContent = label(v);
      range.setAttribute('aria-valuetext', v === 0 ? LR.t('Never pause on battery') : LR.t('Pause below ') + v + LR.t(' percent'));
    };
    let timer = 0;
    range.addEventListener('input', () => {
      show();
      window.clearTimeout(timer);
      timer = window.setTimeout(async () => {
        const v = Number(range.value);
        await saveSetting('power', { battery_min_percent: v }, v === 0 ? LR.t('Long jobs never pause on battery') : LR.t('Long jobs pause below ') + v + LR.t(' % battery'));
        ui.powerSig = '';
      }, 400);
    });
    ui.powerRange = range;
    show();
    out.push(h('div', { class: 'range-field' }, [
      h('div', { class: 'range-head' }, [h('label', { class: 'label', for: id, text: LR.t('Pause long jobs on battery') }), output]),
      range,
      h('div', { class: 'range-scale', 'aria-hidden': 'true' }, [h('span', { text: LR.t('Never') }), h('span', { text: '25 %' }), h('span', { text: '50 %' })]),
      h('p', { class: 'help', id: id + '-help', text: LR.t('When your Mac runs on battery below this level, task loops stop and image jobs wait until you plug in.') }),
    ]));
    ui.power.body.replaceChildren(...out);
  }

  /* ------------------------------------------------------------ view */

  function create() {
    const root = h('section', { class: 'view view-page', 'aria-labelledby': 'settings-title' });
    const page = h('div', { class: 'page' });
    const head = LR.pageHead({ title: LR.t('Models & Settings'), sub: LR.t('Choose which model runs on this computer and how much memory LowRAM AI may plan for.') });
    head.querySelector('h1').id = 'settings-title';
    ui.engine = section(LR.t('Model engine'), LR.t('The local server that runs your chat model.'));
    ui.models = section(LR.t('Models'), LR.t('Smaller models are faster and need less memory; larger ones answer better.'));
    ui.downloads = section('Downloads');
    ui.downloadsList = h('div', { class: 'dl-list' });
    ui.downloads.body.append(ui.downloadsList);
    ui.about = section(LR.t('About this build'));
    const own = buildOwnModels();
    const storage = buildStorage();
    own.hidden = true;
    storage.hidden = true;
    page.append(head, ui.engine.el, ui.models.el, own, ui.downloads.el, storage, buildMemory(), buildInstructions(), buildPrefs(), buildPower(), ui.about.el);
    root.append(page);
    return root;
  }

  LR.registerView('settings', {
    title: LR.t('Models & Settings'),
    create,
    show() {
      const engineOk = LR.has('engine');
      ui.engine.el.hidden = false;
      ui.downloads.el.hidden = !engineOk && !LR.has('media');
      renderEngine();
      renderModels();
      renderAbout();
      syncPrefs();
      ui.storage.el.hidden = true;
      syncExtras();
      syncInstructions();
      loadPower();
      renderMemory(LR.state.memory);
      if (engineOk) LR.loadCatalog().catch(() => {});
      if (!unmountDownloads && !ui.downloads.el.hidden) {
        unmountDownloads = LR.downloads.mount(ui.downloadsList, null, { empty: h('p', { class: 'muted', text: LR.t('No downloads yet. Models you install show their progress here.') }) });
      }
      offs.push(
        LR.on('engine', renderEngine),
        LR.on('catalog', () => { renderModels(); renderEngine(); syncExtras(); }),
        LR.on('memory', (m) => { if (document.activeElement !== ui.range) renderMemory(m); }),
        LR.on('settings', () => { syncPrefs(); syncInstructions(); }),
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
      window.clearTimeout(storageTimer);
      window.clearTimeout(powerTimer);
      while (offs.length) offs.pop()();
      if (unmountDownloads) { unmountDownloads(); unmountDownloads = null; }
    },
  });
})();
