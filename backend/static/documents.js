/* LowRAM AI Studio – documents: insert a PDF, Word file & co. and let the model work through it
 * section by section (summarize, translate, correct, simplify, own instruction) – with
 * "Abschnitt 3 von 12", percent, time left and a step history; the result as Word, Markdown or
 * text. Document names and texts are untrusted: only textContent, results via the Markdown
 * renderer (which never creates raw HTML). */
(function () {
  'use strict';

  const LR = window.LR;
  const h = LR.h;
  const icon = LR.icon;
  const fmt = LR.fmt;
  const t = LR.t;
  const ui = {};
  const st = { formats: null, upload: null, jobs: [], open: new Set(), results: new Map(), uploading: false };
  const isActive = (job) => job.status === 'queued' || job.status === 'running';
  const STATUS = {
    queued: ['info', 'Waiting to start'],
    running: ['accent', 'In progress'],
    done: ['ok', 'Done'],
    failed: ['error', 'Failed'],
    cancelled: ['warn', 'Cancelled'],
  };
  const ACTION_LABEL = { summarize: 'Summarize', translate: 'Translate', correct: 'Correct', simplify: 'Plain language', custom: 'Own instruction' };
  const ACTION_HINT = {
    summarize: 'Every section is summarised, then everything is combined into one summary.',
    translate: 'The whole document comes back in the chosen language.',
    correct: 'Spelling, grammar and punctuation are corrected – nothing else changes.',
    simplify: 'Rewritten in plain, easy language – the content stays complete.',
    custom: 'Your own instruction is applied to every section.',
  };

  /* ------------------------------------------------------------ data */

  async function loadFormats() {
    if (st.formats) return st.formats;
    st.formats = await LR.get('/api/documents/formats');
    return st.formats;
  }

  async function refresh() {
    try {
      const data = await LR.get('/api/documents/jobs');
      st.jobs = Array.isArray(data.jobs) ? data.jobs : [];
    } catch (err) {
      if (err.status === 404) {
        ui.body.replaceChildren(LR.unavailable(t('Documents')));
        return 600000;
      }
      return null;
    }
    const wanted = st.jobs.filter((j) => st.open.has(j.id) || isActive(j));
    const details = await Promise.all(wanted.map((j) => LR.get('/api/documents/jobs/' + j.id).catch(() => null)));
    for (const d of details) {
      if (!d || !d.job) continue;
      const i = st.jobs.findIndex((j) => j.id === d.job.id);
      if (i >= 0) st.jobs[i] = d.job;
    }
    renderJobs();
    return st.jobs.some(isActive) ? 1500 : 10000;
  }
  const poll = LR.poller(refresh, 10000);

  /* ------------------------------------------------------------ upload */

  function buildUpload() {
    const input = h('input', { type: 'file', hidden: true });
    input.addEventListener('change', () => { if (input.files && input.files[0]) upload(input.files[0]); input.value = ''; });
    ui.fileInput = input;
    ui.formatsLine = h('p', { class: 'muted small', text: t('PDF, Word, PowerPoint, Excel, OpenDocument, EPUB, RTF, HTML or text') });
    ui.drop = h('div', { class: 'doc-drop', tabindex: '0', role: 'button', 'aria-label': t('Choose a document') }, [
      h('span', { class: 'doc-drop-icon', 'aria-hidden': 'true' }, icon('file')),
      h('p', { class: 'strong', text: t('Drop a document here or click to choose one') }),
      ui.formatsLine,
      input,
    ]);
    ui.drop.addEventListener('click', () => input.click());
    ui.drop.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); input.click(); } });
    let depth = 0;
    ui.drop.addEventListener('dragenter', (ev) => { ev.preventDefault(); depth += 1; ui.drop.classList.add('is-drop'); });
    ui.drop.addEventListener('dragover', (ev) => ev.preventDefault());
    ui.drop.addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth) ui.drop.classList.remove('is-drop'); });
    ui.drop.addEventListener('drop', (ev) => {
      ev.preventDefault();
      depth = 0;
      ui.drop.classList.remove('is-drop');
      const file = ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
      if (file) upload(file);
    });
    ui.uploadMsg = h('p', { class: 'small', role: 'status', hidden: true });
    ui.form = h('div', { class: 'doc-form', hidden: true });
    const link = h('input', { class: 'input', type: 'url', placeholder: 'https://…', 'aria-label': t('Link to a web page or PDF') });
    const readLink = async (ev) => {
      if (ev) ev.preventDefault();
      if (!link.value.trim() || st.uploading) return;
      st.uploading = true;
      uploadNote(t('Loading the page…'));
      try {
        const data = await LR.post('/api/documents/uploads/url', { url: link.value.trim() });
        st.upload = data.upload;
        link.value = '';
        uploadNote('');
        renderForm();
      } catch (err) {
        uploadNote(err.message, 'error');
      } finally {
        st.uploading = false;
      }
    };
    ui.linkRow = h('form', { class: 'doc-link', onsubmit: readLink }, [
      h('span', { class: 'muted small', text: t('or a web page:') }), link,
      LR.button({ type: 'submit', text: t('Load'), icon: 'download', size: 'sm', variant: 'secondary' }),
    ]);
    return h('section', { class: 'card', 'aria-labelledby': 'doc-new' }, [
      h('div', { class: 'card-head' }, [h('div', null, [
        h('h2', { class: 'card-title', id: 'doc-new', text: t('Insert a document') }),
        h('p', { class: 'card-sub', text: t('Long documents are split into sections that fit your model – you see each step, how far it is and roughly how long it still takes.') }),
      ])]),
      h('div', { class: 'card-body' }, [ui.drop, ui.linkRow, ui.uploadMsg, ui.form]),
    ]);
  }

  function uploadNote(text, tone) {
    ui.uploadMsg.textContent = text || '';
    ui.uploadMsg.hidden = !text;
    ui.uploadMsg.className = 'small' + (tone === 'error' ? ' is-error-text' : ' muted');
  }

  /** A recording: turned into text on this computer, then handled like any document. */
  async function uploadAudio(file) {
    if (st.uploading) return;
    st.uploading = true;
    ui.drop.classList.add('is-busy');
    let cancel = null;
    const stop = LR.button({ text: t('Cancel'), size: 'sm', variant: 'ghost', onclick: () => { if (cancel) cancel(); } });
    const note = (text) => { uploadNote(text); ui.uploadMsg.append(' ', stop); };
    try {
      if (!(await LR.speech.ensureReady())) return;
      note(t('Turning {name} into text on this computer …', { name: file.name }));
      const job = await LR.speech.transcribe(file, {
        onStart: (c) => { cancel = c; },
        onProgress: (j) => note(file.name + ': ' + LR.speech.progressText(j)),
      });
      const data = await LR.post('/api/speech/jobs/' + job.id + '/document', {});
      st.upload = data.upload;
      uploadNote('');
      renderForm();
    } catch (err) {
      if (err.cancelled) uploadNote('');
      else uploadNote(err.message, 'error');
    } finally {
      st.uploading = false;
      ui.drop.classList.remove('is-busy');
    }
  }

  async function upload(file) {
    if (st.uploading) return;
    if (LR.speech && LR.speech.available !== false && LR.speech.isAudio(file)) {
      uploadAudio(file);
      return;
    }
    const formats = st.formats || {};
    if (LR.isNum(formats.max_mb) && file.size > formats.max_mb * 1024 * 1024) {
      uploadNote(t('{name} is larger than {n} MB.', { name: file.name, n: formats.max_mb }), 'error');
      return;
    }
    st.uploading = true;
    ui.drop.classList.add('is-busy');
    uploadNote(t('Reading {name}…', { name: file.name }));
    try {
      const data = await LR.upload('/api/documents/uploads', file);
      st.upload = data.upload;
      uploadNote('');
      renderForm();
    } catch (err) {
      uploadNote(err.message, 'error');
    } finally {
      st.uploading = false;
      ui.drop.classList.remove('is-busy');
    }
  }

  function facts(u) {
    const parts = [t(u.kind_label)];
    const unit = u.kind === 'pptx' ? '{n} slides' : u.kind === 'xlsx' ? '{n} sheets' : u.kind === 'epub' ? '{n} chapters' : '{n} pages';
    if (LR.isNum(u.pages) && u.pages > 0) parts.push(t(unit, { n: u.pages }));
    parts.push(t('{n} characters', { n: fmt.int(u.chars) }));
    return parts.join(' · ');
  }

  function renderForm() {
    const u = st.upload;
    if (!u) { ui.form.hidden = true; ui.form.replaceChildren(); return; }
    const f = st.formats || { actions: [], languages: [] };
    const saved = LR.store.get('lowram.docAction');
    let action = f.actions.some((a) => a.id === saved) ? saved : 'summarize';
    const hint = h('p', { class: 'muted small' });
    const language = h('select', { class: 'input select' }, (f.languages || []).map((l) => h('option', { value: l, text: t(l) })));
    language.value = LR.store.get('lowram.docLanguage') || 'Deutsch';
    if (!language.value) language.value = 'Deutsch';
    const languageLabel = h('span', { text: t('Language') });
    const languageField = h('div', { class: 'field' }, [h('label', { class: 'label' }, [languageLabel, language])]);
    const instruction = h('textarea', { class: 'input textarea', rows: '3', maxlength: '2000', placeholder: t('e.g. Turn every section into a short FAQ') });
    const instructionField = LR.field({ label: t('What should happen with the document?'), control: instruction });
    const skillSelect = LR.skills && LR.skills.select ? LR.skills.select() : null;
    const picker = LR.segmented({
      label: t('What should LowRAM AI do?'),
      options: f.actions.map((a) => ({ value: a.id, label: t(ACTION_LABEL[a.id] || a.label) })),
      value: action,
      onChange: (v) => { action = v; LR.store.set('lowram.docAction', v); sync(); },
    });
    function sync() {
      hint.textContent = t(ACTION_HINT[action] || '');
      languageField.hidden = !(action === 'translate' || action === 'summarize');
      languageLabel.textContent = action === 'translate' ? t('Translate into') : t('Language of the summary');
      instructionField.el.hidden = action !== 'custom';
    }
    const start = LR.button({ text: t('Start'), icon: 'play', variant: 'primary', onclick: async (ev) => {
      const body = { upload_id: u.id, action };
      if (LR.budgetPercent) body.percent = LR.budgetPercent();
      if (action === 'translate' || action === 'summarize') { body.language = language.value; LR.store.set('lowram.docLanguage', language.value); }
      if (action === 'custom') body.instruction = instruction.value;
      if (skillSelect && skillSelect.value()) body.skill_id = skillSelect.value();
      const btn = ev.currentTarget;
      LR.setBusy(btn, true, t('Starting…'));
      try {
        const data = await LR.post('/api/documents/jobs', body);
        st.open.add(data.job.id);
        st.upload = null;
        renderForm();
        LR.toast(t('Started – you can follow every step below.'));
        poll.now();  // from now on every 1.5 s while it runs
      } catch (err) {
        LR.toast(err.message, 'error');
      } finally {
        LR.setBusy(btn, false);
      }
    } });
    const preview = h('details', { class: 'doc-preview', open: u.kind === 'audio' }, [
      h('summary', { text: t('Show the beginning of the text') }),
      h('pre', { class: 'doc-preview-text', text: LR.str(u.preview) + (u.chars > LR.str(u.preview).length ? '\n…' : '') }),
    ]);
    const warnings = (u.warnings || []).map((w) => LR.callout({ tone: 'warn', text: LR.str(w) }));
    ui.form.replaceChildren(...[
      h('div', { class: 'doc-file' }, [
        h('span', { class: 'machine-icon', 'aria-hidden': 'true' }, icon('file')),
        h('div', { class: 'doc-file-main' }, [h('p', { class: 'strong doc-name', text: LR.str(u.name) }), h('p', { class: 'muted small', text: facts(u) })]),
        LR.button({ text: t('Other file'), size: 'sm', variant: 'ghost', onclick: () => { st.upload = null; renderForm(); ui.fileInput.click(); } }),
      ]),
      ...warnings,
      preview,
      picker.el,
      hint,
      languageField,
      instructionField.el,
      skillSelect ? skillSelect.el : null,
      h('div', { class: 'doc-actions' }, [
        start,
        LR.button({ text: t('Copy text'), icon: 'copy', size: 'sm', variant: 'ghost', onclick: async () => {
          try {
            const data = await LR.get('/api/documents/uploads/' + encodeURIComponent(u.id));
            if (await LR.copy(LR.str(data.text))) LR.toast(t('Text copied'));
          } catch (err) { LR.toast(err.message, 'error'); }
        } }),
      ]),
    ].filter(Boolean));
    ui.form.hidden = false;
    sync();
  }

  /* ------------------------------------------------------------ jobs */

  function jobTitle(j) {
    let what = t(ACTION_LABEL[j.action] || j.action_label);
    if (j.action === 'translate' && j.language) what += ' → ' + t(j.language);
    if (j.skill_name) what += ' · ' + t('Skill: {name}', { name: j.skill_name });
    return what;
  }

  function progressLine(j) {
    const p = j.progress || {};
    const parts = [];
    if (j.status === 'running' && p.phase === 'combine') parts.push(t('Last step: combining the summaries'));
    else if (j.status === 'running' && LR.isNum(p.current)) {
      parts.push(t('Section {n} of {total}', { n: p.current, total: j.chunks_total }));
      const left = (j.chunks_total || 0) - (j.chunks_done || 0);  // the running one included
      if (left > 0) parts.push(LR.leftText(left, 'section'));
    }
    else if (j.status !== 'done') parts.push(t('{n} of {total} sections done', { n: j.chunks_done, total: j.chunks_total }));
    if (j.status === 'running') {
      if (LR.isNum(p.percent)) parts.push(p.percent + ' %');
      if (p.overdue) parts.push(t('takes longer than usual'));
      else if (LR.isNum(p.eta_s) && p.eta_s > 0) parts.push(t('in total {eta}', { eta: fmt.eta(p.eta_s) }));
      else if (!p.basis) parts.push(t('time left: estimated after the first section'));
      if (p.basis === 'history') parts.push(t('estimate from earlier documents'));
    }
    return parts.join(' · ');
  }

  function eventText(e) {
    switch (e.kind) {
      case 'queued': return t('Queued') + (e.detail ? ' – ' + LR.str(e.detail) : '');
      case 'start': return t('Started');
      case 'section_done': return t('Section {n} of {total} finished after {t}', { n: e.part, total: e.parts, t: fmt.duration(e.duration_s || 0) });
      case 'combine': return t('Combining the summaries');
      case 'wait': return t('Waiting: {reason}', { reason: LR.str(e.detail) });
      case 'done': return t('Finished');
      case 'failed': return t('Failed') + (e.detail ? ': ' + LR.str(e.detail) : '');
      case 'cancelled': return t('Cancelled');
      case 'paused': return t('Paused – continues after the next start');
      default: return LR.str(e.kind);
    }
  }

  function history(j) {
    const events = Array.isArray(j.events) ? j.events : [];
    if (!events.length) return null;
    const box = h('details', { class: 'loop-history', open: st.open.has('h' + j.id) ? true : null }, [
      h('summary', { text: t('Step history ({n})', { n: events.length }) }),
      h('ol', { class: 'loop-timeline' }, events.slice().reverse().map((e) => h('li', null, [
        h('time', { class: 'mono', text: fmt.time(e.at) }), h('span', { text: eventText(e) }),
      ]))),
    ]);
    box.addEventListener('toggle', () => { if (box.open) st.open.add('h' + j.id); else st.open.delete('h' + j.id); });
    return box;
  }

  async function act(j, what, btn) {
    if (what === 'delete') {
      const ok = await LR.confirm({ title: t('Delete this job?'), message: t('The result and all sections are removed from this computer.'), confirmText: t('Delete'), danger: true });
      if (!ok) return;
    }
    LR.setBusy(btn, true);
    try {
      await LR.post('/api/documents/jobs/' + j.id + '/' + what, {});
      if (what === 'delete') { st.open.delete(j.id); st.results.delete(j.id); }
      if (what === 'resume') st.open.add(j.id);
      poll.now();
    } catch (err) {
      LR.toast(err.message, 'error');
    } finally {
      LR.setBusy(btn, false);
    }
  }

  async function toggleResult(j) {
    if (st.results.has(j.id)) { st.results.delete(j.id); renderJobs(); return; }
    try {
      st.results.set(j.id, await LR.get('/api/documents/jobs/' + j.id + '/result'));
      renderJobs();
    } catch (err) { LR.toast(err.message, 'error'); }
  }

  function jobCard(j) {
    const [tone, label] = STATUS[j.status] || ['info', j.status];
    const bar = LR.progress({ label: t('Progress of {name}', { name: j.name }) });
    const pct = j.progress && LR.isNum(j.progress.percent) ? j.progress.percent : null;
    bar.set(pct === null ? null : Math.min(1, pct / 100), pct === null ? '' : pct + ' %');
    bar.el.classList.toggle('is-error', j.status === 'failed');
    const actions = [];
    if (isActive(j)) actions.push(LR.button({ text: t('Cancel'), icon: 'stop', size: 'sm', variant: 'secondary', onclick: (ev) => act(j, 'cancel', ev.currentTarget) }));
    if (j.status === 'failed' || j.status === 'cancelled') actions.push(LR.button({ text: t('Continue'), icon: 'retry', size: 'sm', variant: 'secondary', onclick: (ev) => act(j, 'resume', ev.currentTarget) }));
    if (j.result_available) {
      actions.push(LR.button({ text: st.results.has(j.id) ? t('Hide result') : t('Show result'), icon: 'eye', size: 'sm', variant: 'secondary', onclick: () => toggleResult(j) }));
      for (const [format, name] of [['docx', 'Word'], ['md', 'Markdown'], ['txt', t('Text')]]) {
        actions.push(h('a', { class: 'btn btn-ghost btn-sm', href: '/api/documents/jobs/' + j.id + '/download?format=' + format, download: '' }, [icon('download'), h('span', { text: name })]));
      }
    }
    if (!isActive(j)) actions.push(LR.button({ text: t('Delete'), icon: 'trash', size: 'sm', variant: 'ghost', onclick: (ev) => act(j, 'delete', ev.currentTarget) }));
    const line = progressLine(j);
    const result = st.results.get(j.id);
    return h('li', { class: 'doc-job tone-' + tone }, [
      h('div', { class: 'doc-job-head' }, [
        j.status === 'running' ? LR.spinner() : h('span', { class: 'machine-icon', 'aria-hidden': 'true' }, icon('file')),
        h('div', { class: 'doc-job-title' }, [
          h('p', { class: 'strong doc-name', text: LR.str(j.name) }),
          h('p', { class: 'muted small', text: jobTitle(j) + ' · ' + t(j.kind_label) }),
        ]),
        LR.badge(t(label), tone === 'accent' ? 'info' : tone),
      ]),
      j.status === 'done' ? null : bar.el,
      line ? h('p', { class: 'loop-eta' }, [icon('clock'), h('span', { text: line })]) : null,
      j.message ? h('p', { class: 'loop-msg is-info' }, [icon('clock'), h('span', { text: LR.str(j.message) })]) : null,
      j.error ? h('p', { class: 'small is-error-text', text: LR.str(j.error) }) : null,
      history(j),
      result ? h('div', { class: 'doc-result' }, [LR.md.render(h('div', { class: 'md' }), LR.str(result.content)),
        result.truncated ? h('p', { class: 'muted small', text: t('Only the beginning is shown – download the file for everything.') }) : null].filter(Boolean)) : null,
      actions.length ? h('div', { class: 'doc-actions' }, actions) : null,
    ].filter(Boolean));
  }

  function renderJobs() {
    if (!ui.jobs) return;
    const keep = document.activeElement && ui.jobs.contains(document.activeElement);
    if (!st.jobs.length) {
      ui.jobs.replaceChildren(LR.empty({ icon: 'file', title: t('No documents yet'), text: t('Insert a document above – its jobs and results appear here.') }));
      return;
    }
    if (keep && st.jobs.some(isActive) === false && ui.jobsSig === JSON.stringify(st.jobs)) return;
    ui.jobsSig = JSON.stringify([st.jobs, [...st.results.keys()]]);
    ui.jobs.replaceChildren(h('ul', { class: 'doc-jobs' }, st.jobs.map((j) => jobCard(j))));
  }

  /* ------------------------------------------------------------ view */

  function create() {
    const root = h('section', { class: 'view view-page', 'aria-labelledby': 'doc-title' });
    const head = LR.pageHead({ title: t('Documents'), sub: t('Insert a PDF, Word file or other document – LowRAM AI summarises, translates, corrects or rewrites it. Everything stays on this computer.') });
    head.querySelector('h1').id = 'doc-title';
    ui.jobs = h('div', { class: 'doc-job-list' });
    ui.body = h('div', { class: 'kb-body' }, [
      buildUpload(),
      h('section', { class: 'card', 'aria-labelledby': 'doc-jobs-title' }, [
        h('div', { class: 'card-head' }, [h('div', null, [h('h2', { class: 'card-title', id: 'doc-jobs-title', text: t('Your documents') })])]),
        h('div', { class: 'card-body' }, [ui.jobs]),
      ]),
    ]);
    root.append(h('div', { class: 'page' }, [head, ui.body]));
    return root;
  }

  async function show() {
    try {
      const f = await loadFormats();
      const speech = LR.speech && LR.speech.available !== false;
      ui.fileInput.accept = (f.suffixes || []).concat(speech ? LR.speech.suffixes : []).join(',');
      ui.formatsLine.textContent = t('PDF, Word, PowerPoint, Excel, OpenDocument, EPUB, RTF, HTML or text') + ' · ' + t('up to {n} MB', { n: f.max_mb })
        + (speech ? ' · ' + t('recordings (MP3, WAV, M4A …) become text on this computer') : '');
    } catch (err) {
      if (err.status === 404) { ui.body.replaceChildren(LR.unavailable(t('Documents'))); return; }
    }
    poll.start();
  }

  LR.registerView('documents', { title: t('Documents'), create, show, hide() { poll.stop(); } });
})();
