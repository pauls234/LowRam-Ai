/* LowRAM AI Studio – task queue: projects with one task per line, run next,
 * bounded loop, retry, recover, results – plus "run on all devices" with an
 * optional merged final result. */
(function () {
  'use strict';

  const LR = window.LR;
  const h = LR.h;
  const icon = LR.icon;
  const fmt = LR.fmt;
  const STATUS_ORDER = ['pending', 'running', 'done', 'failed', 'proposed', 'approved', 'rejected'];
  const STATUS_TONE = { pending: 'neutral', running: 'accent', done: 'ok', failed: 'error', proposed: 'warn', approved: 'ok', rejected: 'neutral' };
  const STOP_REASONS = {
    queue_empty: ['ok', 'Queue empty: every pending task was processed.'],
    max_tasks: ['ok', 'Reached the task limit for this loop.'],
    failure: ['error', 'Stopped because a task failed.'],
    paused_low_ram: ['warn', 'Paused: not enough free RAM.'],
    stop_requested: ['info', 'Stopped on request.'],
    busy: ['warn', 'Did not start: something else was running.'],
    error: ['error', 'Stopped because of an unexpected error.'],
    no_model: ['warn', 'Stopped: no model is running.'],
    on_battery: ['warn', 'Paused: your Mac is on battery.'],
  };
  const MERGE_TEXT = { waiting: 'Waiting for all tasks before combining the results…', running: 'Combining the results on this computer…', failed: 'Combining the results failed.' };
  const ERROR_PREVIEW = 240;
  const ui = {};
  const st = {
    projects: [], loaded: false, selected: null, detail: null, loop: null,
    run: null, loopRequest: false, final: null, finalFor: null,
    distributed: new Set(String(LR.store.get('lowram.distributed') || '').split(',').filter(Boolean).map(Number)),
    loopDismissed: LR.store.get('lowram.loopDismissed'),
  };

  const isLoopActive = () => Boolean(st.loop && st.loop.active);
  const projectById = (id) => st.projects.find((p) => p.id === id) || null;
  const taskLabel = (t) => (t && LR.isNum(t.position) ? 'Task #' + t.position : 'Task');

  /* ------------------------------------------------------------ data */

  async function loadProjects() {
    const data = await LR.get('/api/projects');
    st.projects = Array.isArray(data.projects) ? data.projects : [];
    st.loaded = true;
    if (st.selected !== null && !projectById(st.selected)) {
      st.selected = null;
      st.detail = null;
    }
    renderList();
  }

  async function loadDetail() {
    const id = st.selected;
    if (id === null) return;
    try {
      const data = await LR.get('/api/projects/' + encodeURIComponent(id));
      if (id !== st.selected) return;
      const sig = JSON.stringify([data.project, data.tasks, isLoopActive() ? st.loop.current_task_id : null, st.shared ? st.shared.get(id) || null : null]);
      if (sig !== st.detailSig || !st.detail) {
        st.detailSig = sig;
        st.detail = { project: data.project, tasks: Array.isArray(data.tasks) ? data.tasks : [] };
        renderDetail();
      }
    } catch (err) {
      if (err.status === 404) {
        st.selected = null;
        st.detail = null;
        LR.go('#/tasks');
      }
    }
    // The merged result only exists for shared projects (404 until merged), so
    // only ask for projects shared via "Run on all devices".
    const shared = st.shared ? st.shared.get(id) : null;
    if (st.shared) {
      // Newer backend: it tells us when the merged result exists (no 404 polling).
      if (shared && shared.final_available && !(st.finalFor === id && st.final)) loadFinal(id);
      return;
    }
    const settled = st.detail && st.detail.tasks.length && !st.detail.tasks.some((t) => t.status === 'pending' || t.status === 'running');
    if (!settled) {
      st.settledAt = 0;
      return;
    }
    if (!st.settledAt) st.settledAt = Date.now();
    const due = Date.now() - st.settledAt >= 4000 && Date.now() - (st.finalCheckedAt || 0) >= 10000;
    if (LR.has('cluster') && st.distributed.has(id) && due && !(st.finalFor === id && st.final)) {
      st.finalCheckedAt = Date.now();
      loadFinal(id);
    }
  }

  async function loadFinal(id) {
    try {
      const data = await LR.get('/api/cluster/projects/' + encodeURIComponent(id) + '/final');
      if (id !== st.selected) return;
      const sig = LR.str(data.content).length + ':' + LR.str(data.path);
      if (st.finalFor === id && st.final && st.final.sig === sig) return;
      st.final = { content: LR.str(data.content), path: LR.str(data.path), sig };
      st.finalFor = id;
      renderFinal();
    } catch (err) {
      if (err.status === 404 && st.finalFor !== id) {
        st.final = null;
        st.finalFor = id;
        renderFinal();
      }
    }
  }

  async function loadLoop() {
    const data = await LR.get('/api/loop');
    applyLoop(data.loop || null);
  }

  async function loadShared() {
    if (!LR.has('cluster')) return;
    const data = await LR.get('/api/cluster');
    const c = data.cluster || data;
    if (!Array.isArray(c.projects)) return; // older backend: fall back to local memory
    st.shared = new Map(c.projects.map((p) => [p.project_id, p]));
    for (const p of c.projects) st.distributed.add(p.project_id);
  }

  const poller = LR.poller(async () => {
    try {
      await Promise.all([loadLoop(), loadProjects(), loadShared().catch(() => {})]);
      await loadDetail();
    } catch (_) { /* banner handles outages */ }
    const running = isLoopActive() || st.run || (st.detail && st.detail.tasks.some((t) => t.status === 'running')) || st.distributed.has(st.selected);
    return running ? 2000 : 7000;
  }, 7000);

  /* ------------------------------------------------------------ list */

  function renderList() {
    if (!ui.list) return;
    if (!st.loaded) {
      ui.list.replaceChildren(LR.skeleton(4));
      return;
    }
    if (!st.projects.length) {
      ui.list.replaceChildren(LR.empty({ icon: 'tasks', title: 'No projects yet', text: 'Create one with a task per line – LowRAM AI works through them one at a time.', actions: [LR.button({ text: 'New project', icon: 'plus', variant: 'primary', onclick: newProject })] }));
      return;
    }
    const loopPid = isLoopActive() ? st.loop.project_id : null;
    const sig = JSON.stringify([st.projects, st.selected, loopPid, st.run && st.run.projectId]);
    if (sig === ui.listSig) return;
    ui.listSig = sig;
    ui.list.replaceChildren(h('ul', { class: 'proj-list', 'aria-label': 'Task projects' }, st.projects.map((p) => {
      const counts = p.counts || {};
      const total = LR.isNum(p.total) ? p.total : STATUS_ORDER.reduce((n, s) => n + (counts[s] || 0), 0);
      const finished = (counts.done || 0) + (counts.approved || 0);
      const bar = LR.progress({ size: 'xs', value: total ? finished / total : 0 });
      bar.el.setAttribute('aria-hidden', 'true');
      const parts = [fmt.plural(total, 'task')];
      if (counts.done) parts.push(counts.done + ' done');
      if (counts.failed) parts.push(counts.failed + ' failed');
      if (counts.pending) parts.push(counts.pending + ' pending');
      return h('li', null, h('a', { class: 'proj-item', href: '#/tasks/' + p.id, 'aria-current': p.id === st.selected ? 'page' : null }, [
        h('span', { class: 'proj-name' }, [
          h('span', { text: p.name || 'Untitled' }),
          p.kind === 'coding' ? LR.badge('coding', 'info') : null,
          p.id === loopPid ? LR.badge('loop', 'accent') : null,
          st.run && st.run.projectId === p.id ? LR.badge('running', 'accent') : null,
        ]),
        bar.el,
        h('span', { class: 'proj-meta', text: parts.join(' · ') }),
      ]));
    })));
  }

  /* ------------------------------------------------------------ loop banner */

  function applyLoop(loop) {
    const was = isLoopActive();
    st.loop = loop;
    if (was && !isLoopActive() && loop) {
      const reason = STOP_REASONS[loop.stopped_because] || ['info', 'Loop finished.'];
      LR.announce('Loop finished. ' + reason[1]);
      loadDetail();
    }
    renderLoop();
  }

  function loopKey(loop) {
    return loop ? String(loop.started_at || '') + '|' + String(loop.finished_at || '') : '';
  }

  function renderLoop() {
    if (!ui.loop) return;
    const loop = st.loop;
    const active = isLoopActive();
    const finishedVisible = Boolean(loop) && !active && Boolean(loop.started_at) && Boolean(loop.finished_at) && st.loopDismissed !== loopKey(loop);
    if (!active && !finishedVisible) {
      ui.loop.hidden = true;
      return;
    }
    ui.loop.hidden = false;
    const project = projectById(loop.project_id);
    const sig = JSON.stringify([loop, project && project.name, project && project.counts, st.selected]);
    if (sig === ui.loopSig) return;
    ui.loopSig = sig;
    const completed = loop.completed || 0;
    const failed = loop.failed || 0;
    const max = LR.isNum(loop.max_tasks) && loop.max_tasks > 0 ? loop.max_tasks : null;
    // Progress against what is actually left in the project (the limit is only a cap).
    const left = project && project.counts && LR.isNum(project.counts.pending) ? project.counts.pending + (project.counts.running || 0) : null;
    const planned = left !== null ? Math.min(max || Infinity, completed + failed + left) : max;
    const bar = LR.progress({ label: 'Loop progress' });
    bar.set(planned ? Math.min(1, (completed + failed) / planned) : null, completed + ' done' + (planned ? ' of ' + planned : ''));
    const parts = [completed + ' done'];
    if (failed) parts.push(failed + ' failed');
    if (active && left !== null) parts.push(left + ' left');
    if (max) parts.push('limit ' + max);
    let tone = 'accent';
    let title = loop.stop_requested ? 'Loop stopping…' : 'Loop running';
    if (!active) {
      const reason = STOP_REASONS[loop.stopped_because] || ['info', 'Loop finished.'];
      tone = reason[0];
      title = 'Loop finished';
      parts.push(reason[1]);
    } else if (loop.stop_requested) parts.push('ends after the current task');
    const actions = [];
    if (project && project.id !== st.selected) actions.push(LR.button({ text: 'View project', size: 'sm', variant: 'ghost', onclick: () => LR.go('#/tasks/' + project.id) }));
    if (!active && loop.stopped_because === 'no_model') actions.push(...modelsButton());
    if (!active && loop.stopped_because === 'on_battery') actions.push(LR.button({ text: 'Power settings', icon: 'battery', size: 'sm', variant: 'ghost', onclick: () => LR.go('#/settings') }));
    if (active) actions.push(LR.button({ text: loop.stop_requested ? 'Stopping…' : 'Stop loop', icon: 'stop', size: 'sm', variant: 'secondary', disabled: loop.stop_requested, onclick: stopLoop }));
    else actions.push(LR.button({ text: 'Dismiss', size: 'sm', variant: 'ghost', onclick: () => { st.loopDismissed = loopKey(loop); LR.store.set('lowram.loopDismissed', st.loopDismissed); renderLoop(); } }));
    ui.loop.className = 'loop-banner tone-' + tone + (active ? ' is-active' : '');
    ui.loop.replaceChildren(
      h('div', { class: 'loop-head' }, [
        active ? LR.spinner() : icon(tone === 'error' ? 'alert' : 'check'),
        h('div', { class: 'loop-heading' }, [
          h('p', { class: 'loop-title', text: title }),
          h('p', { class: 'loop-sub', text: (project ? project.name : 'Project ' + loop.project_id) + ' · ' + parts.join(' · ') }),
        ]),
        h('div', { class: 'loop-actions' }, actions),
      ]),
      bar.el,
      loop.last_message ? h('p', { class: 'loop-msg', text: LR.str(loop.last_message) }) : null,
      !active && loop.stopped_because === 'on_battery' ? h('p', { class: 'loop-msg loop-hint' }, [LR.icon('bolt'), h('span', { text: 'Plug in your Mac and run the loop again – or lower the battery limit in Models & Settings.' })]) : null,
    );
  }

  /* ------------------------------------------------------------ detail */

  function renderDetail() {
    if (!ui.detail) return;
    const p = st.detail && st.detail.project && st.detail.project.id === st.selected ? st.detail.project : projectById(st.selected);
    if (!p) {
      ui.detail.replaceChildren(LR.empty({ icon: 'tasks', title: st.projects.length ? 'Choose a project' : 'Your task queue', text: 'Write one task per line. LowRAM AI runs them one at a time, waits for free memory, and keeps every result.' }));
      return;
    }
    const counts = p.counts || {};
    const coding = p.kind === 'coding';
    const tasks = st.detail && st.detail.project.id === p.id ? st.detail.tasks : null;
    const loopActive = isLoopActive();
    const busy = Boolean(st.run) || loopActive || st.loopRequest;
    const mem = LR.state.memory;

    const head = h('div', { class: 'detail-head' }, [
      h('div', { class: 'detail-heading' }, [
        h('h2', { class: 'detail-title', text: p.name || 'Untitled' }),
        h('p', { class: 'muted small', text: (coding ? 'Coding project' : 'Task project') + (p.created_at ? ' · created ' + fmt.dateLong(p.created_at) : '') }),
      ]),
      h('ul', { class: 'count-chips', 'aria-label': 'Task counts' }, STATUS_ORDER.filter((s) => counts[s]).map((s) => h('li', { class: 'badge badge-' + STATUS_TONE[s], text: counts[s] + ' ' + s }))),
    ]);
    const out = [head];
    if (coding) {
      out.push(LR.callout({ tone: 'info', title: 'This is a coding project', text: 'Review and approve its steps in Code.', actions: LR.has('api_coding') ? [LR.button({ text: 'Open in Code', icon: 'code', size: 'sm', onclick: () => LR.go('#/code/' + p.id) })] : [] }));
    } else {
      const maxId = LR.uid('max');
      ui.maxTasks = h('input', { class: 'input input-number', id: maxId, type: 'number', min: '1', max: '100', step: '1', inputmode: 'numeric', value: LR.store.get('lowram.maxTasks') || '5', disabled: busy });
      ui.maxErr = h('p', { class: 'field-error', hidden: true });
      const controls = h('div', { class: 'run-controls' }, [
        LR.button({ text: 'Run next task', icon: 'play', variant: 'primary', disabled: busy, onclick: runNext }),
        h('div', { class: 'loop-group' }, [
          h('label', { class: 'inline-label', for: maxId, text: 'Up to' }),
          ui.maxTasks,
          LR.button({ text: 'Run loop', icon: 'retry', variant: 'secondary', disabled: busy, onclick: startLoop }),
        ]),
      ]);
      out.push(controls, ui.maxErr);
      if (st.run) {
        const elapsed = h('span', { class: 'run-elapsed' });
        out.push(h('p', { class: 'run-busy' }, [LR.spinner(), h('span', { text: st.run.projectId === p.id ? 'Running… local models take their time.' : 'Running a task in “' + st.run.name + '”…' }), elapsed]));
        if (ui.stopRunTimer) ui.stopRunTimer();
        ui.stopRunTimer = LR.elapsed(elapsed, st.run.started);
      }
      let hint = 'Runs with a ' + LR.budgetPercent() + '% memory budget. Results are saved on this computer.';
      if (loopActive) hint = 'A loop is running – stop it above to run single tasks.';
      else if (mem && mem.can_run === false) hint = 'Not enough free RAM right now; a run would pause. Close other apps, then try again.';
      out.push(h('p', { class: 'help' + (mem && mem.can_run === false ? ' is-warn' : ''), text: hint }));
      if (LR.has('cluster')) out.push(distributeRow(p, busy));
    }
    out.push(ui.finalSlot, ui.outcome);
    if (tasks === null) out.push(LR.skeleton(4));
    else if (!tasks.length) out.push(LR.empty({ icon: 'list', title: 'This project has no tasks' }));
    else out.push(taskTable(tasks, coding));
    ui.detail.replaceChildren(...out);
    renderFinal();
  }

  function distributeRow(p, busy) {
    const mergeId = LR.uid('merge');
    const merge = h('input', { type: 'checkbox', id: mergeId, class: 'checkbox', checked: LR.store.get('lowram.merge') !== '0' });
    merge.addEventListener('change', () => LR.store.set('lowram.merge', merge.checked ? '1' : '0'));
    const shared = st.shared ? st.shared.get(p.id) : null;
    const on = shared ? true : st.distributed.has(p.id);
    const merge_state = shared ? shared.merge_state : null;
    const mergeNote = merge_state && MERGE_TEXT[merge_state]
      ? h('p', { class: 'small' + (merge_state === 'failed' ? ' is-error-text' : ' muted') }, [merge_state === 'failed' ? LR.icon('alert') : LR.spinner(), ' ', MERGE_TEXT[merge_state] + (merge_state === 'failed' && shared.merge_error ? ' ' + shared.merge_error : '')])
      : null;
    return h('div', { class: 'distribute' }, [
      h('div', { class: 'distribute-text' }, [
        h('p', { class: 'strong' }, [icon('devices'), h('span', { text: on ? 'Shared with your devices' : 'Run on all devices' })]),
        h('p', { class: 'muted small', text: on ? 'Paired computers pick up pending tasks while this one works too.' : 'Paired computers on your network help work through the pending tasks.' }),
        h('label', { class: 'check-row', for: mergeId }, [merge, h('span', { text: 'Combine all results into one final answer at the end' })]),
        mergeNote,
      ]),
      LR.button({ text: on ? 'Share again' : 'Run on all devices', icon: 'merge', variant: 'secondary', disabled: busy, onclick: (ev) => distribute(p, merge.checked, ev.currentTarget) }),
    ]);
  }

  function renderFinal() {
    if (!ui.finalSlot) return;
    const f = st.finalFor === st.selected ? st.final : null;
    if (!f) {
      ui.finalSlot.replaceChildren();
      return;
    }
    const body = h('div', { class: 'md final-body' });
    LR.md.render(body, f.content);
    ui.finalSlot.replaceChildren(h('section', { class: 'final-card', 'aria-labelledby': 'final-title' }, [
      h('div', { class: 'final-head' }, [
        h('div', null, [h('p', { class: 'eyebrow', text: 'Merged from all devices' }), h('h3', { class: 'final-title', id: 'final-title', text: 'Final result' })]),
        LR.copyButton({ getText: () => f.content }),
      ]),
      body,
      f.path ? h('p', { class: 'muted small mono', text: f.path }) : null,
    ]));
  }

  function taskTable(tasks, coding) {
    const rows = tasks.map((t) => {
      const status = STATUS_ORDER.includes(t.status) ? t.status : 'pending';
      const titleCell = h('td', { class: 'col-title' }, h('span', { class: 'task-title', text: t.title || '(untitled task)' }));
      if (t.status === 'failed' && t.error) {
        const err = LR.str(t.error);
        const p = h('p', { class: 'task-error' });
        if (err.length > ERROR_PREVIEW) {
          p.append(err.slice(0, ERROR_PREVIEW).trimEnd() + '… ', h('button', { type: 'button', class: 'link-btn', text: 'Show full error', onclick: () => textDialog(taskLabel(t) + ' error', t.title, err) }));
        } else p.textContent = err;
        titleCell.append(p);
      }
      const actions = [];
      if (t.has_result) actions.push(LR.button({ text: 'Result', icon: 'eye', size: 'sm', variant: 'secondary', label: 'View result of ' + taskLabel(t), onclick: () => openResult(t) }));
      if (t.status === 'failed' && !coding) actions.push(LR.button({ text: 'Retry', icon: 'retry', size: 'sm', variant: 'ghost', label: 'Retry ' + taskLabel(t), onclick: (ev) => retryTask(t, ev.currentTarget) }));
      const isCurrent = status === 'running' || (isLoopActive() && st.loop.current_task_id === t.id);
      return h('tr', { class: isCurrent ? 'is-running' : null }, [
        h('td', { class: 'col-pos', text: LR.isNum(t.position) ? String(t.position) : '' }),
        titleCell,
        h('td', { class: 'col-status' }, h('span', { class: 'badge badge-' + STATUS_TONE[status] }, [isCurrent ? LR.spinner('spinner-xs') : null, h('span', { text: status })])),
        h('td', { class: 'col-attempts' }, [h('span', { class: 'cell-label', text: 'Attempts ' }), String(LR.isNum(t.attempts) ? t.attempts : 0)]),
        h('td', { class: 'col-finished' }, t.finished_at ? h('time', { datetime: String(t.finished_at), title: fmt.dateLong(t.finished_at), text: fmt.time(t.finished_at) }) : h('span', { class: 'muted', text: '—' })),
        h('td', { class: 'col-actions' }, actions.length ? h('div', { class: 'row-actions' }, actions) : null),
      ]);
    });
    return h('div', { class: 'table-scroll' }, h('table', { class: 'task-table' }, [
      h('caption', { class: 'visually-hidden', text: 'Tasks in this project' }),
      h('thead', null, h('tr', null, [
        h('th', { scope: 'col', class: 'col-pos', text: '#' }),
        h('th', { scope: 'col', text: 'Task' }),
        h('th', { scope: 'col', text: 'Status' }),
        h('th', { scope: 'col', class: 'col-attempts', text: 'Tries' }),
        h('th', { scope: 'col', class: 'col-finished', text: 'Finished' }),
        h('th', { scope: 'col' }, h('span', { class: 'visually-hidden', text: 'Actions' })),
      ])),
      h('tbody', null, rows),
    ]));
  }

  function modelsButton() {
    return LR.has('engine') ? [LR.button({ text: 'Open Models', icon: 'sliders', size: 'sm', variant: 'primary', onclick: () => LR.go('#/settings') })] : [];
  }

  function showOutcome(tone, title, text, actions) {
    ui.outcome.replaceChildren(LR.callout({ tone, title, text, actions, role: tone === 'error' ? 'alert' : null }));
  }

  /* ------------------------------------------------------------ actions */

  async function runNext() {
    const p = projectById(st.selected);
    if (!p || st.run || isLoopActive()) return;
    st.run = { projectId: p.id, name: p.name || 'Untitled', started: Date.now() };
    ui.outcome.replaceChildren();
    renderDetail();
    renderList();
    poller.now();
    try {
      const data = await LR.post('/api/projects/' + p.id + '/run', { percent: LR.budgetPercent() });
      const task = data.task || null;
      const label = taskLabel(task);
      if (data.status === 'done') showOutcome('ok', label + ' done', task ? task.title : '', task ? [LR.button({ text: 'View result', size: 'sm', onclick: () => openResult(task) })] : []);
      else if (data.status === 'failed') showOutcome('error', label + ' failed', LR.str(data.error || data.reason, 'Unknown error.'), task ? [LR.button({ text: 'Retry task', size: 'sm', onclick: (ev) => retryTask(task, ev.currentTarget) })] : []);
      else if (data.status === 'paused') showOutcome('warn', 'Paused – low RAM', LR.str(data.reason, 'Not enough free memory to start a task.'));
      else if (data.status === 'empty') showOutcome('info', 'Nothing to run', LR.str(data.reason, 'This project has no pending tasks.'));
      else if (data.status === 'on_battery') showOutcome('warn', 'Paused – on battery', LR.str(data.reason, 'Plug in your Mac to run long tasks.'), [LR.button({ text: 'Power settings', icon: 'battery', size: 'sm', onclick: () => LR.go('#/settings') })]);
      else if (data.status === 'no_model') showOutcome('warn', 'No model is running', LR.str(data.reason, 'Start a model first.'), modelsButton());
      else showOutcome('info', 'Run finished', 'Status: ' + LR.str(data.status));
    } catch (err) {
      showOutcome(err.status === 409 ? 'warn' : 'error', err.status === 409 ? 'Busy' : 'Run failed', err.network ? 'LowRAM AI could not be reached. The task may still be running.' : err.message);
    } finally {
      if (ui.stopRunTimer) { ui.stopRunTimer(); ui.stopRunTimer = null; }
      st.run = null;
      await loadProjects().catch(() => {});
      await loadDetail();
      renderList();
      LR.refreshStatus();
    }
  }

  function readMax() {
    const n = Number(ui.maxTasks.value);
    if (!Number.isInteger(n) || n < 1 || n > 100) {
      ui.maxTasks.setAttribute('aria-invalid', 'true');
      ui.maxErr.textContent = 'Choose a whole number from 1 to 100.';
      ui.maxErr.hidden = false;
      ui.maxTasks.focus();
      return null;
    }
    ui.maxTasks.removeAttribute('aria-invalid');
    ui.maxErr.hidden = true;
    return n;
  }

  async function startLoop() {
    const p = projectById(st.selected);
    if (!p || st.run || isLoopActive()) return;
    const max = readMax();
    if (max === null) return;
    LR.store.set('lowram.maxTasks', max);
    st.loopRequest = true;
    ui.outcome.replaceChildren();
    renderDetail();
    try {
      const data = await LR.post('/api/projects/' + p.id + '/loop', { percent: LR.budgetPercent(), max_tasks: max });
      st.loopDismissed = null;
      LR.store.remove('lowram.loopDismissed');
      if (data.loop) applyLoop(data.loop);
      LR.announce('Loop started: up to ' + fmt.plural(max, 'task') + ', one at a time.');
    } catch (err) {
      showOutcome(err.status === 409 ? 'warn' : 'error', 'Could not start the loop', err.message);
    } finally {
      st.loopRequest = false;
      renderDetail();
      renderList();
      poller.now();
    }
  }

  async function stopLoop() {
    try {
      const data = await LR.post('/api/loop/stop', {});
      if (data.loop) applyLoop(data.loop);
    } catch (err) {
      showOutcome('error', 'Could not stop the loop', err.message);
    }
  }

  async function retryTask(task, btn) {
    if (btn) LR.setBusy(btn, true);
    try {
      const data = await LR.post('/api/tasks/' + task.id + '/retry', {});
      const t = data.task || task;
      showOutcome('info', taskLabel(t) + ' queued again', LR.str(t.title) + ' is back in the queue.');
    } catch (err) {
      showOutcome('error', 'Could not retry ' + taskLabel(task), err.message);
    } finally {
      await loadDetail();
      loadProjects().catch(() => {});
    }
  }

  async function distribute(p, merge, btn) {
    LR.setBusy(btn, true, 'Sharing…');
    try {
      await LR.post('/api/cluster/projects/' + p.id + '/distribute', { percent: LR.budgetPercent(), merge });
      st.distributed.add(p.id);
      await loadShared().catch(() => {});
      LR.store.set('lowram.distributed', [...st.distributed].slice(-50).join(','));
      if (st.finalFor === p.id) { st.final = null; st.finalFor = null; renderFinal(); }
      showOutcome('ok', 'Shared with your devices', merge ? 'Helpers pick up pending tasks now. The combined final answer appears here when everything is done.' : 'Helpers pick up pending tasks now.');
      poller.now();
    } catch (err) {
      showOutcome(err.status === 409 ? 'warn' : 'error', 'Could not share the project', err.message, [LR.button({ text: 'Open Devices', size: 'sm', onclick: () => LR.go('#/devices') })]);
    } finally {
      renderDetail();
    }
  }

  async function recover() {
    const ok = await LR.confirm({ title: 'Recover stuck tasks?', message: 'Tasks left “running” by a crash or a closed app are marked as failed so you can retry them.', confirmText: 'Recover' });
    if (!ok) return;
    try {
      const data = await LR.post('/api/recover', {});
      const ids = Array.isArray(data.recovered) ? data.recovered : [];
      LR.toast(ids.length ? 'Recovered ' + fmt.plural(ids.length, 'task') : 'No stuck tasks found', 'info');
    } catch (err) {
      LR.toast('Could not recover: ' + err.message, 'error');
    } finally {
      poller.now();
    }
  }

  /* ------------------------------------------------------------ dialogs */

  function textDialog(title, sub, text) {
    LR.modal({ title, sub, size: 'lg', body: h('pre', { class: 'result-pre', tabindex: '0', text }) });
  }

  async function openResult(task) {
    const body = h('div', { class: 'result-body md', tabindex: '0' }, LR.skeleton(6));
    const raw = h('pre', { class: 'result-pre', tabindex: '0', hidden: true });
    let content = '';
    const toggle = LR.button({ text: 'Show plain text', size: 'sm', variant: 'ghost', disabled: true, onclick: () => {
      const showRaw = raw.hidden;
      raw.hidden = !showRaw;
      body.hidden = showRaw;
      toggle.querySelector('span').textContent = showRaw ? 'Show formatted' : 'Show plain text';
    } });
    const copy = LR.copyButton({ getText: () => content, cls: 'btn btn-secondary btn-sm' });
    const m = LR.modal({ title: taskLabel(task) + (task.title ? ': ' + task.title : ''), size: 'lg', body: [body, raw], actions: [toggle, h('span', { class: 'spacer' }), copy, LR.button({ text: 'Close', variant: 'primary', size: 'sm', onclick: () => m.close() })] });
    try {
      const data = await LR.get('/api/tasks/' + task.id + '/result');
      content = LR.str(data.content);
      m.setSub((data.path ? data.path + ' · ' : '') + fmt.int(content.length) + ' characters' + (data.truncated ? ' (truncated)' : ''));
      if (content) LR.md.render(body, content);
      else body.replaceChildren(h('p', { class: 'muted', text: 'The result file is empty.' }));
      raw.textContent = content;
      toggle.disabled = false;
    } catch (err) {
      body.replaceChildren(LR.callout({ tone: 'error', title: 'Could not load the result', text: err.status === 404 ? 'No result is stored for this task.' : err.message }));
    }
  }

  function newProject() {
    const name = h('input', { class: 'input', type: 'text', maxlength: '200', autocomplete: 'off', placeholder: 'e.g. Release notes' });
    const tasks = h('textarea', { class: 'input textarea', rows: '7', placeholder: 'Summarize the changelog\nDraft the announcement\nList open questions' });
    const count = h('p', { class: 'help', 'aria-live': 'polite', text: '0 tasks' });
    const fName = LR.field({ label: 'Name', control: name });
    const fTasks = LR.field({ label: 'Tasks – one per line', control: tasks });
    fTasks.el.append(count);
    const lines = () => tasks.value.split('\n').map((l) => l.trim()).filter(Boolean);
    tasks.addEventListener('input', () => { count.textContent = fmt.plural(lines().length, 'task'); });
    const form = h('form', { class: 'form', novalidate: true }, [fName.el, fTasks.el]);
    const createBtn = LR.button({ text: 'Create project', variant: 'primary', onclick: (ev) => submit(ev.currentTarget) });
    async function submit(btn) {
      fName.setError('');
      fTasks.setError('');
      if (!name.value.trim()) { fName.setError('Enter a project name.'); name.focus(); return; }
      if (!lines().length) { fTasks.setError('Add at least one task, one per line.'); tasks.focus(); return; }
      LR.setBusy(btn, true, 'Creating…');
      try {
        const data = await LR.post('/api/projects', { name: name.value.trim(), tasks: tasks.value });
        m.close();
        LR.toast('Project created');
        await loadProjects();
        if (data.project) LR.go('#/tasks/' + data.project.id);
      } catch (err) {
        (/task/i.test(err.message) ? fTasks : fName).setError(err.message);
        LR.setBusy(btn, false);
      }
    }
    form.addEventListener('submit', (ev) => { ev.preventDefault(); submit(createBtn); });
    const m = LR.modal({ title: 'New task project', sub: 'Each line becomes one task for the local model.', body: form, actions: [LR.button({ text: 'Cancel', onclick: () => m.close() }), createBtn] });
    name.focus();
  }

  /* ------------------------------------------------------------ view */

  function create() {
    const root = h('section', { class: 'view view-page', 'aria-labelledby': 'tasks-title' });
    const head = LR.pageHead({
      title: 'Task queue',
      sub: 'Queue up text tasks and let the local model work through them one at a time – even overnight.',
      actions: [LR.button({ text: 'Recover stuck tasks', variant: 'ghost', onclick: recover }), LR.button({ text: 'New project', icon: 'plus', variant: 'primary', onclick: newProject })],
    });
    head.querySelector('h1').id = 'tasks-title';
    ui.list = h('div', { class: 'proj-list-wrap' });
    ui.loop = h('section', { class: 'loop-banner', 'aria-live': 'polite', 'aria-label': 'Loop status', hidden: true });
    ui.detail = h('div', { class: 'card detail-card' });
    ui.outcome = h('div', { class: 'outcome', 'aria-live': 'polite' });
    ui.finalSlot = h('div', { class: 'final-slot' });
    root.append(h('div', { class: 'page page-wide' }, [head, h('div', { class: 'split' }, [
      h('aside', { class: 'split-side', 'aria-label': 'Projects' }, ui.list),
      h('div', { class: 'split-main' }, [ui.loop, ui.detail]),
    ])]));
    return root;
  }

  LR.registerView('tasks', {
    title: 'Task queue',
    create,
    show(params) {
      const id = params[0] && /^\d+$/.test(params[0]) ? Number(params[0]) : null;
      if (id !== st.selected) {
        st.selected = id;
        st.detail = null;
        ui.outcome.replaceChildren();
      }
      renderList();
      renderDetail();
      renderLoop();
      poller.start();
      poller.now();
      loadProjects().then(() => {
        if (st.selected === null && st.projects.length && window.matchMedia('(min-width: 900px)').matches) {
          window.history.replaceState(null, '', '#/tasks/' + st.projects[0].id);
          st.selected = st.projects[0].id;
          renderList();
          renderDetail();
          loadDetail();
        }
      }).catch(() => {});
    },
    hide() {
      poller.stop();
    },
  });
})();
