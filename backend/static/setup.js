/* LowRAM AI Studio – first-run setup: detect RAM, recommend a model, then
 * install the runtime + model and start the engine with one click. */
(function () {
  'use strict';

  const LR = window.LR;
  const h = LR.h;
  const icon = LR.icon;
  const fmt = LR.fmt;
  const GB = 1024 * 1024 * 1024;
  const ui = {};
  const st = { selected: null, running: false, done: false, current: null };
  const STEPS = [
    ['runtime', LR.t('Install the engine'), LR.t('The small program that runs models (llama.cpp).')],
    ['model', LR.t('Download the model'), LR.t('A one-time download; it stays on this computer.')],
    ['start', LR.t('Start the model'), LR.t('Loads the model into memory.')],
  ];

  function create() {
    const root = h('section', { class: 'view view-setup', 'aria-labelledby': 'setup-title' });
    ui.inner = h('div', { class: 'setup' });
    root.append(ui.inner);
    return root;
  }

  function hero() {
    return h('header', { class: 'setup-hero' }, [
      h('span', { class: 'welcome-mark', 'aria-hidden': 'true' }, icon('logo')),
      h('h1', { class: 'setup-title', id: 'setup-title', text: LR.t('Welcome to LowRAM AI') }),
      h('p', { class: 'setup-sub', text: LR.t('Your own private assistant. It runs entirely on this computer – a little slower than the cloud, but nothing ever leaves your machine.') }),
    ]);
  }

  const PURPOSES = [
    { value: 'code', label: 'Programming', icon: 'code', desc: 'Code, scripts and debugging' },
    { value: 'chat', label: 'Writing', icon: 'pencil', desc: 'Texts, emails and research' },
    { value: 'general', label: 'Everything', icon: 'sparkle', desc: 'A bit of everything' },
  ];

  function currentPurpose() {
    const s = LR.state.settings && LR.state.settings.ui;
    const v = (s && s.purpose) || LR.store.get('lowram.purpose');
    return v === 'code' || v === 'chat' ? v : 'general';
  }

  /** Stored in settings.ui.purpose when the backend knows the key, else locally. */
  LR.savePurpose = function (value) {
    LR.store.set('lowram.purpose', value);
    const s = LR.state.settings && LR.state.settings.ui;
    if (s && 'purpose' in s) return LR.saveSetting('ui', { purpose: value });
    return Promise.resolve(true);
  };

  function pickDefault(models, total, purpose) {
    const fits = (m) => !total || !LR.isNum(m.min_ram_gb) || m.min_ram_gb * GB <= total;
    const hinted = models.filter((m) => Array.isArray(m.default_for) && m.default_for.includes(purpose) && fits(m));
    if (hinted.length) {
      return hinted.find((m) => m.recommended)
        || [...hinted].sort((a, b) => (b.size_bytes || 0) - (a.size_bytes || 0))[0];
    }
    if (purpose === 'code') {
      const coder = models.find((m) => m.purpose === 'code' && fits(m));
      if (coder) return coder;
    }
    return models.find((m) => m.recommended && m.purpose !== 'code' && fits(m))
      || models.find((m) => m.recommended && fits(m))
      || models.filter(fits).sort((a, b) => (a.size_bytes || 0) - (b.size_bytes || 0))[0]
      || models[0] || null;
  }

  async function render() {
    if (!LR.has('engine')) {
      ui.inner.replaceChildren(hero(), LR.callout({
        tone: 'info',
        title: LR.t('Nothing to set up here'),
        text: LR.t('This build of LowRAM AI uses the model server configured with LOWRAM_LLM_URL, so there is nothing to install.'),
        actions: [LR.button({ text: LR.t('Open Models & Settings'), variant: 'primary', onclick: () => LR.go('#/settings') })],
      }));
      return;
    }
    if (st.running || st.done) return;
    ui.inner.replaceChildren(hero(), h('div', { class: 'card setup-card' }, LR.skeleton(6)));
    let eng;
    let cat;
    let mem;
    try {
      [eng, cat, mem] = await Promise.all([
        LR.get('/api/engine/status'),
        LR.loadCatalog(),
        LR.get('/api/memory?percent=' + LR.budgetPercent()).catch(() => null),
      ]);
    } catch (err) {
      ui.inner.replaceChildren(hero(), LR.callout({
        tone: 'error', title: LR.t('Could not check this computer'), text: err.message,
        actions: [LR.button({ text: LR.t('Try again'), icon: 'retry', onclick: render })],
      }));
      return;
    }
    LR.state.engine = eng;
    if (eng.external) {
      ui.inner.replaceChildren(hero(), LR.callout({
        tone: 'ok', title: LR.t('You are already set up'),
        text: LR.t('LowRAM AI uses your external model server, so there is nothing to install.'),
        actions: [LR.button({ text: LR.t('Start chatting'), variant: 'primary', icon: 'chat', onclick: () => LR.go('#/chat/new') })],
      }));
      return;
    }
    st.data = { cat, mem };
    draw();
  }

  function draw() {
    if (st.running || st.done || !st.data) return;
    const { cat, mem } = st.data;
    if (!st.purpose) st.purpose = currentPurpose();
    const total = (mem && mem.total_bytes) || cat.ram_total_bytes || 0;
    const models = (Array.isArray(cat.models) ? [...cat.models] : [])
      .sort((a, b) => (b.recommended - a.recommended) || ((a.size_bytes || 0) - (b.size_bytes || 0)));
    const recommended = pickDefault(models, total, st.purpose);
    if (!st.userPicked || !st.selected || !models.some((m) => m.id === st.selected)) st.selected = recommended ? recommended.id : null;

    const machine = h('div', { class: 'card setup-machine' }, [
      h('div', { class: 'machine-icon', 'aria-hidden': 'true' }, icon('laptop')),
      h('div', null, [
        h('p', { class: 'machine-title', text: LR.t('This computer has ') + fmt.gb(total) + LR.t(' of memory') }),
        h('p', { class: 'muted', text: (mem ? fmt.gb(mem.available_bytes) + LR.t(' is free right now. ') : '') + (recommended ? LR.t('We recommend ') + (recommended.name || recommended.id) + LR.t(' – a good balance of quality and speed for this machine.') : '') }),
      ]),
    ]);

    const purpose = LR.segmented({
      label: LR.t('What will you mainly use LowRAM AI for?'),
      cls: 'purpose-seg',
      value: st.purpose,
      options: PURPOSES.map((p) => ({ value: p.value, label: LR.t(p.label), icon: p.icon, title: LR.t(p.desc) })),
      onChange: (value) => {
        st.purpose = value;
        st.userPicked = false;
        LR.savePurpose(value);
        draw();
        const again = ui.inner.querySelector('.purpose-seg [aria-checked="true"]');
        if (again) again.focus();
      },
    });
    const purposeCard = h('section', { class: 'card setup-card setup-purpose', 'aria-labelledby': 'setup-purpose-title' }, [
      h('h2', { class: 'card-title', id: 'setup-purpose-title', text: LR.t('What will you mainly use LowRAM AI for?') }),
      h('p', { class: 'card-sub', text: LR.t('This only changes the recommendation – every model below stays available.') }),
      purpose.el,
    ]);

    const choices = models.map((m) => {
      const tooBig = total && LR.isNum(m.min_ram_gb) && m.min_ram_gb * GB > total;
      const input = h('input', { type: 'radio', name: 'setup-model', value: m.id, class: 'choice-input', checked: m.id === st.selected });
      input.addEventListener('change', () => {
        st.selected = m.id;
        st.userPicked = true;
        ui.go.querySelector('span').textContent = goLabel(models);
      });
      const hinted = Array.isArray(m.default_for) && m.default_for.includes(st.purpose);
      return h('label', { class: 'choice' + (tooBig ? ' is-tight' : '') }, [
        input,
        h('span', { class: 'choice-radio', 'aria-hidden': 'true' }),
        h('span', { class: 'choice-body' }, [
          h('span', { class: 'choice-head' }, [
            h('span', { class: 'choice-name', text: m.name || m.id }),
            m.id === (recommended && recommended.id) || (hinted && !tooBig) ? LR.badge(LR.t('Recommended'), 'accent') : null,
            m.installed ? LR.badge(LR.t('Installed'), 'ok') : null,
            m.purpose === 'code' ? LR.badge('Code', 'info') : null,
          ]),
          m.description ? h('span', { class: 'choice-desc', text: m.description }) : null,
          h('span', { class: 'choice-facts', text: [
            m.installed ? LR.t('Already downloaded') : fmt.bytes(m.size_bytes) + LR.t(' download'),
            LR.isNum(m.min_ram_gb) ? LR.t('needs ') + fmt.ramGb(m.min_ram_gb) + ' RAM' : '',
            tooBig ? LR.t('too large for this computer') : '',
          ].filter(Boolean).join(' · ') }),
        ]),
      ]);
    });
    ui.go = LR.button({ text: goLabel(models), icon: 'download', variant: 'primary', cls: 'btn-lg', onclick: () => run(models) });
    const skip = LR.button({ text: LR.t('Skip for now'), variant: 'ghost', onclick: () => {
      LR.store.set('lowram.setupSkipped', '1');
      LR.go('#/chat/new');
    } });
    const pick = h('fieldset', { class: 'card setup-card choices' }, [
      h('legend', { class: 'card-title', text: LR.t('All models') }),
      h('p', { class: 'card-sub', text: LR.t('The recommendation is only a suggestion – pick whichever model you like.') + ' ' + LR.t('You can add or switch models later in Models & Settings.') }),
      models.length ? h('div', { class: 'choice-list' }, choices) : LR.empty({ icon: 'cpu', title: LR.t('No models available'), text: LR.t('The catalog of this build is empty.') }),
    ]);
    ui.inner.replaceChildren(...[hero(), machine, purposeCard, pick, hfBox(models), ownModelBox(models), h('div', { class: 'setup-actions' }, [ui.go, skip]),
      h('p', { class: 'setup-note' }, [icon('shield'), h('span', { text: LR.t('Downloads come only from trusted sources over HTTPS and are verified before use.') })])].filter(Boolean));
  }

  /** "Load your own model from Hugging Face" right inside the wizard. */
  function hfBox(models) {
    if (!models.some((m) => 'source' in m) || typeof LR.renderResolvedFiles !== 'function') return null;
    const source = h('input', { class: 'input', type: 'text', placeholder: LR.t('e.g. bartowski/Llama-3.2-3B-Instruct-GGUF or a Hugging Face link'), spellcheck: 'false', autocomplete: 'off' });
    const field = LR.field({ label: LR.t('Hugging Face model'), control: source, hint: LR.t('Paste the name or the link. LowRAM AI lists the files and recommends one that fits your memory.') });
    const find = LR.button({ text: LR.t('Find files'), icon: 'search', variant: 'secondary', type: 'submit' });
    const row = h('div', { class: 'input-row' });
    source.replaceWith(row);
    row.append(source, find);
    const results = h('div', { class: 'resolve-results', 'aria-live': 'polite' });
    const note = h('p', { class: 'muted small', hidden: true, text: LR.t('Download started – the model appears in the list above when it is ready.') });
    const form = h('form', { class: 'form', novalidate: true }, [field.el, results, note]);
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      field.setError('');
      const value = source.value.trim();
      if (!value) {
        field.setError(LR.t('Paste a model name like owner/model-GGUF or a link.'));
        source.focus();
        return;
      }
      LR.setBusy(find, true, LR.t('Looking…'));
      results.replaceChildren(LR.skeleton(3));
      try {
        const data = await LR.post('/api/engine/custom/resolve', { source: value });
        LR.renderResolvedFiles(results, data, () => { note.hidden = false; watchDownloads(); });
      } catch (err) {
        results.replaceChildren();
        field.setError(err.message);
      } finally {
        LR.setBusy(find, false);
      }
    });
    const search = LR.hfSearch ? LR.hfSearch((repo) => { source.value = repo; LR.submitForm(form); }) : null;
    return h('section', { class: 'card setup-card setup-own', 'aria-labelledby': 'setup-hf-title' }, [
      h('h2', { class: 'card-title', id: 'setup-hf-title', text: LR.t('Load your own model from Hugging Face') }),
      search ? search.el : null,
      form,
    ]);
  }

  /** Re-draw the wizard once a custom download finishes so the model shows up. */
  function watchDownloads() {
    if (st.unwatch) return;
    let active = -1;
    st.unwatch = LR.downloads.subscribe((list) => {
      const n = list.filter(LR.downloads.isActive).length;
      if (active > 0 && n < active) {
        LR.loadCatalog().then((cat) => {
          if (cat && st.data) { st.data.cat = cat; draw(); }
        }).catch(() => {});
      }
      active = n;
    });
  }

  /** "Use a model file I already have" + link to Hugging Face models (v0.3.1 backends). */
  function ownModelBox(models) {
    if (!models.some((m) => 'source' in m)) return null;
    const input = h('input', { class: 'input mono', type: 'text', placeholder: '/Volumes/External/models/my-model.gguf', spellcheck: 'false', autocomplete: 'off' });
    const field = LR.field({ label: LR.t('Use a model file I already have'), control: input, hint: LR.t('A .gguf file. It stays where it is – an external drive works too.') });
    const row = h('div', { class: 'input-row' });
    input.replaceWith(row);
    row.append(input);
    const api = window.pywebview && window.pywebview.api;
    if (api && typeof api.pick_file === 'function') {
      row.append(LR.button({ text: LR.t('Choose…'), icon: 'folder', onclick: async () => {
        try {
          const picked = await api.pick_file(LR.t('Choose a GGUF model file'));
          if (picked) input.value = String(picked);
        } catch (_) { /* dialog closed */ }
      } }));
    }
    const add = LR.button({ text: LR.t('Use this file'), icon: 'plus', variant: 'secondary', type: 'submit' });
    row.append(add);
    const form = h('form', { class: 'form', novalidate: true }, field.el);
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      field.setError('');
      const path = input.value.trim();
      if (!/\.gguf$/i.test(path) || !/^(\/|~|[A-Za-z]:[\\/])/.test(path)) {
        field.setError(LR.t('Enter the full path of a .gguf file.'));
        input.focus();
        return;
      }
      LR.setBusy(add, true, LR.t('Adding…'));
      try {
        const data = await LR.post('/api/engine/custom/import', { path });
        if (data.model && data.model.id) st.selected = data.model.id;
        LR.toast(LR.t('Added ') + LR.str(data.model && data.model.name, LR.t('your model')));
        await LR.loadCatalog();
        render();
      } catch (err) {
        field.setError(err.message);
        LR.setBusy(add, false);
      }
    });
    return h('section', { class: 'card setup-card setup-own', 'aria-labelledby': 'setup-own-title' }, [
      h('h2', { class: 'card-title', id: 'setup-own-title', text: LR.t('Already have a model?') }),
      form,
    ]);
  }

  function goLabel(models) {
    const m = models.find((x) => x.id === st.selected);
    if (!m) return LR.t('Install & start');
    return m.installed ? LR.t('Start ') + (m.name || m.id) : LR.t('Install & start');
  }

  /* ------------------------------------------------------------ install flow */

  function buildProgress(model) {
    ui.steps = new Map();
    const list = h('ol', { class: 'steps' });
    for (const [key, title, desc] of STEPS) {
      const marker = h('span', { class: 'step-marker', 'aria-hidden': 'true' });
      const detail = h('p', { class: 'step-detail', text: desc });
      const slot = h('div', { class: 'step-slot' });
      const stateText = h('span', { class: 'visually-hidden', text: 'waiting' });
      const li = h('li', { class: 'step is-pending' }, [marker, h('div', { class: 'step-body' }, [h('p', { class: 'step-title' }, [title, ' ', stateText]), detail, slot])]);
      ui.steps.set(key, { li, detail, slot, marker, stateText });
      list.append(li);
    }
    ui.result = h('div', { class: 'setup-result', 'aria-live': 'polite' });
    ui.inner.replaceChildren(hero(), h('section', { class: 'card setup-card', 'aria-labelledby': 'setup-progress-title' }, [
      h('h2', { class: 'card-title', id: 'setup-progress-title', text: LR.t('Setting up ') + (model.name || model.id) }),
      h('p', { class: 'card-sub', text: LR.t('You can keep this window open or use other parts of LowRAM AI meanwhile.') }),
      list,
    ]), ui.result);
  }

  function setStep(key, state, detail) {
    const s = ui.steps.get(key);
    if (!s) return;
    s.li.className = 'step is-' + state;
    s.stateText.textContent = LR.t({ pending: 'waiting', active: 'in progress', done: 'done', error: 'failed' }[state] || state);
    s.marker.replaceChildren(state === 'done' ? icon('check') : state === 'error' ? icon('x') : state === 'active' ? LR.spinner() : '');
    if (detail) s.detail.textContent = detail;
    if (state === 'active') st.current = key;
  }

  async function follow(download, key, label) {
    const s = ui.steps.get(key);
    const row = LR.downloads.row(download, { label });
    s.slot.replaceChildren(row.el);
    await LR.downloads.wait(download.id, (d) => row.update(d));
  }

  async function installWithReuse(path, model, key, label) {
    let download;
    try {
      const data = await LR.post(path, {});
      download = data.download || (Array.isArray(data.downloads) ? data.downloads[0] : null);
    } catch (err) {
      if (err.status !== 409) throw err;
      if (err.data && err.data.suggestion) {
        const data = await LR.offerSuggestion(err, path);
        if (!data) throw err;
        download = data.download || (Array.isArray(data.downloads) ? data.downloads[0] : null);
      } else {
        await LR.downloads.refresh();
        download = model ? LR.downloadFor(model) : LR.downloads.list.find((d) => d.kind === 'runtime' && LR.downloads.isActive(d));
        if (!download) throw err;
      }
    }
    if (download) await follow(download, key, label);
  }

  async function waitReady(modelId) {
    const started = Date.now();
    for (;;) {
      const eng = await LR.get('/api/engine/status');
      LR.state.engine = eng;
      LR.emit('engine', eng);
      const server = eng.server || {};
      if (server.state === 'ready' && (!modelId || !server.model_id || server.model_id === modelId)) return;
      if (server.state === 'error') throw new Error(server.error || LR.t('The model could not be started.'));
      if (server.state === 'stopped' && Date.now() - started > 15000) throw new Error(LR.t('The engine stopped before the model was ready.'));
      if (Date.now() - started > 5 * 60 * 1000) throw new Error(LR.t('The model is taking unusually long to start. Check Models & Settings.'));
      await new Promise((r) => window.setTimeout(r, 1000));
    }
  }

  async function run(models) {
    const model = models.find((m) => m.id === st.selected);
    if (!model || st.running) return;
    st.running = true;
    st.done = false;
    buildProgress(model);
    const name = model.name || model.id;
    try {
      let eng = await LR.get('/api/engine/status');
      if (!(eng.runtime && eng.runtime.installed)) {
        setStep('runtime', 'active', LR.t('Downloading the engine…'));
        await installWithReuse('/api/engine/runtime/install', null, 'runtime', 'Engine (llama.cpp)');
        eng = await LR.get('/api/engine/status');
        if (!(eng.runtime && eng.runtime.installed)) throw new Error(LR.t('The engine download finished, but the engine is still not installed.'));
        setStep('runtime', 'done', LR.t('Installed.'));
      } else {
        setStep('runtime', 'done', LR.t('Already installed') + (eng.runtime.version ? ' (' + eng.runtime.version + ')' : '') + '.');
      }

      let cat = await LR.loadCatalog();
      let entry = cat.models.find((m) => m.id === model.id);
      if (!entry || !entry.installed) {
        setStep('model', 'active', LR.t('Downloading ') + name + ' (' + fmt.bytes(model.size_bytes) + ')…');
        await installWithReuse('/api/engine/models/' + encodeURIComponent(model.id) + '/install', model, 'model', name);
        cat = await LR.loadCatalog();
        entry = cat.models.find((m) => m.id === model.id);
        if (!entry || !entry.installed) throw new Error(LR.t('The download finished, but ') + name + LR.t(' is not listed as installed. Please try again.'));
        setStep('model', 'done', name + LR.t(' is downloaded.'));
      } else {
        setStep('model', 'done', name + LR.t(' is already downloaded.'));
      }

      setStep('start', 'active', LR.t('Loading ') + name + LR.t(' into memory – this can take a minute…'));
      const elapsed = h('span', { class: 'step-elapsed' });
      ui.steps.get('start').slot.replaceChildren(h('p', { class: 'muted small' }, [LR.t('Elapsed '), elapsed]));
      const stopTimer = LR.elapsed(elapsed);
      try {
        await LR.post('/api/engine/start', { model_id: model.id });
        await waitReady(model.id);
      } finally {
        stopTimer();
      }
      LR.saveSetting('engine', { model_id: model.id });
      setStep('start', 'done', name + LR.t(' is running.'));
      ui.steps.get('start').slot.replaceChildren();
      st.done = true;
      LR.store.remove('lowram.setupSkipped');
      LR.refreshStatus();
      LR.announce(LR.t('Setup finished. ') + name + LR.t(' is ready.'));
      const startBtn = LR.button({ text: LR.t('Start chatting'), icon: 'chat', variant: 'primary', cls: 'btn-lg', onclick: () => LR.go('#/chat/new') });
      ui.result.replaceChildren(h('div', { class: 'setup-done' }, [
        h('span', { class: 'done-icon', 'aria-hidden': 'true' }, icon('check')),
        h('div', null, [h('p', { class: 'done-title', text: LR.t('All set – ') + name + LR.t(' is ready.') }), h('p', { class: 'muted', text: LR.t('Everything you type stays on this computer.') })]),
        startBtn,
      ]));
      startBtn.focus();
    } catch (err) {
      setStep(st.current || 'runtime', 'error', err.message);
      ui.result.replaceChildren(LR.callout({
        tone: 'error',
        role: 'alert',
        title: LR.t('Setup did not finish'),
        text: err.message,
        actions: [
          LR.button({ text: LR.t('Try again'), icon: 'retry', variant: 'primary', onclick: () => { st.running = false; run(models); } }),
          LR.button({ text: LR.t('Choose another model'), variant: 'secondary', onclick: () => { st.running = false; render(); } }),
          LR.button({ text: LR.t('Skip for now'), variant: 'ghost', onclick: () => { st.running = false; LR.store.set('lowram.setupSkipped', '1'); LR.go('#/chat/new'); } }),
        ],
      }));
      return;
    } finally {
      if (!st.done) st.running = false;
    }
    st.running = false;
  }

  LR.registerView('setup', {
    title: LR.t('Welcome'),
    create,
    show() {
      if (st.done) {
        st.done = false;
      }
      render();
    },
    hide() {
      if (st.unwatch) { st.unwatch(); st.unwatch = null; }
    },
  });
})();
