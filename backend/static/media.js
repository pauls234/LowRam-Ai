/* LowRAM AI Studio – Images & Video: runtime/model install, prompt form,
 * job queue with progress, gallery and lightbox. Media files come from the
 * same origin (/api/media/jobs/<id>/file). */
(function () {
  'use strict';

  const LR = window.LR;
  const h = LR.h;
  const icon = LR.icon;
  const fmt = LR.fmt;
  const PRESETS = {
    image: [
      { id: 'square', label: 'Square', w: 512, h: 512 },
      { id: 'portrait', label: 'Portrait', w: 512, h: 768 },
      { id: 'landscape', label: 'Landscape', w: 768, h: 512 },
    ],
    video: [
      { id: 'wide', label: 'Wide', w: 832, h: 480 },
      { id: 'square', label: 'Square', w: 512, h: 512 },
      { id: 'tall', label: 'Tall', w: 480, h: 832 },
    ],
  };
  const ACTIVE = new Set(['queued', 'running']);
  const ui = {};
  const st = { kind: 'image', status: null, jobs: [], loaded: false, preset: { image: 'square', video: 'wide' }, offs: [] };

  const safeUrl = (url) => (typeof url === 'string' && /^\/api\/media\/jobs\/\d+\/file(\?download=1)?$/.test(url) ? url : null);
  const fileUrl = (job) => safeUrl(job.output_url) || (job.status === 'done' ? safeUrl('/api/media/jobs/' + job.id + '/file') : null);
  const isVideo = (job) => job.kind === 'video' || /^video\//.test(LR.str(job.output_type));

  /* ------------------------------------------------------------ data */

  async function loadStatus() {
    st.status = await LR.get('/api/media/status');
    renderSetup();
    renderForm();
  }

  async function loadJobs() {
    const data = await LR.get('/api/media/jobs');
    st.jobs = Array.isArray(data.jobs) ? data.jobs : [];
    st.loaded = true;
    renderJobs();
    renderGallery();
    return st.jobs.some((j) => ACTIVE.has(j.status));
  }

  const poller = LR.poller(async () => {
    try {
      const active = await loadJobs();
      return active ? 1200 : 6000;
    } catch (_) {
      return 5000;
    }
  }, 6000);

  function modelsOf(kind) {
    const s = st.status || {};
    const list = kind === 'video' ? s.video_models : s.image_models;
    return Array.isArray(list) ? list : [];
  }

  function ready(kind) {
    const s = st.status;
    return Boolean(s && s.runtime && s.runtime.installed && modelsOf(kind).some((m) => m.installed));
  }

  /* ------------------------------------------------------------ setup */

  function renderRuntime() {
    if (!ui.runtime) return;
    const s = st.status;
    if (!s) {
      ui.runtime.replaceChildren(LR.spinner(), h('span', { class: 'muted', text: 'Checking the image engine…' }));
      return;
    }
    const rt = s.runtime || {};
    const dl = LR.downloads.list.find((d) => d.kind === 'media-runtime' && LR.downloads.isActive(d));
    if (rt.installed) {
      ui.runtime.replaceChildren(
        LR.dot('ok'),
        h('span', { class: 'strong', text: 'Image engine ready' }),
        h('span', { class: 'muted', text: [rt.version ? 'version ' + rt.version : '', s.ffmpeg === false ? 'videos as WebM (no ffmpeg)' : s.ffmpeg ? 'MP4 videos via ffmpeg' : ''].filter(Boolean).join(' · ') }),
      );
    } else if (dl || rt.downloading) {
      ui.runtime.replaceChildren(LR.spinner(), h('span', { class: 'strong', text: 'Installing the image engine…' }), h('span', { class: 'muted', text: dl ? LR.downloads.summary(dl) : '' }));
    } else {
      ui.runtime.replaceChildren(
        LR.dot('idle'),
        h('span', { class: 'strong', text: 'Image engine not installed' }),
        LR.button({ text: 'Install', icon: 'download', size: 'sm', variant: 'secondary', onclick: (ev) => install('/api/media/runtime/install', ev.currentTarget) }),
      );
    }
  }

  function renderSetup() {
    renderRuntime();
    if (!ui.setup) return;
    const s = st.status;
    if (!s) {
      ui.setup.replaceChildren(LR.skeleton(3));
      ui.setup.hidden = false;
      return;
    }
    const kind = st.kind;
    if (ready(kind)) {
      ui.setup.hidden = true;
      ui.setup.replaceChildren();
      return;
    }
    ui.setup.hidden = false;
    const parts = [h('div', { class: 'card-head' }, [h('div', null, [
      h('h2', { class: 'card-title', text: kind === 'video' ? 'Set up video generation' : 'Set up image generation' }),
      h('p', { class: 'card-sub', text: 'A one-time download. Everything is generated on this computer.' }),
    ])])];
    if (!(s.runtime && s.runtime.installed)) {
      const active = LR.downloads.list.find((d) => d.kind === 'media-runtime' && LR.downloads.isActive(d));
      if (active) parts.push(LR.downloads.row(active, { label: 'Image engine' }).el);
      else parts.push(h('div', { class: 'setup-line' }, [
        h('div', null, [h('p', { class: 'strong', text: 'Image engine' }), h('p', { class: 'muted small', text: 'The program that turns prompts into pictures.' })]),
        LR.button({ text: 'Install', icon: 'download', variant: 'primary', size: 'sm', onclick: (ev) => install('/api/media/runtime/install', ev.currentTarget) }),
      ]));
    }
    const models = modelsOf(kind);
    if (models.length) {
      parts.push(h('div', { class: 'model-grid' }, models.map((m) => LR.modelCard(m, {
        ramTotal: s.ram_total_bytes || (LR.state.memory && LR.state.memory.total_bytes),
        onInstall: (model, btn) => install('/api/media/models/' + encodeURIComponent(model.id) + '/install', btn),
      }))));
    } else {
      parts.push(h('p', { class: 'muted', text: 'No ' + kind + ' models are available in this build.' }));
    }
    ui.setup.replaceChildren(...parts);
  }

  async function install(path, btn) {
    LR.setBusy(btn, true, 'Starting…');
    try {
      const data = await LR.post(path, {});
      if (data && data.installed) {
        LR.toast('Already installed');
        await loadStatus();
        return;
      }
      LR.toast('Download started');
      await LR.downloads.refresh();
    } catch (err) {
      LR.toast('Could not start the download: ' + err.message, 'error');
      if (btn.isConnected) LR.setBusy(btn, false);
    }
  }

  /* ------------------------------------------------------------ form */

  function buildForm() {
    ui.kind = LR.segmented({
      label: 'What to create',
      options: [{ value: 'image', label: 'Image', icon: 'image' }, { value: 'video', label: 'Video', icon: 'video' }],
      value: st.kind,
      onChange: (k) => {
        st.kind = k;
        renderSetup();
        renderForm();
      },
    });
    ui.prompt = h('textarea', { class: 'input textarea prompt-input', rows: '3', placeholder: 'A cozy reading nook by a rainy window, warm light, watercolor', maxlength: '2000' });
    ui.fPrompt = LR.field({ label: 'Prompt', control: ui.prompt });
    ui.presets = h('div', { class: 'chips', role: 'radiogroup', 'aria-label': 'Size' });
    ui.negative = h('input', { class: 'input', type: 'text', placeholder: 'blurry, text, watermark', maxlength: '1000' });
    ui.steps = h('input', { class: 'input input-number', type: 'number', min: '1', max: '100', step: '1', value: '8', inputmode: 'numeric' });
    ui.seed = h('input', { class: 'input input-number', type: 'number', min: '0', step: '1', placeholder: 'Random', inputmode: 'numeric' });
    ui.frames = h('input', { class: 'input input-number', type: 'number', min: '5', max: '161', step: '4', value: '33', inputmode: 'numeric' });
    ui.model = h('select', { class: 'input select' });
    const dice = LR.button({ icon: 'dice', variant: 'ghost', cls: 'btn-icon', label: 'Random seed', onclick: () => { ui.seed.value = String(Math.floor(Math.random() * 2147483647)); } });
    ui.fFrames = LR.field({ label: 'Frames', control: ui.frames, hint: 'Use 17, 33, 49 or 81 (4n+1). 33 frames ≈ 2 seconds.' });
    const seedField = LR.field({ label: 'Seed', control: ui.seed, optional: true, hint: 'Same seed + prompt = same picture.' });
    seedField.el.querySelector('input').after(dice);
    seedField.el.classList.add('field-seed');
    ui.fModel = LR.field({ label: 'Model', control: ui.model });
    const more = h('details', { class: 'more-options' }, [
      h('summary', null, [icon('chevron-right', 'summary-chevron'), h('span', { text: 'More options' })]),
      h('div', { class: 'options-grid' }, [
        LR.field({ label: 'Avoid', control: ui.negative, optional: true, hint: 'Things you do not want in the result.', cls: 'span-2' }).el,
        LR.field({ label: 'Steps', control: ui.steps, hint: 'More steps: finer detail, slower.' }).el,
        seedField.el,
        ui.fFrames.el,
        ui.fModel.el,
      ]),
    ]);
    ui.videoHint = LR.callout({ tone: 'warn', icon: 'clock', title: 'Video is very slow on low-RAM machines', text: 'A two-second clip can take ten minutes or more. You can keep using LowRAM AI meanwhile.' });
    ui.formError = h('div', { class: 'form-error-slot' });
    ui.generate = LR.button({ text: 'Generate', icon: 'sparkle', variant: 'primary', type: 'submit', cls: 'btn-lg' });
    ui.form = h('form', { class: 'card media-form', novalidate: true }, [
      h('div', { class: 'media-form-head' }, [h('h2', { class: 'card-title', text: 'Create' }), ui.kind.el]),
      ui.fPrompt.el,
      h('div', { class: 'field' }, [h('p', { class: 'label', id: 'size-label', text: 'Size' }), ui.presets]),
      more,
      ui.videoHint,
      ui.formError,
      h('div', { class: 'form-actions' }, [ui.generate, h('p', { class: 'muted small', text: 'Generation pauses the chat model if memory is tight.' })]),
    ]);
    ui.form.addEventListener('submit', (ev) => { ev.preventDefault(); generate(); });
    ui.prompt.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && (ev.metaKey || ev.ctrlKey)) { ev.preventDefault(); generate(); }
    });
    return ui.form;
  }

  function renderForm() {
    if (!ui.form) return;
    const kind = st.kind;
    ui.kind.set(kind);
    ui.videoHint.hidden = kind !== 'video';
    ui.fFrames.el.hidden = kind !== 'video';
    ui.prompt.placeholder = kind === 'video' ? 'Waves rolling onto a quiet beach at sunset, slow camera pan' : 'A cozy reading nook by a rainy window, warm light, watercolor';
    ui.presets.replaceChildren(...PRESETS[kind].map((p) => {
      const on = st.preset[kind] === p.id;
      return h('button', {
        type: 'button', role: 'radio', class: 'chip', 'aria-checked': String(on), tabindex: on ? '0' : '-1',
        onclick: () => { st.preset[kind] = p.id; renderForm(); },
        onkeydown: (ev) => {
          const keys = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
          if (!(ev.key in keys)) return;
          ev.preventDefault();
          const list = PRESETS[kind];
          const idx = list.findIndex((x) => x.id === st.preset[kind]);
          st.preset[kind] = list[(idx + keys[ev.key] + list.length) % list.length].id;
          renderForm();
          ui.presets.querySelector('[aria-checked="true"]').focus();
        },
      }, [h('span', { class: 'chip-shape shape-' + p.id, 'aria-hidden': 'true' }), h('span', { text: p.label }), h('span', { class: 'chip-dim', text: p.w + '×' + p.h })]);
    }));
    const models = modelsOf(kind).filter((m) => m.installed);
    const current = ui.model.value;
    const preferred = LR.state.settings && LR.state.settings.media ? LR.state.settings.media[kind + '_model_id'] : null;
    ui.model.replaceChildren(...(models.length ? models : [{ id: '', name: 'No model installed' }]).map((m) => h('option', { value: m.id, text: m.name || m.id })));
    ui.model.value = models.some((m) => m.id === current) ? current : models.some((m) => m.id === preferred) ? preferred : (models[0] ? models[0].id : '');
    const selected = models.find((m) => m.id === ui.model.value);
    if (selected && ui.lastModel !== selected.id) {
      ui.lastModel = selected.id;
      const d = selected.defaults || {};
      if (LR.isNum(d.steps)) ui.steps.value = String(d.steps);
      if (kind === 'video' && LR.isNum(d.frames)) ui.frames.value = String(d.frames);
    }
    const noFfmpeg = kind === 'video' && st.status && st.status.ffmpeg === false;
    ui.videoHint.querySelector('.callout-text').textContent = 'A two-second clip can take an hour on a CPU-only machine. You can keep using LowRAM AI meanwhile.' +
      (noFfmpeg ? ' ffmpeg was not found, so videos are saved as WebM/AVI instead of MP4.' : '');
    ui.generate.disabled = !ready(kind);
    ui.generate.title = ready(kind) ? '' : 'Install the engine and a model first';
  }

  async function generate() {
    ui.fPrompt.setError('');
    ui.formError.replaceChildren();
    const kind = st.kind;
    const prompt = ui.prompt.value.trim();
    if (!prompt) {
      ui.fPrompt.setError('Describe what you would like to see.');
      ui.prompt.focus();
      return;
    }
    if (!ready(kind)) return;
    const preset = PRESETS[kind].find((p) => p.id === st.preset[kind]) || PRESETS[kind][0];
    const body = { kind, prompt, width: preset.w, height: preset.h };
    const neg = ui.negative.value.trim();
    if (neg) body.negative_prompt = neg;
    const steps = Number(ui.steps.value);
    if (Number.isInteger(steps) && steps > 0) body.steps = steps;
    if (ui.seed.value.trim() !== '') {
      const seed = Number(ui.seed.value);
      if (Number.isInteger(seed) && seed >= 0) body.seed = seed;
    }
    if (kind === 'video') {
      const frames = Number(ui.frames.value);
      if (!Number.isInteger(frames) || frames < 5 || (frames - 1) % 4) {
        ui.fFrames.setError('Use a frame count like 17, 33, 49 or 81 (a multiple of 4, plus 1).');
        ui.frames.focus();
        return;
      }
      ui.fFrames.setError('');
      body.frames = frames;
    }
    if (ui.model.value) body.model_id = ui.model.value;
    LR.setBusy(ui.generate, true, 'Queuing…');
    try {
      const data = await LR.post('/api/media/jobs', body);
      if (data.job) st.jobs = [data.job, ...st.jobs.filter((j) => j.id !== data.job.id)];
      renderJobs();
      LR.toast(kind === 'video' ? 'Video queued' : 'Image queued');
      LR.announce('Your ' + kind + ' was added to the queue.');
      poller.now();
      LR.refreshStatus();
    } catch (err) {
      ui.formError.replaceChildren(LR.callout({ tone: err.status === 409 ? 'warn' : 'error', role: 'alert', title: err.status === 409 ? 'Busy right now' : 'Could not start', text: err.message }));
    } finally {
      LR.setBusy(ui.generate, false);
      renderForm();
    }
  }

  /* ------------------------------------------------------------ queue */

  function renderJobs() {
    if (!ui.queue) return;
    const jobs = st.jobs.filter((j) => ACTIVE.has(j.status) || j.status === 'failed' || j.status === 'cancelled');
    ui.queueCard.hidden = !jobs.length;
    const rows = ui.jobRows || (ui.jobRows = new Map());
    const keep = new Set();
    for (const j of jobs) {
      const active = ACTIVE.has(j.status);
      const key = j.id + ':' + (active ? 'a' : j.status);
      keep.add(key);
      let r = rows.get(key);
      if (!r) {
        r = {
          bar: LR.progress({ size: 'sm', label: 'Progress' }),
          badge: h('span', { class: 'badge' }),
          msg: h('p', { class: 'job-msg' }),
        };
        const actions = [];
        if (active) actions.push(LR.button({ text: 'Cancel', icon: 'x', size: 'sm', variant: 'ghost', onclick: (ev) => cancelJob(j, ev.currentTarget) }));
        else {
          actions.push(LR.button({ text: 'Use prompt', size: 'sm', variant: 'ghost', onclick: () => reuse(j) }));
          actions.push(LR.button({ icon: 'trash', size: 'sm', variant: 'ghost', cls: 'btn-icon', label: 'Remove', onclick: (ev) => deleteJob(j, ev.currentTarget) }));
        }
        r.bar.el.hidden = !active;
        r.el = h('li', { class: 'job' + (active ? ' is-active' : '') }, [
          h('span', { class: 'job-icon', 'aria-hidden': 'true' }, icon(j.kind === 'video' ? 'video' : 'image')),
          h('div', { class: 'job-main' }, [h('div', { class: 'job-head' }, [h('p', { class: 'job-prompt', text: LR.str(j.prompt) }), r.badge]), r.bar.el, r.msg]),
          h('div', { class: 'job-actions' }, actions),
        ]);
        rows.set(key, r);
      }
      const tone = j.status === 'failed' ? 'error' : j.status === 'running' ? 'accent' : 'neutral';
      r.badge.className = 'badge badge-' + tone;
      r.badge.textContent = { queued: 'Waiting', running: LR.isNum(j.progress) ? Math.round(j.progress * 100) + '%' : 'Working', failed: 'Failed', cancelled: 'Cancelled' }[j.status] || j.status;
      r.bar.set(j.status === 'running' && LR.isNum(j.progress) ? j.progress : null);
      r.msg.className = 'job-msg' + (j.status === 'failed' ? ' is-error' : '');
      r.msg.textContent = j.status === 'failed' ? LR.str(j.error, 'Generation failed.') : LR.str(j.message) || (j.status === 'queued' ? 'Waiting for the current job to finish…' : '');
      ui.queue.append(r.el);
    }
    for (const [key, r] of rows) {
      if (!keep.has(key)) {
        r.el.remove();
        rows.delete(key);
      }
    }
  }

  function reuse(job) {
    st.kind = job.kind === 'video' ? 'video' : 'image';
    ui.prompt.value = LR.str(job.prompt);
    ui.negative.value = LR.str(job.negative_prompt);
    const p = job.params || {};
    if (LR.isNum(p.steps)) ui.steps.value = String(p.steps);
    if (LR.isNum(p.seed)) ui.seed.value = String(p.seed);
    renderSetup();
    renderForm();
    ui.prompt.focus();
    ui.form.scrollIntoView({ behavior: LR.reducedMotion() ? 'auto' : 'smooth', block: 'start' });
  }

  async function cancelJob(job, btn) {
    LR.setBusy(btn, true, 'Cancelling…');
    try {
      await LR.post('/api/media/jobs/' + encodeURIComponent(job.id) + '/cancel', {});
      LR.toast('Cancelled', 'info');
    } catch (err) {
      LR.toast('Could not cancel: ' + err.message, 'error');
    } finally {
      poller.now();
    }
  }

  async function deleteJob(job, btn, skipConfirm) {
    if (!skipConfirm && job.status === 'done') {
      const ok = await LR.confirm({ title: 'Delete this ' + (isVideo(job) ? 'video' : 'image') + '?', message: 'The file is removed from this computer.', confirmText: 'Delete', danger: true });
      if (!ok) return false;
    }
    if (btn) LR.setBusy(btn, true);
    try {
      await LR.post('/api/media/jobs/' + encodeURIComponent(job.id) + '/delete', {});
      st.jobs = st.jobs.filter((j) => j.id !== job.id);
      renderJobs();
      renderGallery();
      LR.toast('Deleted');
      return true;
    } catch (err) {
      LR.toast('Could not delete: ' + err.message, 'error');
      if (btn && btn.isConnected) LR.setBusy(btn, false);
      return false;
    }
  }

  /* ------------------------------------------------------------ gallery */

  function mediaEl(job, big) {
    const url = fileUrl(job);
    if (!url) return h('div', { class: 'media-missing', text: 'File unavailable' });
    if (isVideo(job)) {
      const v = h('video', { src: url, preload: 'metadata', playsinline: true, muted: !big, loop: !big, controls: big, 'aria-label': LR.str(job.prompt) });
      if (!big) v.muted = true;
      return v;
    }
    return h('img', { src: url, alt: LR.str(job.prompt), loading: 'lazy', decoding: 'async' });
  }

  function renderGallery() {
    if (!ui.gallery) return;
    const done = st.jobs.filter((j) => j.status === 'done');
    const sig = st.loaded + '|' + done.map((j) => j.id + (j.output_url || '')).join(',');
    if (sig === ui.gallerySig) return;
    ui.gallerySig = sig;
    if (!st.loaded) {
      ui.gallery.replaceChildren(h('div', { class: 'gallery-grid' }, [0, 1, 2].map(() => h('div', { class: 'tile skeleton' }))));
      return;
    }
    if (!done.length) {
      ui.gallery.replaceChildren(LR.empty({ icon: 'image', title: 'Nothing here yet', text: 'Finished images and videos appear here. They are stored only on this computer.' }));
      return;
    }
    ui.gallery.replaceChildren(h('ul', { class: 'gallery-grid', 'aria-label': 'Finished results' }, done.map((j) => h('li', null, h('button', {
      type: 'button',
      class: 'tile' + (isVideo(j) ? ' is-video' : ''),
      'aria-label': 'Open ' + (isVideo(j) ? 'video' : 'image') + ': ' + LR.str(j.prompt),
      onclick: (ev) => openLightbox(j, ev.currentTarget),
    }, [
      mediaEl(j, false),
      isVideo(j) ? h('span', { class: 'tile-play', 'aria-hidden': 'true' }, icon('play')) : null,
      h('span', { class: 'tile-caption', text: LR.str(j.prompt) }),
    ])))));
  }

  function openLightbox(job) {
    const p = job.params || {};
    const url = fileUrl(job);
    const type = LR.str(job.output_type);
    const ext = isVideo(job) ? (type.includes('webm') ? 'webm' : type.includes('avi') ? 'avi' : 'mp4') : 'png';
    const details = [
      LR.isNum(p.width) && LR.isNum(p.height) ? p.width + '×' + p.height : '',
      LR.isNum(p.steps) ? p.steps + ' steps' : '',
      LR.isNum(p.seed) ? 'seed ' + p.seed : '',
      LR.isNum(p.frames) ? p.frames + ' frames' : '',
      LR.isNum(job.duration_s) ? 'took ' + fmt.duration(job.duration_s) : '',
    ].filter(Boolean).join(' · ');
    const del = LR.button({ text: 'Delete', icon: 'trash', variant: 'ghost', onclick: async (ev) => { if (await deleteJob(job, ev.currentTarget)) m.close(); } });
    const use = LR.button({ text: 'Use prompt', variant: 'secondary', onclick: () => { m.close(); reuse(job); } });
    const download = url ? h('a', { class: 'btn btn-primary', href: url.replace(/\?download=1$/, '') + '?download=1', download: 'lowram-' + job.id + '.' + ext }, [icon('download'), h('span', { text: 'Download' })]) : null;
    const m = LR.modal({
      title: isVideo(job) ? 'Video' : 'Image',
      sub: details,
      size: 'xl',
      cls: 'lightbox',
      body: [h('div', { class: 'lightbox-media' }, mediaEl(job, true)), h('p', { class: 'lightbox-prompt', text: LR.str(job.prompt) })],
      actions: [del, h('span', { class: 'spacer' }), use, download].filter(Boolean),
    });
  }

  /* ------------------------------------------------------------ view */

  function create() {
    const root = h('section', { class: 'view view-page', 'aria-labelledby': 'media-title' });
    ui.page = h('div', { class: 'page' });
    root.append(ui.page);
    ui.setup = h('section', { class: 'card media-setup', 'aria-label': 'Setup' });
    ui.runtime = h('p', { class: 'runtime-line', role: 'status' });
    ui.queue = h('ul', { class: 'jobs', 'aria-live': 'polite', 'aria-label': 'Queue' });
    ui.queueCard = h('section', { class: 'card', 'aria-labelledby': 'queue-title', hidden: true }, [
      h('div', { class: 'card-head' }, h('h2', { class: 'card-title', id: 'queue-title', text: 'Queue' })),
      ui.queue,
    ]);
    ui.gallery = h('div', { class: 'gallery' });
    return root;
  }

  LR.registerView('media', {
    title: 'Images & Video',
    create,
    show() {
      const head = LR.pageHead({ title: 'Images & Video', sub: 'Create pictures and short clips on this computer – slow, but completely private.' });
      head.querySelector('h1').id = 'media-title';
      if (!LR.has('media')) {
        ui.page.replaceChildren(head, LR.unavailable('Image and video generation'));
        return;
      }
      if (!ui.form) buildForm();
      ui.page.replaceChildren(head, ui.runtime, ui.setup, ui.form, ui.queueCard, h('section', { class: 'gallery-section', 'aria-labelledby': 'gallery-title' }, [
        h('h2', { class: 'section-title', id: 'gallery-title', text: 'Gallery' }),
        ui.gallery,
      ]));
      renderSetup();
      renderForm();
      renderJobs();
      renderGallery();
      loadStatus().catch((err) => {
        ui.setup.hidden = false;
        ui.setup.replaceChildren(LR.callout({ tone: 'error', title: 'Could not load the media status', text: err.message }));
      });
      poller.start();
      let sig = '';
      st.offs.push(LR.downloads.subscribe((list) => {
        const next = list.filter((d) => LR.downloads.isActive(d) && String(d.kind).startsWith('media')).map((d) => d.id).join(',');
        if (next !== sig) {
          const finished = sig.length > next.length;
          sig = next;
          if (finished) loadStatus().catch(() => {});
          else renderSetup();
        }
      }));
    },
    hide() {
      poller.stop();
      while (st.offs.length) st.offs.pop()();
    },
  });
})();
