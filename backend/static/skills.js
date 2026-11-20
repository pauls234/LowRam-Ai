/* LowRAM AI Studio – skills: reusable working instructions ("so arbeitest du") for the chat and for
 * documents. Own skills are active right away; an imported .md file stays off until it was read
 * and switched on. Skill names and texts are untrusted: textContent only, the preview goes through
 * the Markdown renderer (no raw HTML). */
(function () {
  'use strict';

  const LR = window.LR;
  const h = LR.h;
  const icon = LR.icon;
  const fmt = LR.fmt;
  const t = LR.t;
  const ui = {};

  /* ------------------------------------------------------------ shared state (chat and documents use it) */

  const sk = (LR.skills = { list: [], loaded: false, available: null, max: 8000 });

  sk.load = async function () {
    try {
      const data = await LR.get('/api/skills');
      sk.list = Array.isArray(data.skills) ? data.skills : [];
      if (LR.isNum(data.max_chars)) sk.max = data.max_chars;
      sk.available = true;
    } catch (err) {
      if (err.status === 404) { sk.available = false; sk.list = []; }
    }
    sk.loaded = true;
    LR.emit('skills', sk);
    return sk.list;
  };
  sk.usable = () => sk.list.filter((s) => s.usable);

  /** A "Skill" select for forms: hidden while no skill can be used. */
  sk.select = function () {
    const id = LR.uid('skill');
    const select = h('select', { class: 'input select', id });
    const el = h('div', { class: 'field', hidden: true }, [
      h('label', { class: 'label', for: id, text: t('Skill (optional)') }),
      select,
      h('p', { class: 'help', text: t('A skill tells LowRAM AI how to work. Manage your skills under “Skills”.') }),
    ]);
    function render() {
      const list = sk.usable();
      const current = select.value;
      select.replaceChildren(h('option', { value: '', text: t('No skill') }), ...list.map((s) => h('option', { value: s.name, text: LR.str(s.title) })));
      if (list.some((s) => s.name === current)) select.value = current;
      el.hidden = !list.length;
    }
    const off = LR.on('skills', () => {
      if (el.dataset.mounted && !el.isConnected) { off(); return; }  // the form is gone: stop listening
      if (el.isConnected) el.dataset.mounted = '1';
      render();
    });
    render();
    if (!sk.loaded) sk.load();
    return { el, value: () => select.value || null };
  };

  let probed = false;
  LR.on('status', () => {
    if (probed) return;
    probed = true;
    if (LR.mayHave('skills')) sk.load();
  });

  /* ------------------------------------------------------------ editor */

  function editor(skill) {
    const title = h('input', { class: 'input', type: 'text', maxlength: '80', value: skill ? LR.str(skill.title) : '', placeholder: t('e.g. Polite e-mails') });
    const description = h('input', { class: 'input', type: 'text', maxlength: '300', value: skill ? LR.str(skill.description) : '', placeholder: t('Optional: what is it for?') });
    const instructions = h('textarea', { class: 'input textarea skill-text', rows: '8', maxlength: String(sk.max), placeholder: t('e.g. Always answer in friendly, short German. Start with the most important point. End with one clear next step.') });
    instructions.value = skill ? LR.str(skill.instructions) : '';
    const counter = h('p', { class: 'muted small' });
    const count = () => { counter.textContent = t('{n} of {max} characters', { n: fmt.int(instructions.value.length), max: fmt.int(sk.max) }); };
    instructions.addEventListener('input', count);
    count();
    const save = LR.button({ text: skill ? t('Save') : t('Create skill'), icon: 'check', variant: 'primary', onclick: async (ev) => {
      const btn = ev.currentTarget;
      LR.setBusy(btn, true);
      try {
        const body = { title: title.value, description: description.value, instructions: instructions.value };
        if (skill) await LR.post('/api/skills/' + encodeURIComponent(skill.name), body);
        else await LR.post('/api/skills', body);
        LR.toast(skill ? t('Skill saved') : t('Skill created – you can choose it in the chat now.'));
        ui.editing = null;
        await sk.load();
        render();
      } catch (err) {
        LR.toast(err.message, 'error');
        LR.setBusy(btn, false);
      }
    } });
    return h('div', { class: 'skill-editor' }, [
      LR.field({ label: t('Name'), control: title }).el,
      LR.field({ label: t('Short description'), control: description, optional: true }).el,
      LR.field({ label: t('How should LowRAM AI work?'), control: instructions, hint: t('Write it like you would explain it to an assistant. Markdown works.') }).el,
      counter,
      h('div', { class: 'doc-actions' }, [save, LR.button({ text: t('Cancel'), variant: 'ghost', onclick: () => { ui.editing = null; render(); } })]),
    ]);
  }

  /* ------------------------------------------------------------ list */

  async function importFile(file) {
    try {
      const data = await LR.upload('/api/skills/import', file);
      await sk.load();
      ui.full = data.skill;
      ui.open = data.skill.name;
      render();
      LR.toast(t('Imported – read it and switch it on when it is fine.'));
    } catch (err) {
      LR.toast(err.message, 'error');
    }
  }

  async function toggle(skill, on, btn) {
    LR.setBusy(btn, true);
    try {
      await LR.post('/api/skills/' + encodeURIComponent(skill.name) + '/enabled', { enabled: on });
      await sk.load();
      render();
    } catch (err) {
      LR.toast(err.message, 'error');
      LR.setBusy(btn, false);
    }
  }

  async function remove(skill, btn) {
    const ok = await LR.confirm({ title: t('Delete this skill?'), message: t('“{name}” is removed from this computer.', { name: skill.title }), confirmText: t('Delete'), danger: true });
    if (!ok) return;
    LR.setBusy(btn, true);
    try {
      await LR.post('/api/skills/' + encodeURIComponent(skill.name) + '/delete', {});
      await sk.load();
      render();
    } catch (err) {
      LR.toast(err.message, 'error');
      LR.setBusy(btn, false);
    }
  }

  async function open(skill) {
    if (ui.open === skill.name) { ui.open = null; render(); return; }
    try {
      ui.full = (await LR.get('/api/skills/' + encodeURIComponent(skill.name))).skill;
      ui.open = skill.name;
      render();
    } catch (err) { LR.toast(err.message, 'error'); }
  }

  function state(s) {
    if (s.usable) return ['ok', t('Active')];
    if (s.changed_since_review) return ['warn', t('Changed – please check again')];
    if (s.too_long) return ['warn', t('Too long')];
    if (s.source !== 'own' && !s.enabled) return ['warn', t('Not checked yet')];
    return [null, t('Off')];
  }

  function row(s) {
    const [tone, label] = state(s);
    const isOpen = ui.open === s.name && ui.full && ui.full.name === s.name;
    const actions = [
      LR.button({ text: isOpen ? t('Hide') : t('Read'), icon: 'eye', size: 'sm', variant: 'ghost', onclick: () => open(s) }),
      LR.button({ text: t('Edit'), icon: 'pencil', size: 'sm', variant: 'ghost', onclick: async () => {
        try { ui.editing = (await LR.get('/api/skills/' + encodeURIComponent(s.name))).skill; render(); } catch (err) { LR.toast(err.message, 'error'); }
      } }),
      s.usable
        ? LR.button({ text: t('Switch off'), size: 'sm', variant: 'secondary', onclick: (ev) => toggle(s, false, ev.currentTarget) })
        : LR.button({ text: s.source === 'own' && !s.changed_since_review ? t('Switch on') : t('Checked – switch on'), icon: 'check', size: 'sm', variant: 'primary', disabled: s.too_long, onclick: (ev) => toggle(s, true, ev.currentTarget) }),
      LR.button({ text: t('Delete'), icon: 'trash', size: 'sm', variant: 'ghost', onclick: (ev) => remove(s, ev.currentTarget) }),
    ];
    const meta = [s.description ? LR.str(s.description) : null, s.source === 'own' ? t('Own skill') : t('Imported'), t('{n} characters', { n: fmt.int(s.chars) })];
    return h('li', { class: 'doc-job' }, [
      h('div', { class: 'doc-job-head' }, [
        h('span', { class: 'machine-icon', 'aria-hidden': 'true' }, icon('sparkle')),
        h('div', { class: 'doc-job-title' }, [
          h('p', { class: 'strong doc-name', text: LR.str(s.title) }),
          h('p', { class: 'muted small', text: meta.filter(Boolean).join(' · ') }),
        ]),
        LR.badge(label, tone),
      ]),
      !s.usable && s.source !== 'own' ? LR.callout({ tone: 'warn', text: t('Read this skill before you switch it on: it decides how LowRAM AI answers. It can never run programs or change files.') }) : null,
      isOpen ? h('div', { class: 'doc-result' }, [LR.md.render(h('div', { class: 'md' }), LR.str(ui.full.instructions))]) : null,
      h('div', { class: 'doc-actions' }, actions),
    ].filter(Boolean));
  }

  function render() {
    if (!ui.body) return;
    if (sk.available === false) { ui.body.replaceChildren(LR.unavailable(t('Skills'))); return; }
    const input = h('input', { type: 'file', accept: '.md,.markdown,.txt', hidden: true });
    input.addEventListener('change', () => { if (input.files && input.files[0]) importFile(input.files[0]); input.value = ''; });
    const editing = ui.editing;
    const top = h('section', { class: 'card', 'aria-labelledby': 'skill-new' }, [
      h('div', { class: 'card-head' }, [h('div', null, [
        h('h2', { class: 'card-title', id: 'skill-new', text: editing && editing.name ? t('Edit skill') : t('New skill') }),
        h('p', { class: 'card-sub', text: t('Describe once how LowRAM AI should work – for example for e-mails, applications or summaries – and choose it in the chat or for documents whenever you need it.') }),
      ])]),
      h('div', { class: 'card-body' }, [
        editing ? editor(editing.name ? editing : null) : h('div', { class: 'doc-actions' }, [
          LR.button({ text: t('Write a new skill'), icon: 'plus', variant: 'primary', onclick: () => { ui.editing = {}; render(); } }),
          LR.button({ text: t('Import a skill file (.md)'), icon: 'file', variant: 'secondary', onclick: () => input.click() }),
          input,
        ]),
      ]),
    ]);
    const list = sk.list.length
      ? h('ul', { class: 'doc-jobs' }, sk.list.map(row))
      : LR.empty({ icon: 'sparkle', title: t('No skills yet'), text: t('Write your first skill above or import a Markdown file.') });
    ui.body.replaceChildren(top, h('section', { class: 'card', 'aria-labelledby': 'skill-list' }, [
      h('div', { class: 'card-head' }, [h('div', null, [h('h2', { class: 'card-title', id: 'skill-list', text: t('Your skills') })])]),
      h('div', { class: 'card-body' }, [list]),
    ]));
  }

  function create() {
    const root = h('section', { class: 'view view-page', 'aria-labelledby': 'skills-title' });
    const head = LR.pageHead({ title: t('Skills'), sub: t('Reusable working instructions for the chat and for documents. They only change how LowRAM AI answers – nothing is ever run.') });
    head.querySelector('h1').id = 'skills-title';
    ui.body = h('div', { class: 'kb-body' });
    root.append(h('div', { class: 'page' }, [head, ui.body]));
    return root;
  }

  async function show() {
    if (!sk.loaded) ui.body.replaceChildren(h('div', { class: 'card' }, LR.skeleton(4)));
    await sk.load();
    render();
  }

  LR.registerView('skills', { title: t('Skills'), create, show, hide() {} });
})();
