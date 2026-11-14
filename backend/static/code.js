/* LowRAM AI Studio – Code: coding projects. The local model proposes changes
 * to a folder; nothing is written to disk until the user approves. */
(function () {
  'use strict';

  const LR = window.LR;
  const h = LR.h;
  const icon = LR.icon;
  const fmt = LR.fmt;
  const STATUS_TONE = { pending: 'neutral', running: 'accent', done: 'ok', failed: 'error', proposed: 'warn', approved: 'ok', rejected: 'neutral' };
  const STATUS_TEXT = { pending: LR.t('Pending'), running: LR.t('Working'), done: LR.t('Done'), failed: LR.t('Failed'), proposed: LR.t('Needs review'), approved: LR.t('Applied'), rejected: LR.t('Rejected') };
  const MAX_DIFF_LINES = 3000;
  const ui = {};
  const st = { projects: [], loaded: false, selected: null, detail: null, busy: null, review: null };

  /* ------------------------------------------------------------ diff viewer */

  function parseDiff(text) {
    const files = [];
    let file = null;
    let oldNo = 0;
    let newNo = 0;
    const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (line.startsWith('diff --git ') || (line.startsWith('--- ') && (lines[i + 1] || '').startsWith('+++ ') && (!file || file.rows.length))) {
        file = { path: '', oldPath: '', newPath: '', rows: [], add: 0, del: 0 };
        files.push(file);
        if (line.startsWith('diff --git ')) {
          const m = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
          if (m) file.path = m[2];
          continue;
        }
      }
      if (!file) {
        file = { path: '', oldPath: '', newPath: '', rows: [], add: 0, del: 0 };
        files.push(file);
      }
      if (line.startsWith('--- ') && !file.rows.length) {
        file.oldPath = line.slice(4).replace(/^a\//, '').replace(/\t.*$/, '');
        continue;
      }
      if (line.startsWith('+++ ') && !file.rows.length) {
        file.newPath = line.slice(4).replace(/^b\//, '').replace(/\t.*$/, '');
        file.path = file.newPath !== '/dev/null' ? file.newPath : file.oldPath;
        continue;
      }
      const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/.exec(line);
      if (hunk) {
        oldNo = Number(hunk[1]);
        newNo = Number(hunk[2]);
        file.rows.push({ type: 'hunk', text: line });
      } else if (line.startsWith('+')) {
        file.rows.push({ type: 'add', text: line.slice(1), n: newNo++ });
        file.add++;
      } else if (line.startsWith('-')) {
        file.rows.push({ type: 'del', text: line.slice(1), o: oldNo++ });
        file.del++;
      } else if (line.startsWith('\\')) {
        file.rows.push({ type: 'meta', text: line });
      } else if (file.rows.length && (line.startsWith(' ') || line === '')) {
        if (line === '' && i === lines.length - 1) continue;
        file.rows.push({ type: 'ctx', text: line.slice(1), o: oldNo++, n: newNo++ });
      } else if (line.trim()) {
        file.rows.push({ type: 'meta', text: line });
      }
    }
    return files.filter((f) => f.path || f.rows.length);
  }

  function diffView(diffText, attention, actions) {
    const files = parseDiff(diffText);
    const wrap = h('div', { class: 'diff' });
    if (!files.length) {
      wrap.append(h('p', { class: 'muted', text: LR.t('The proposal contains no visible changes.') }));
      return wrap;
    }
    let budget = MAX_DIFF_LINES;
    for (const f of files) {
      const flagged = attention.has(f.path);
      const created = f.oldPath === '/dev/null' || (actions && actions.get(f.path) === 'create');
      const bodyId = LR.uid('diff');
      const toggle = h('button', { type: 'button', class: 'diff-head', 'aria-expanded': 'true', 'aria-controls': bodyId }, [
        icon('chevron-down', 'diff-chevron'),
        icon('file'),
        h('span', { class: 'diff-path', text: f.path || LR.t('(unnamed file)') }),
        created ? LR.badge(LR.t('New file'), 'info') : null,
        flagged ? h('span', { class: 'badge badge-attention' }, [icon('alert'), h('span', { text: LR.t('ATTENTION') })]) : null,
        h('span', { class: 'diff-stats' }, [h('span', { class: 'diff-add-n', text: '+' + f.add }), h('span', { class: 'diff-del-n', text: '−' + f.del })]),
      ]);
      const table = h('table', { class: 'diff-table' });
      const caption = h('caption', { class: 'visually-hidden', text: LR.t('Changes in ') + f.path });
      const tbody = h('tbody');
      for (const r of f.rows) {
        if (budget-- <= 0) break;
        const sign = r.type === 'add' ? '+' : r.type === 'del' ? '−' : '';
        tbody.append(h('tr', { class: 'd-' + r.type }, r.type === 'hunk' || r.type === 'meta'
          ? [h('td', { class: 'd-hunk', colspan: '4', text: r.text })]
          : [
            h('td', { class: 'd-ln', text: r.o !== undefined ? String(r.o) : '' }),
            h('td', { class: 'd-ln', text: r.n !== undefined ? String(r.n) : '' }),
            h('td', { class: 'd-sign', 'aria-label': r.type === 'add' ? 'added' : r.type === 'del' ? 'removed' : null, text: sign }),
            h('td', { class: 'd-code', text: r.text }),
          ]));
      }
      table.append(caption, tbody);
      const body = h('div', { class: 'diff-body', id: bodyId, tabindex: '0', role: 'region', 'aria-label': LR.t('Diff of ') + f.path }, table);
      toggle.addEventListener('click', () => {
        const open = toggle.getAttribute('aria-expanded') !== 'true';
        toggle.setAttribute('aria-expanded', String(open));
        body.hidden = !open;
      });
      wrap.append(h('div', { class: 'diff-file' + (flagged ? ' is-flagged' : '') }, [toggle, body]));
      if (budget <= 0) {
        wrap.append(h('p', { class: 'muted small', text: LR.t('The diff is very long; only the first ') + MAX_DIFF_LINES + LR.t(' lines are shown here.') }));
        break;
      }
    }
    return wrap;
  }
  LR.diffView = diffView;

  /* ------------------------------------------------------------ data */

  async function loadProjects() {
    const data = await LR.get('/api/coding/projects');
    st.projects = Array.isArray(data.projects) ? data.projects : [];
    st.loaded = true;
    renderList();
  }

  async function loadDetail() {
    if (st.selected === null) return;
    const id = st.selected;
    try {
      const data = await LR.get('/api/coding/projects/' + encodeURIComponent(id));
      if (id !== st.selected) return;
      st.detail = { project: data.project, tasks: Array.isArray(data.tasks) ? data.tasks : [] };
      renderDetail();
      // Reopening a project with an open proposal shows it again right away.
      const open = st.detail.tasks.find((t) => t.status === 'proposed');
      if (open && !ui.reviewSlot.childElementCount && st.autoReview !== id + ':' + open.id) {
        st.autoReview = id + ':' + open.id;
        review(open);
      }
    } catch (err) {
      if (err.status === 404) {
        st.selected = null;
        st.detail = null;
        LR.go('#/code');
      }
    }
  }

  function stepInfo(task) {
    try {
      const data = JSON.parse(task.detail || '{}');
      return { files: Array.isArray(data.files) ? data.files.filter((f) => typeof f === 'string') : [], detail: typeof data.detail === 'string' ? data.detail : '' };
    } catch (_) {
      return { files: [], detail: LR.str(task.detail) };
    }
  }

  /* ------------------------------------------------------------ list */

  function renderList() {
    if (!ui.list) return;
    if (!st.loaded) {
      ui.list.replaceChildren(LR.skeleton(4));
      return;
    }
    if (!st.projects.length) {
      ui.list.replaceChildren(LR.empty({
        icon: 'folder',
        title: LR.t('No coding projects yet'),
        text: LR.t('Point LowRAM AI at a folder and describe a goal. It plans small steps and shows every change before anything is written.'),
        actions: [LR.button({ text: LR.t('New project'), icon: 'plus', variant: 'primary', onclick: newProject })],
      }));
      return;
    }
    ui.list.replaceChildren(h('ul', { class: 'proj-list', 'aria-label': LR.t('Coding projects') }, st.projects.map((p) => {
      const counts = p.counts || {};
      const total = LR.isNum(p.total) ? p.total : 0;
      const finished = (counts.approved || 0) + (counts.done || 0);
      const bar = LR.progress({ size: 'xs', value: total ? finished / total : 0 });
      bar.el.setAttribute('aria-hidden', 'true');
      return h('li', null, h('a', {
        class: 'proj-item',
        href: '#/code/' + p.id,
        'aria-current': p.id === st.selected ? 'page' : null,
      }, [
        h('span', { class: 'proj-name', text: p.goal || p.name || LR.t('Untitled project') }),
        h('span', { class: 'proj-path' }, [icon('folder'), h('span', { text: LR.str(p.repo_path) })]),
        bar.el,
        h('span', { class: 'proj-meta', text: total ? finished + LR.t(' of ') + fmt.plural(total, 'step') + LR.t(' applied') + (counts.proposed ? ' · ' + counts.proposed + ' to review' : '') : LR.t('No steps yet') }),
      ]));
    })));
  }

  /* ------------------------------------------------------------ detail */

  function renderDetail() {
    if (!ui.detail) return;
    if (st.selected === null) {
      ui.detail.replaceChildren(LR.empty({
        icon: 'code',
        title: st.projects.length ? LR.t('Choose a project') : LR.t('How it works'),
        text: LR.t('1. Pick a folder and describe the goal.  2. LowRAM AI plans small steps.  3. For each step you review a diff, then approve or reject it.'),
      }));
      return;
    }
    if (!st.detail) {
      ui.detail.replaceChildren(LR.skeleton(6));
      return;
    }
    const { project, tasks } = st.detail;
    const counts = project.counts || {};
    const proposed = tasks.find((t) => t.status === 'proposed');
    const pending = tasks.some((t) => t.status === 'pending');
    const busy = Boolean(st.busy);

    const head = h('div', { class: 'detail-head' }, [
      h('div', { class: 'detail-heading' }, [
        h('h2', { class: 'detail-title', text: project.goal || project.name || LR.t('Coding project') }),
        h('p', { class: 'detail-path' }, [icon('folder'), h('code', { text: LR.str(project.repo_path) })]),
      ]),
      h('ul', { class: 'count-chips', 'aria-label': LR.t('Step counts') }, Object.keys(STATUS_TEXT).filter((s) => counts[s]).map((s) => h('li', { class: 'badge badge-' + STATUS_TONE[s], text: counts[s] + ' ' + STATUS_TEXT[s].toLowerCase() }))),
    ]);

    const safety = LR.callout({ tone: 'info', icon: 'shield', title: LR.t('Nothing changes on disk until you approve.'), text: LR.t('Each step produces a proposal you can read line by line. Approve writes the files (with backups); reject discards it.') });

    const actions = [];
    actions.push(LR.button({ text: tasks.length ? LR.t('Plan again') : LR.t('Plan steps'), icon: 'list', variant: tasks.length ? 'secondary' : 'primary', disabled: busy, onclick: (ev) => plan(ev.currentTarget) }));
    actions.push(LR.button({ text: LR.t('Add step'), icon: 'plus', variant: 'secondary', disabled: busy, onclick: addStep }));
    actions.push(LR.button({
      text: LR.t('Run next step'),
      icon: 'zap',
      variant: tasks.length ? 'primary' : 'secondary',
      disabled: busy || !pending || Boolean(proposed),
      title: proposed ? LR.t('Review the open proposal first') : !pending ? LR.t('No pending steps') : null,
      onclick: (ev) => runStep(ev.currentTarget),
    }));
    const controls = h('div', { class: 'row-actions' }, actions);

    const busyEl = h('div', { class: 'run-busy', hidden: !busy });
    if (busy) {
      const elapsed = h('span', { class: 'run-elapsed' });
      busyEl.append(LR.spinner(), h('span', { text: st.busy.text }), elapsed);
      if (ui.stopTimer) ui.stopTimer();
      ui.stopTimer = LR.elapsed(elapsed, st.busy.started);
    }

    const stepsList = tasks.length
      ? h('ol', { class: 'step-list' }, tasks.map((t) => {
        const info = stepInfo(t);
        const rowActions = [];
        if (t.status === 'proposed') rowActions.push(LR.button({ text: LR.t('Review changes'), icon: 'eye', size: 'sm', variant: 'primary', onclick: () => review(t) }));
        return h('li', { class: 'step-item is-' + t.status }, [
          h('span', { class: 'step-num', text: String(t.position || '') }),
          h('div', { class: 'step-main' }, [
            h('p', { class: 'step-name', text: t.title || LR.t('Untitled step') }),
            info.files.length ? h('p', { class: 'step-files' }, info.files.map((f) => h('code', { text: f }))) : null,
            t.error ? h('p', { class: 'step-error', text: LR.str(t.error) }) : null,
          ]),
          h('span', { class: 'badge badge-' + (STATUS_TONE[t.status] || 'neutral'), text: STATUS_TEXT[t.status] || t.status }),
          rowActions.length ? h('div', { class: 'step-actions' }, rowActions) : null,
        ]);
      }))
      : LR.empty({ icon: 'list', title: LR.t('No steps yet'), text: LR.t('Let the model plan small steps for this goal, or add one yourself.') });

    ui.detail.replaceChildren(head, safety, controls, busyEl, ui.outcome, ui.reviewSlot, h('h3', { class: 'section-label', text: LR.t('Steps') }), stepsList);
  }

  function showOutcome(node) {
    ui.outcome.replaceChildren(node || '');
  }

  /* ------------------------------------------------------------ actions */

  async function withBusy(text, fn) {
    st.busy = { text, started: Date.now() };
    renderDetail();
    try {
      return await fn();
    } finally {
      st.busy = null;
      if (ui.stopTimer) { ui.stopTimer(); ui.stopTimer = null; }
      await loadDetail();
      loadProjects().catch(() => {});
      LR.refreshStatus();
    }
  }

  async function plan() {
    const pid = st.selected;
    showOutcome(null);
    try {
      const data = await withBusy(LR.t('The model is reading the project and planning steps…'), () => LR.post('/api/coding/projects/' + pid + '/plan', { percent: LR.budgetPercent() }));
      const n = Array.isArray(data.tasks) ? data.tasks.length : 0;
      showOutcome(LR.callout({ tone: 'ok', title: LR.t('Plan ready'), text: fmt.plural(n, 'step') + LR.t(' planned. Run them one at a time and review each change.') }));
    } catch (err) {
      showOutcome(LR.callout({ tone: err.status === 409 ? 'warn' : 'error', title: LR.t('Could not plan'), text: err.message, role: 'alert' }));
    }
  }

  async function addStep() {
    const title = h('input', { class: 'input', type: 'text', maxlength: '200', placeholder: LR.t('e.g. Add a --verbose flag') });
    const files = h('input', { class: 'input', type: 'text', placeholder: 'src/cli.py, README.md', spellcheck: 'false' });
    const detail = h('textarea', { class: 'input textarea', rows: '3', placeholder: LR.t('Anything the model should know') });
    const fTitle = LR.field({ label: LR.t('What should change?'), control: title });
    const fFiles = LR.field({ label: LR.t('Files'), control: files, optional: true, hint: LR.t('Relative paths, separated by commas. The model sees these files in full.') });
    const fDetail = LR.field({ label: 'Details', control: detail, optional: true });
    const form = h('form', { class: 'form', novalidate: true }, [fTitle.el, fFiles.el, fDetail.el]);
    const submit = async (btn) => {
      if (!title.value.trim()) {
        fTitle.setError(LR.t('Describe the step.'));
        title.focus();
        return;
      }
      LR.setBusy(btn, true, LR.t('Adding…'));
      try {
        await LR.post('/api/coding/projects/' + st.selected + '/steps', {
          title: title.value.trim(),
          files: files.value.split(/[,\n]/).map((s) => s.trim()).filter(Boolean),
          detail: detail.value.trim(),
        });
        m.close();
        LR.toast(LR.t('Step added'));
        loadDetail();
      } catch (err) {
        fTitle.setError(err.message);
        LR.setBusy(btn, false);
      }
    };
    form.addEventListener('submit', (ev) => { ev.preventDefault(); submit(addBtn); });
    const addBtn = LR.button({ text: LR.t('Add step'), variant: 'primary', onclick: (ev) => submit(ev.currentTarget) });
    const m = LR.modal({ title: LR.t('Add a step'), body: form, actions: [LR.button({ text: LR.t('Cancel'), onclick: () => m.close() }), addBtn] });
    title.focus();
  }

  async function runStep() {
    const pid = st.selected;
    showOutcome(null);
    ui.reviewSlot.replaceChildren();
    let result;
    try {
      result = await withBusy(LR.t('The model is writing a proposal for the next step…'), () => LR.post('/api/coding/projects/' + pid + '/step', { percent: LR.budgetPercent() }));
    } catch (err) {
      showOutcome(LR.callout({ tone: err.status === 409 ? 'warn' : 'error', title: LR.t('Could not run the step'), text: err.message, role: 'alert' }));
      return;
    }
    if (result.status === 'proposed') {
      showProposal(pid, result.task_id, {
        step: result.step,
        summary: result.summary,
        diff: result.diff,
        attention: result.attention,
        warnings: result.warnings,
        files: result.files,
      });
    } else if (result.status === 'failed') {
      const errors = Array.isArray(result.errors) ? result.errors : [result.error || LR.t('Unknown error')];
      showOutcome(LR.callout({ tone: 'error', role: 'alert', title: LR.t('The proposal was rejected by the safety checks'), text: errors.join(' · '), extra: h('p', { class: 'muted small', text: LR.t('Nothing was written. You can run the step again or reword it.') }) }));
    } else if (result.status === 'paused') {
      showOutcome(LR.callout({ tone: 'warn', title: LR.t('Paused – not enough free memory'), text: LR.str(result.reason) }));
    } else if (result.status === 'empty') {
      showOutcome(LR.callout({ tone: 'info', title: LR.t('Nothing to run'), text: LR.str(result.reason, LR.t('No pending steps.')) }));
    } else {
      showOutcome(LR.callout({ tone: 'info', title: LR.t('Step finished'), text: 'Status: ' + LR.str(result.status) }));
    }
  }

  async function review(task) {
    showOutcome(null);
    ui.reviewSlot.replaceChildren(h('div', { class: 'card proposal' }, LR.skeleton(5)));
    try {
      const data = await LR.get('/api/coding/projects/' + st.selected + '/tasks/' + task.id + '/proposal');
      const p = data.proposal || {};
      const changes = Array.isArray(p.changes) ? p.changes : [];
      showProposal(st.selected, task.id, {
        step: p.step || task.title,
        summary: p.summary,
        diff: p.diff,
        attention: changes.filter((c) => c && c.attention).map((c) => c.path),
        warnings: p.warnings,
        files: changes.map((c) => ({ path: c.path, action: c.action, attention: c.attention })),
        repo: p.repo,
      });
    } catch (err) {
      ui.reviewSlot.replaceChildren(LR.callout({ tone: 'error', title: LR.t('Could not load the proposal'), text: err.message, role: 'alert' }));
    }
  }

  function showProposal(pid, taskId, p) {
    const attention = new Set(Array.isArray(p.attention) ? p.attention : []);
    const files = Array.isArray(p.files) ? p.files : [];
    const actions = new Map(files.map((f) => [f.path, f.action]));
    const warnings = Array.isArray(p.warnings) ? p.warnings.filter(Boolean) : [];
    const approveBtn = LR.button({ text: LR.t('Approve & write ') + fmt.plural(files.length || 1, 'file'), icon: 'check', variant: 'primary', onclick: (ev) => approve(pid, taskId, files, ev.currentTarget) });
    const rejectBtn = LR.button({ text: LR.t('Reject'), icon: 'x', variant: 'secondary', onclick: (ev) => reject(pid, taskId, ev.currentTarget) });
    const card = h('section', { class: 'card proposal', 'aria-labelledby': 'proposal-title' }, [
      h('div', { class: 'proposal-head' }, [
        h('div', null, [
          h('p', { class: 'eyebrow', text: LR.t('Proposal for review') }),
          h('h3', { class: 'proposal-title', id: 'proposal-title', text: LR.str(p.step, LR.t('Proposed change')) }),
        ]),
        LR.badge(LR.t('Not applied yet'), 'warn'),
      ]),
      p.summary ? h('p', { class: 'proposal-summary', text: p.summary }) : null,
      attention.size ? LR.callout({ tone: 'warn', title: LR.t('Look closely at flagged files'), text: LR.t('These files can run code or change how the project is built: ') + [...attention].join(', ') }) : null,
      warnings.length ? LR.callout({ tone: 'warn', title: LR.t('Warnings'), text: warnings.join(' ') }) : null,
      diffView(p.diff, attention, actions),
      h('div', { class: 'proposal-foot' }, [
        h('p', { class: 'muted small' }, [icon('shield'), h('span', { text: LR.t('Approving writes these files and keeps backups of the originals.') })]),
        h('div', { class: 'row-actions' }, [rejectBtn, approveBtn]),
      ]),
    ]);
    ui.reviewSlot.replaceChildren(card);
    card.scrollIntoView({ behavior: LR.reducedMotion() ? 'auto' : 'smooth', block: 'start' });
  }

  async function approve(pid, taskId, files, btn) {
    // The button itself says "Approve & write N files" – one clear confirmation is enough.
    LR.setBusy(btn, true, LR.t('Writing…'));
    try {
      const res = await LR.post('/api/coding/projects/' + pid + '/tasks/' + taskId + '/approve', {});
      ui.reviewSlot.replaceChildren();
      const written = Array.isArray(res.written) ? res.written : [];
      showOutcome(LR.callout({
        tone: 'ok',
        title: LR.t('Applied – ') + fmt.plural(written.length, 'file') + LR.t(' written'),
        text: written.join(', ') + (res.backup_dir ? LR.t(' · Backups: ') + res.backup_dir : ''),
      }));
      LR.toast(LR.t('Changes applied'));
    } catch (err) {
      LR.setBusy(btn, false);
      showOutcome(LR.callout({ tone: 'error', role: 'alert', title: LR.t('Nothing was written'), text: err.message }));
    } finally {
      loadDetail();
      loadProjects().catch(() => {});
    }
  }

  async function reject(pid, taskId, btn) {
    const reason = await LR.prompt({ title: LR.t('Reject this proposal?'), label: LR.t('Reason'), optional: true, multiline: true, placeholder: LR.t('Helps you remember why (optional)'), confirmText: LR.t('Reject'), danger: true });
    if (reason === null) return;
    LR.setBusy(btn, true, LR.t('Rejecting…'));
    try {
      await LR.post('/api/coding/projects/' + pid + '/tasks/' + taskId + '/reject', { reason });
      ui.reviewSlot.replaceChildren();
      showOutcome(LR.callout({ tone: 'info', title: LR.t('Proposal rejected'), text: LR.t('Nothing was written. Add a step or plan again to continue.') }));
    } catch (err) {
      LR.setBusy(btn, false);
      showOutcome(LR.callout({ tone: 'error', role: 'alert', title: LR.t('Could not reject'), text: err.message }));
    } finally {
      loadDetail();
      loadProjects().catch(() => {});
    }
  }

  function newProject() {
    const repo = h('input', { class: 'input mono', type: 'text', placeholder: '/Users/you/projects/my-app', spellcheck: 'false', autocomplete: 'off' });
    const goal = h('textarea', { class: 'input textarea', rows: '3', placeholder: LR.t('e.g. Add a dark mode toggle to the settings page') });
    const fRepo = LR.field({ label: LR.t('Project folder'), control: repo, hint: LR.t('The full path of the folder (a git repository works best).') });
    const fGoal = LR.field({ label: LR.t('Goal'), control: goal });
    const form = h('form', { class: 'form', novalidate: true }, [fRepo.el, fGoal.el]);
    const createBtn = LR.button({ text: LR.t('Create project'), variant: 'primary', onclick: (ev) => submit(ev.currentTarget) });
    async function submit(btn) {
      fRepo.setError('');
      fGoal.setError('');
      const path = repo.value.trim();
      if (!/^(\/|~|[A-Za-z]:[\\/])/.test(path)) {
        fRepo.setError(LR.t('Enter the full (absolute) path, starting with /.'));
        repo.focus();
        return;
      }
      if (!goal.value.trim()) {
        fGoal.setError(LR.t('Describe what you want to achieve.'));
        goal.focus();
        return;
      }
      LR.setBusy(btn, true, LR.t('Creating…'));
      try {
        const data = await LR.post('/api/coding/projects', { goal: goal.value.trim(), repo_path: path });
        m.close();
        await loadProjects();
        if (data.project) LR.go('#/code/' + data.project.id);
      } catch (err) {
        (/path|repo|folder|director/i.test(err.message) ? fRepo : fGoal).setError(err.message);
        LR.setBusy(btn, false);
      }
    }
    form.addEventListener('submit', (ev) => { ev.preventDefault(); submit(createBtn); });
    const m = LR.modal({ title: LR.t('New coding project'), sub: LR.t('LowRAM AI reads the folder to plan steps. It never writes without your approval.'), body: form, actions: [LR.button({ text: LR.t('Cancel'), onclick: () => m.close() }), createBtn] });
    repo.focus();
  }

  /* ------------------------------------------------------------ view */

  function create() {
    const root = h('section', { class: 'view view-page', 'aria-labelledby': 'code-title' });
    ui.page = h('div', { class: 'page page-wide' });
    root.append(ui.page);
    ui.list = h('div', { class: 'proj-list-wrap' });
    ui.detail = h('div', { class: 'card detail-card', tabindex: '-1' });
    ui.outcome = h('div', { class: 'outcome', 'aria-live': 'polite' });
    ui.reviewSlot = h('div', { class: 'review-slot' });
    return root;
  }

  function layout() {
    const actions = [];
    if (LR.has('chat')) actions.push(LR.button({ text: LR.t('Ask a coding question'), icon: 'chat', variant: 'ghost', onclick: () => LR.go('#/chat/new/code') }));
    if (LR.has('api_coding')) actions.push(LR.button({ text: LR.t('New project'), icon: 'plus', variant: 'primary', onclick: newProject }));
    const head = LR.pageHead({ title: 'Code', sub: LR.t('Let the local model propose changes to a project folder. Nothing on disk changes until you approve.'), actions });
    head.querySelector('h1').id = 'code-title';
    if (!LR.has('api_coding')) {
      ui.page.replaceChildren(head, LR.unavailable(LR.t('Coding projects'), LR.t('This build has no coding-project module. ') + (LR.has('chat') ? LR.t('You can still ask coding questions in a Code chat.') : '')));
      return false;
    }
    ui.page.replaceChildren(head, h('div', { class: 'split' }, [h('aside', { class: 'split-side', 'aria-label': LR.t('Projects') }, ui.list), h('div', { class: 'split-main' }, ui.detail)]));
    return true;
  }

  LR.registerView('code', {
    title: 'Code',
    create,
    show(params) {
      if (!layout()) return;
      const id = params[0] && /^\d+$/.test(params[0]) ? Number(params[0]) : null;
      if (id !== st.selected) {
        st.selected = id;
        st.detail = null;
        st.autoReview = null;
        ui.reviewSlot.replaceChildren();
        showOutcome(null);
      }
      renderList();
      renderDetail();
      loadProjects().then(() => {
        if (st.selected === null && st.projects.length && window.matchMedia('(min-width: 900px)').matches) {
          window.history.replaceState(null, '', '#/code/' + st.projects[0].id);
          st.selected = st.projects[0].id;
          renderList();
          renderDetail();
          loadDetail();
        }
      }).catch(() => {});
      loadDetail();
    },
    hide() {},
  });
})();
