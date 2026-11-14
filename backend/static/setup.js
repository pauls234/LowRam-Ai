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
    ['runtime', 'Install the engine', 'The small program that runs models (llama.cpp).'],
    ['model', 'Download the model', 'A one-time download; it stays on this computer.'],
    ['start', 'Start the model', 'Loads the model into memory.'],
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
      h('h1', { class: 'setup-title', id: 'setup-title', text: 'Welcome to LowRAM AI' }),
      h('p', { class: 'setup-sub', text: 'Your own private assistant. It runs entirely on this computer – a little slower than the cloud, but nothing ever leaves your machine.' }),
    ]);
  }

  function pickDefault(models, total) {
    const fits = (m) => !total || !LR.isNum(m.min_ram_gb) || m.min_ram_gb * GB <= total;
    return models.find((m) => m.recommended && m.purpose !== 'code' && fits(m))
      || models.find((m) => m.recommended && fits(m))
      || models.filter(fits).sort((a, b) => (a.size_bytes || 0) - (b.size_bytes || 0))[0]
      || models[0] || null;
  }

  async function render() {
    if (!LR.has('engine')) {
      ui.inner.replaceChildren(hero(), LR.callout({
        tone: 'info',
        title: 'Nothing to set up here',
        text: 'This build of LowRAM AI uses the model server configured with LOWRAM_LLM_URL, so there is nothing to install.',
        actions: [LR.button({ text: 'Open Models & Settings', variant: 'primary', onclick: () => LR.go('#/settings') })],
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
        tone: 'error', title: 'Could not check this computer', text: err.message,
        actions: [LR.button({ text: 'Try again', icon: 'retry', onclick: render })],
      }));
      return;
    }
    LR.state.engine = eng;
    if (eng.external) {
      ui.inner.replaceChildren(hero(), LR.callout({
        tone: 'ok', title: 'You are already set up',
        text: 'LowRAM AI uses your external model server, so there is nothing to install.',
        actions: [LR.button({ text: 'Start chatting', variant: 'primary', icon: 'chat', onclick: () => LR.go('#/chat/new') })],
      }));
      return;
    }
    const total = (mem && mem.total_bytes) || cat.ram_total_bytes || 0;
    const models = (Array.isArray(cat.models) ? [...cat.models] : [])
      .sort((a, b) => (b.recommended - a.recommended) || ((a.size_bytes || 0) - (b.size_bytes || 0)));
    const recommended = pickDefault(models, total);
    if (!st.selected || !models.some((m) => m.id === st.selected)) st.selected = recommended ? recommended.id : null;

    const machine = h('div', { class: 'card setup-machine' }, [
      h('div', { class: 'machine-icon', 'aria-hidden': 'true' }, icon('laptop')),
      h('div', null, [
        h('p', { class: 'machine-title', text: 'This computer has ' + fmt.gb(total) + ' of memory' }),
        h('p', { class: 'muted', text: (mem ? fmt.gb(mem.available_bytes) + ' is free right now. ' : '') + (recommended ? 'We recommend ' + (recommended.name || recommended.id) + ' – a good balance of quality and speed for this machine.' : '') }),
      ]),
    ]);

    const choices = models.map((m) => {
      const tooBig = total && LR.isNum(m.min_ram_gb) && m.min_ram_gb * GB > total;
      const input = h('input', { type: 'radio', name: 'setup-model', value: m.id, class: 'choice-input', checked: m.id === st.selected });
      input.addEventListener('change', () => {
        st.selected = m.id;
        ui.go.querySelector('span').textContent = goLabel(models);
      });
      return h('label', { class: 'choice' + (tooBig ? ' is-tight' : '') }, [
        input,
        h('span', { class: 'choice-radio', 'aria-hidden': 'true' }),
        h('span', { class: 'choice-body' }, [
          h('span', { class: 'choice-head' }, [
            h('span', { class: 'choice-name', text: m.name || m.id }),
            m.id === (recommended && recommended.id) ? LR.badge('Recommended', 'accent') : null,
            m.installed ? LR.badge('Installed', 'ok') : null,
            m.purpose === 'code' ? LR.badge('Code', 'info') : null,
          ]),
          m.description ? h('span', { class: 'choice-desc', text: m.description }) : null,
          h('span', { class: 'choice-facts', text: [
            m.installed ? 'Already downloaded' : fmt.bytes(m.size_bytes) + ' download',
            LR.isNum(m.min_ram_gb) ? 'needs ' + fmt.ramGb(m.min_ram_gb) + ' RAM' : '',
            tooBig ? 'too large for this computer' : '',
          ].filter(Boolean).join(' · ') }),
        ]),
      ]);
    });
    ui.go = LR.button({ text: goLabel(models), icon: 'download', variant: 'primary', cls: 'btn-lg', onclick: () => run(models) });
    const skip = LR.button({ text: 'Skip for now', variant: 'ghost', onclick: () => {
      LR.store.set('lowram.setupSkipped', '1');
      LR.go('#/chat/new');
    } });
    const pick = h('fieldset', { class: 'card setup-card choices' }, [
      h('legend', { class: 'card-title', text: 'Choose a model' }),
      h('p', { class: 'card-sub', text: 'You can add or switch models later in Models & Settings.' }),
      models.length ? h('div', { class: 'choice-list' }, choices) : LR.empty({ icon: 'cpu', title: 'No models available', text: 'The catalog of this build is empty.' }),
    ]);
    ui.inner.replaceChildren(...[hero(), machine, pick, ownModelBox(models), h('div', { class: 'setup-actions' }, [ui.go, skip]),
      h('p', { class: 'setup-note' }, [icon('shield'), h('span', { text: 'Downloads come only from trusted sources over HTTPS and are verified before use.' })])].filter(Boolean));
  }

  /** "Use a model file I already have" + link to Hugging Face models (v0.3.1 backends). */
  function ownModelBox(models) {
    if (!models.some((m) => 'source' in m)) return null;
    const input = h('input', { class: 'input mono', type: 'text', placeholder: '/Volumes/External/models/my-model.gguf', spellcheck: 'false', autocomplete: 'off' });
    const field = LR.field({ label: 'Use a model file I already have', control: input, hint: 'A .gguf file. It stays where it is – an external drive works too.' });
    const row = h('div', { class: 'input-row' });
    input.replaceWith(row);
    row.append(input);
    const api = window.pywebview && window.pywebview.api;
    if (api && typeof api.pick_file === 'function') {
      row.append(LR.button({ text: 'Choose…', icon: 'folder', onclick: async () => {
        try {
          const picked = await api.pick_file('Choose a GGUF model file');
          if (picked) input.value = String(picked);
        } catch (_) { /* dialog closed */ }
      } }));
    }
    const add = LR.button({ text: 'Use this file', icon: 'plus', variant: 'secondary', type: 'submit' });
    row.append(add);
    const form = h('form', { class: 'form', novalidate: true }, field.el);
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      field.setError('');
      const path = input.value.trim();
      if (!/\.gguf$/i.test(path) || !/^(\/|~|[A-Za-z]:[\\/])/.test(path)) {
        field.setError('Enter the full path of a .gguf file.');
        input.focus();
        return;
      }
      LR.setBusy(add, true, 'Adding…');
      try {
        const data = await LR.post('/api/engine/custom/import', { path });
        if (data.model && data.model.id) st.selected = data.model.id;
        LR.toast('Added ' + LR.str(data.model && data.model.name, 'your model'));
        await LR.loadCatalog();
        render();
      } catch (err) {
        field.setError(err.message);
        LR.setBusy(add, false);
      }
    });
    return h('section', { class: 'card setup-card setup-own', 'aria-labelledby': 'setup-own-title' }, [
      h('h2', { class: 'card-title', id: 'setup-own-title', text: 'Already have a model?' }),
      form,
      h('p', { class: 'muted small' }, ['Looking for something else? ', h('a', { href: '#/settings', text: 'Add a model from Hugging Face' }), ' in Models & Settings.']),
    ]);
  }

  function goLabel(models) {
    const m = models.find((x) => x.id === st.selected);
    if (!m) return 'Install & start';
    return m.installed ? 'Start ' + (m.name || m.id) : 'Install & start';
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
      h('h2', { class: 'card-title', id: 'setup-progress-title', text: 'Setting up ' + (model.name || model.id) }),
      h('p', { class: 'card-sub', text: 'You can keep this window open or use other parts of LowRAM AI meanwhile.' }),
      list,
    ]), ui.result);
  }

  function setStep(key, state, detail) {
    const s = ui.steps.get(key);
    if (!s) return;
    s.li.className = 'step is-' + state;
    s.stateText.textContent = { pending: 'waiting', active: 'in progress', done: 'done', error: 'failed' }[state] || state;
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
      await LR.downloads.refresh();
      download = model ? LR.downloadFor(model) : LR.downloads.list.find((d) => d.kind === 'runtime' && LR.downloads.isActive(d));
      if (!download) throw err;
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
      if (server.state === 'error') throw new Error(server.error || 'The model could not be started.');
      if (server.state === 'stopped' && Date.now() - started > 15000) throw new Error('The engine stopped before the model was ready.');
      if (Date.now() - started > 5 * 60 * 1000) throw new Error('The model is taking unusually long to start. Check Models & Settings.');
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
        setStep('runtime', 'active', 'Downloading the engine…');
        await installWithReuse('/api/engine/runtime/install', null, 'runtime', 'Engine (llama.cpp)');
        eng = await LR.get('/api/engine/status');
        if (!(eng.runtime && eng.runtime.installed)) throw new Error('The engine download finished, but the engine is still not installed.');
        setStep('runtime', 'done', 'Installed.');
      } else {
        setStep('runtime', 'done', 'Already installed' + (eng.runtime.version ? ' (' + eng.runtime.version + ')' : '') + '.');
      }

      let cat = await LR.loadCatalog();
      let entry = cat.models.find((m) => m.id === model.id);
      if (!entry || !entry.installed) {
        setStep('model', 'active', 'Downloading ' + name + ' (' + fmt.bytes(model.size_bytes) + ')…');
        await installWithReuse('/api/engine/models/' + encodeURIComponent(model.id) + '/install', model, 'model', name);
        cat = await LR.loadCatalog();
        entry = cat.models.find((m) => m.id === model.id);
        if (!entry || !entry.installed) throw new Error('The download finished, but ' + name + ' is not listed as installed. Please try again.');
        setStep('model', 'done', name + ' is downloaded.');
      } else {
        setStep('model', 'done', name + ' is already downloaded.');
      }

      setStep('start', 'active', 'Loading ' + name + ' into memory – this can take a minute…');
      const elapsed = h('span', { class: 'step-elapsed' });
      ui.steps.get('start').slot.replaceChildren(h('p', { class: 'muted small' }, ['Elapsed ', elapsed]));
      const stopTimer = LR.elapsed(elapsed);
      try {
        await LR.post('/api/engine/start', { model_id: model.id });
        await waitReady(model.id);
      } finally {
        stopTimer();
      }
      LR.saveSetting('engine', { model_id: model.id });
      setStep('start', 'done', name + ' is running.');
      ui.steps.get('start').slot.replaceChildren();
      st.done = true;
      LR.store.remove('lowram.setupSkipped');
      LR.refreshStatus();
      LR.announce('Setup finished. ' + name + ' is ready.');
      const startBtn = LR.button({ text: 'Start chatting', icon: 'chat', variant: 'primary', cls: 'btn-lg', onclick: () => LR.go('#/chat/new') });
      ui.result.replaceChildren(h('div', { class: 'setup-done' }, [
        h('span', { class: 'done-icon', 'aria-hidden': 'true' }, icon('check')),
        h('div', null, [h('p', { class: 'done-title', text: 'All set – ' + name + ' is ready.' }), h('p', { class: 'muted', text: 'Everything you type stays on this computer.' })]),
        startBtn,
      ]));
      startBtn.focus();
    } catch (err) {
      setStep(st.current || 'runtime', 'error', err.message);
      ui.result.replaceChildren(LR.callout({
        tone: 'error',
        role: 'alert',
        title: 'Setup did not finish',
        text: err.message,
        actions: [
          LR.button({ text: 'Try again', icon: 'retry', variant: 'primary', onclick: () => { st.running = false; run(models); } }),
          LR.button({ text: 'Choose another model', variant: 'secondary', onclick: () => { st.running = false; render(); } }),
          LR.button({ text: 'Skip for now', variant: 'ghost', onclick: () => { st.running = false; LR.store.set('lowram.setupSkipped', '1'); LR.go('#/chat/new'); } }),
        ],
      }));
      return;
    } finally {
      if (!st.done) st.running = false;
    }
    st.running = false;
  }

  LR.registerView('setup', {
    title: 'Welcome',
    create,
    show() {
      if (st.done) {
        st.done = false;
      }
      render();
    },
    hide() {},
  });
})();
