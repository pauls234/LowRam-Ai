/* LowRAM AI dashboard: vanilla JS, no dependencies.
 * Security notes: server and model text is only ever written with
 * textContent / createElement (never innerHTML). Every POST carries the
 * per-process CSRF token from <meta name="lowram-token">.
 */
(function () {
  'use strict';

  const GB = 1024 * 1024 * 1024;
  const POLL_ACTIVE_MS = 2000;
  const POLL_IDLE_MS = 10000;
  const POLL_DOWN_MS = 5000;
  const ERROR_PREVIEW_CHARS = 280;
  const STORAGE = {
    percent: 'lowram.percent',
    project: 'lowram.selectedProject',
    maxTasks: 'lowram.maxTasks',
    loopDismissed: 'lowram.loopDismissed',
  };
  const STATUS_ORDER = ['pending', 'running', 'done', 'failed', 'proposed', 'approved', 'rejected'];
  const STOP_REASONS = {
    queue_empty: { tone: 'ok', text: 'Queue empty: every pending task was processed.' },
    max_tasks: { tone: 'ok', text: 'Reached the max-tasks limit.' },
    failure: { tone: 'error', text: 'Stopped because a task failed.' },
    paused_low_ram: { tone: 'warn', text: 'Paused: not enough free RAM.' },
    stop_requested: { tone: 'info', text: 'Stopped on request.' },
    busy: { tone: 'warn', text: 'Did not start: another task was already running.' },
    error: { tone: 'error', text: 'Stopped because of an unexpected error.' },
  };

  /* ---------------- Storage (never throws) ---------------- */

  const storage = {
    get(key) {
      try { return window.localStorage.getItem(key); } catch (_) { return null; }
    },
    set(key, value) {
      try { window.localStorage.setItem(key, String(value)); } catch (_) { /* ignore */ }
    },
    remove(key) {
      try { window.localStorage.removeItem(key); } catch (_) { /* ignore */ }
    },
  };

  /* ---------------- DOM helpers ---------------- */

  function $(id) {
    return document.getElementById(id);
  }

  /** createElement helper. Text is always set via textContent / text nodes. */
  function h(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const key of Object.keys(attrs)) {
        const value = attrs[key];
        if (value === null || value === undefined || value === false) continue;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = String(value);
        else if (key === 'dataset') Object.assign(node.dataset, value);
        else if (key === 'onclick') node.addEventListener('click', value);
        else node.setAttribute(key, value === true ? '' : String(value));
      }
    }
    if (children !== undefined && children !== null) {
      const list = Array.isArray(children) ? children : [children];
      for (const child of list) {
        if (child === null || child === undefined || child === false) continue;
        node.append(child instanceof Node ? child : String(child));
      }
    }
    return node;
  }

  /* ---------------- Formatting ---------------- */

  function isNum(n) {
    return typeof n === 'number' && Number.isFinite(n);
  }

  function clamp(n, min, max) {
    return Math.min(max, Math.max(min, n));
  }

  function fmtGB(bytes) {
    return isNum(bytes) ? (bytes / GB).toFixed(1) + ' GB' : '—';
  }

  function fmtInt(n) {
    return isNum(n) ? Math.round(n).toLocaleString('en-US') : '—';
  }

  function fmtPercent(n) {
    return isNum(n) ? Math.round(n) + '%' : '—';
  }

  function plural(n, word) {
    return n + ' ' + word + (n === 1 ? '' : 's');
  }

  function parseTime(value) {
    if (!value) return null;
    let s = String(value).trim();
    // "YYYY-MM-DD HH:MM:SS" (SQLite CURRENT_TIMESTAMP) is UTC without a zone.
    if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(s)) s = s.replace(' ', 'T') + 'Z';
    const d = new Date(s);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function fmtTime(value) {
    const d = parseTime(value);
    if (!d) return value ? String(value) : '';
    const time = d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
    if (d.toDateString() === new Date().toDateString()) return time;
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ', ' + time;
  }

  function fmtDateLong(value) {
    const d = parseTime(value);
    if (!d) return value ? String(value) : '';
    return d.toLocaleString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
    });
  }

  function fmtElapsed(ms) {
    const total = Math.max(0, Math.floor(ms / 1000));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return m + ':' + String(s).padStart(2, '0');
  }

  function taskLabel(task) {
    if (!task) return 'Task';
    return isNum(task.position) ? 'Task #' + task.position : 'Task ' + task.id;
  }

  /* ---------------- State ---------------- */

  const state = {
    token: '',
    percent: 40,
    memory: null,
    memorySeq: 0,
    projects: [],
    projectsLoaded: false,
    projectsKey: '',
    selectedId: null,
    detail: null,
    detailSeq: 0,
    tasksKey: '',
    loop: null,
    loopDismissed: null,
    runInFlight: null,
    runStartedAt: 0,
    runTimer: 0,
    loopRequestInFlight: false,
    backendDown: false,
    tokenRejected: false,
    polling: false,
    pollTimer: 0,
    memoryDebounce: 0,
    dialogReturnKey: null,
    dialogReturnEl: null,
  };

  const ui = {};

  /* ---------------- API ---------------- */

  class ApiError extends Error {
    constructor(message, status, network) {
      super(message);
      this.status = status || 0;
      this.network = Boolean(network);
    }
  }

  async function api(method, path, body) {
    const init = {
      method,
      headers: { Accept: 'application/json' },
      cache: 'no-store',
      credentials: 'same-origin',
    };
    if (method !== 'GET') {
      init.headers['Content-Type'] = 'application/json';
      init.headers['X-LowRAM-Token'] = state.token;
      init.body = JSON.stringify(body || {});
    }

    let res;
    let text = '';
    try {
      res = await fetch(path, init);
      text = await res.text();
    } catch (_) {
      setBackendDown(true);
      throw new ApiError('Backend not reachable.', 0, true);
    }
    setBackendDown(false);

    let data = null;
    if (text) {
      try { data = JSON.parse(text); } catch (_) { data = null; }
    }
    if (!res.ok) {
      const msg = data && typeof data.error === 'string' && data.error
        ? data.error
        : 'Request failed (HTTP ' + res.status + ').';
      if (res.status === 403 && method !== 'GET') setTokenRejected(true);
      throw new ApiError(msg, res.status);
    }
    if (!data || typeof data !== 'object') {
      throw new ApiError('Unexpected response from the server.', res.status);
    }
    return data;
  }

  /* ---------------- Banner (backend down / token) ---------------- */

  function setBackendDown(down) {
    if (state.backendDown === down) return;
    state.backendDown = down;
    renderBanner();
    if (!down) schedulePoll(300);
  }

  function setTokenRejected(rejected) {
    if (state.tokenRejected === rejected) return;
    state.tokenRejected = rejected;
    renderBanner();
  }

  function renderBanner() {
    if (state.backendDown) {
      ui.bannerTitle.textContent = 'Backend not reachable.';
      ui.bannerMessage.textContent = 'Check that the LowRAM AI server is running. Retrying automatically…';
      ui.bannerAction.textContent = 'Retry now';
      ui.bannerAction.dataset.action = 'retry';
      ui.banner.hidden = false;
    } else if (state.tokenRejected) {
      ui.bannerTitle.textContent = 'Session token rejected.';
      ui.bannerMessage.textContent = 'The server was probably restarted. Reload the page to continue.';
      ui.bannerAction.textContent = 'Reload page';
      ui.bannerAction.dataset.action = 'reload';
      ui.banner.hidden = false;
    } else {
      ui.banner.hidden = true;
    }
  }

  /* ---------------- Memory ---------------- */

  async function loadMemory() {
    const seq = ++state.memorySeq;
    const data = await api('GET', '/api/memory?percent=' + encodeURIComponent(state.percent));
    if (seq === state.memorySeq) renderMemory(data);
  }

  function renderMemory(m) {
    if (m && typeof m === 'object') state.memory = m;
    const mem = state.memory;
    if (!mem) return;

    ui.ramTotal.textContent = fmtGB(mem.total_bytes);
    ui.ramAvailable.textContent = fmtGB(mem.available_bytes);
    ui.ramUsed.textContent = fmtPercent(mem.used_percent);

    const total = isNum(mem.total_bytes) && mem.total_bytes > 0 ? mem.total_bytes : 0;
    const availPct = total ? clamp((mem.available_bytes / total) * 100, 0, 100) : 0;
    const budgetPct = total ? clamp((mem.budget_bytes / total) * 100, 0, 100) : 0;
    const reservePct = total && isNum(mem.min_free_bytes) ? clamp((mem.min_free_bytes / total) * 100, 0, 100) : 0;
    ui.gauge.style.setProperty('--avail', availPct.toFixed(2) + '%');
    ui.gauge.style.setProperty('--budget', budgetPct.toFixed(2) + '%');
    ui.gauge.style.setProperty('--reserve', reservePct.toFixed(2) + '%');
    ui.gauge.classList.toggle('is-low', !mem.can_run);
    ui.gauge.setAttribute('aria-valuenow', String(Math.round(availPct)));
    ui.gauge.setAttribute('aria-valuetext',
      fmtGB(mem.available_bytes) + ' available of ' + fmtGB(mem.total_bytes) +
      '; planning budget ' + fmtGB(mem.budget_bytes) + '; reserve ' + fmtGB(mem.min_free_bytes));
    ui.legendAvailable.textContent = fmtGB(mem.available_bytes);
    ui.legendBudget.textContent = fmtGB(mem.budget_bytes);
    ui.ramMinFree.textContent = fmtGB(mem.min_free_bytes);

    if (mem.can_run) {
      ui.ramPill.className = 'pill pill-ok';
      ui.ramPillText.textContent = 'Ready';
      ui.ramReason.hidden = true;
      ui.ramReason.textContent = '';
    } else {
      ui.ramPill.className = 'pill pill-warn';
      ui.ramPillText.textContent = 'Paused – low RAM';
      ui.ramReason.textContent = mem.reason ? String(mem.reason) : 'Not enough free memory to start a task.';
      ui.ramReason.hidden = false;
    }

    ui.budgetBytes.textContent = fmtGB(mem.budget_bytes);
    ui.budgetContext.textContent = isNum(mem.context_chars) ? fmtInt(mem.context_chars) + ' chars' : '—';
    ui.budgetTokens.textContent = fmtInt(mem.max_tokens);

    renderRunControls();
  }

  function renderMemoryUnavailable() {
    if (state.memory) return;
    ui.ramPill.className = 'pill pill-unknown';
    ui.ramPillText.textContent = 'Unavailable';
  }

  function setPercent(value, persist) {
    const snapped = clamp(Math.round(Number(value) / 5) * 5, 20, 80);
    state.percent = Number.isFinite(snapped) ? snapped : 40;
    ui.budgetRange.value = String(state.percent);
    ui.budgetOutput.textContent = state.percent + '%';
    ui.budgetRange.setAttribute('aria-valuetext', state.percent + ' percent of total RAM');
    if (persist) storage.set(STORAGE.percent, state.percent);
    renderRunControls();
  }

  function onBudgetInput() {
    setPercent(ui.budgetRange.value, true);
    window.clearTimeout(state.memoryDebounce);
    state.memoryDebounce = window.setTimeout(() => {
      loadMemory().catch(() => {});
    }, 200);
  }

  /* ---------------- Projects ---------------- */

  function projectById(id) {
    return state.projects.find((p) => p.id === id) || null;
  }

  function currentProject() {
    if (state.selectedId === null) return null;
    if (state.detail && state.detail.project && state.detail.project.id === state.selectedId) {
      return state.detail.project;
    }
    return projectById(state.selectedId);
  }

  function isLoopActive() {
    return Boolean(state.loop && state.loop.active);
  }

  async function loadProjects() {
    const data = await api('GET', '/api/projects');
    state.projects = Array.isArray(data.projects) ? data.projects : [];
    state.projectsLoaded = true;

    if (state.selectedId !== null && !projectById(state.selectedId)) {
      state.selectedId = null;
      state.detail = null;
      storage.remove(STORAGE.project);
      renderDetail();
    }
    if (state.selectedId === null && state.projects.length) {
      selectProject(state.projects[0].id, { quiet: true });
    }
    renderProjects();
    renderLoop(); // project names may have arrived after the loop status
  }

  function renderProjects() {
    const loopPid = isLoopActive() ? state.loop.project_id : null;
    const runPid = state.runInFlight ? state.runInFlight.projectId : null;
    const key = JSON.stringify([state.projects, state.selectedId, loopPid, runPid]);
    if (key === state.projectsKey) return;
    state.projectsKey = key;

    const focusedId = focusedProjectId();
    ui.projectList.replaceChildren(...state.projects.map((p) => projectItem(p, loopPid, runPid)));

    if (!state.projectsLoaded) {
      ui.projectsEmpty.textContent = 'Loading projects…';
      ui.projectsEmpty.hidden = false;
    } else if (!state.projects.length) {
      ui.projectsEmpty.textContent = 'No projects yet. Create one with the form.';
      ui.projectsEmpty.hidden = false;
    } else {
      ui.projectsEmpty.hidden = true;
    }

    if (focusedId !== null) {
      const btn = ui.projectList.querySelector('[data-id="' + CSS.escape(String(focusedId)) + '"]');
      if (btn) btn.focus();
    }
  }

  function focusedProjectId() {
    const active = document.activeElement;
    if (!active || !ui.projectList.contains(active)) return null;
    const item = active.closest('.project-item');
    return item ? item.dataset.id : null;
  }

  function countSummary(counts, total) {
    const parts = [plural(total, 'task')];
    for (const s of STATUS_ORDER) {
      if (counts[s]) parts.push(counts[s] + ' ' + s);
    }
    return parts.join(' · ');
  }

  function projectTotal(p) {
    if (isNum(p.total)) return p.total;
    const counts = p.counts || {};
    return STATUS_ORDER.reduce((sum, s) => sum + (counts[s] || 0), 0);
  }

  function projectItem(p, loopPid, runPid) {
    const counts = p.counts || {};
    const total = projectTotal(p);
    const finished = (counts.done || 0) + (counts.approved || 0);
    const pct = total ? Math.round((finished / total) * 100) : 0;

    const fill = h('span', { class: 'mini-progress-fill' });
    const bar = h('span', { class: 'mini-progress', 'aria-hidden': 'true' }, fill);
    bar.style.setProperty('--value', pct + '%');

    const head = h('span', { class: 'project-item-head' }, [
      h('span', { class: 'project-name', text: p.name || 'Untitled' }),
      p.kind === 'coding' ? h('span', { class: 'badge badge-kind-coding', text: 'coding' }) : null,
      p.id === loopPid ? h('span', { class: 'badge badge-loop', text: 'loop' }) : null,
      p.id === runPid && p.id !== loopPid ? h('span', { class: 'badge badge-loop', text: 'running' }) : null,
    ]);

    const button = h('button', {
      type: 'button',
      class: 'project-item',
      'aria-current': p.id === state.selectedId ? 'true' : null,
      dataset: { id: String(p.id) },
      onclick: () => selectProject(p.id, { focusDetail: false, scroll: true }),
    }, [
      head,
      bar,
      h('span', { class: 'project-meta', text: countSummary(counts, total) }),
    ]);
    return h('li', null, button);
  }

  function selectProject(id, opts) {
    const options = opts || {};
    const changed = state.selectedId !== id;
    state.selectedId = id;
    storage.set(STORAGE.project, id);
    if (changed) {
      state.detail = null;
      state.tasksKey = '';
      if (!options.quiet) clearOutcome();
    }
    renderProjects();
    renderDetail();
    loadDetail().catch(() => {});
    if (options.scroll && window.matchMedia('(max-width: 899px)').matches) {
      const smooth = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      ui.detail.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'start' });
    }
  }

  async function loadDetail() {
    const id = state.selectedId;
    if (id === null) return;
    const seq = ++state.detailSeq;
    try {
      const data = await api('GET', '/api/projects/' + encodeURIComponent(id));
      if (seq !== state.detailSeq || id !== state.selectedId) return;
      state.detail = {
        project: data.project || projectById(id),
        tasks: Array.isArray(data.tasks) ? data.tasks : [],
      };
      renderDetail();
    } catch (err) {
      if (seq !== state.detailSeq || id !== state.selectedId) return;
      if (err.status === 404) {
        state.selectedId = null;
        state.detail = null;
        storage.remove(STORAGE.project);
        renderDetail();
        renderProjects();
      }
      throw err;
    }
  }

  /* ---------------- Detail ---------------- */

  function renderDetail() {
    const p = currentProject();
    if (!p) {
      ui.detailTitle.textContent = 'No project selected';
      ui.detailSub.hidden = true;
      ui.detailCounts.replaceChildren();
      ui.detailEmpty.hidden = false;
      ui.detailBody.hidden = true;
      return;
    }

    ui.detailEmpty.hidden = true;
    ui.detailBody.hidden = false;
    ui.detailTitle.textContent = p.name || 'Untitled';
    ui.detailSub.hidden = false;
    const coding = p.kind === 'coding';
    ui.detailKind.className = 'badge ' + (coding ? 'badge-kind-coding' : 'badge-kind-general');
    ui.detailKind.textContent = coding ? 'coding' : 'general';
    ui.detailCreated.textContent = p.created_at ? 'Created ' + fmtDateLong(p.created_at) : '';

    // Count chips
    const counts = p.counts || {};
    const chips = [];
    for (const s of STATUS_ORDER) {
      if (counts[s]) chips.push(h('li', { class: 'badge status status-' + s, text: counts[s] + ' ' + s }));
    }
    ui.detailCounts.replaceChildren(...chips);

    // Goal / repo
    const meta = [];
    if (p.goal) meta.push(h('dt', { text: 'Goal' }), h('dd', { text: p.goal }));
    if (p.repo_path) meta.push(h('dt', { text: 'Repository' }), h('dd', null, h('code', { text: p.repo_path })));
    ui.detailMeta.replaceChildren(...meta);
    ui.detailMeta.hidden = meta.length === 0;

    ui.codingNote.hidden = !coding;
    renderRunControls();
    renderTasks();
  }

  function renderTasks() {
    const p = currentProject();
    const tasks = state.detail && state.detail.project && state.detail.project.id === state.selectedId
      ? state.detail.tasks
      : null;
    const key = JSON.stringify([state.selectedId, p ? p.kind : null, tasks, isLoopActive() ? state.loop.current_task_id : null]);
    if (key === state.tasksKey) return;
    state.tasksKey = key;

    const focusKey = focusedKeyIn(ui.taskRows);

    if (tasks === null) {
      ui.taskRows.replaceChildren();
      ui.tasksEmpty.textContent = 'Loading tasks…';
      ui.tasksEmpty.hidden = false;
      return;
    }
    if (!tasks.length) {
      ui.taskRows.replaceChildren();
      ui.tasksEmpty.textContent = 'This project has no tasks.';
      ui.tasksEmpty.hidden = false;
      return;
    }
    ui.tasksEmpty.hidden = true;
    const coding = p && p.kind === 'coding';
    ui.taskRows.replaceChildren(...tasks.map((t) => taskRow(t, coding)));
    restoreFocus(ui.taskRows, focusKey);
  }

  function taskRow(t, coding) {
    const status = STATUS_ORDER.includes(t.status) ? t.status : 'pending';
    const titleCell = h('td', { class: 'col-title' }, h('span', { class: 'task-title', text: t.title || '(untitled task)' }));

    if (t.status === 'failed' && t.error) {
      const err = String(t.error);
      const errEl = h('p', { class: 'task-error' });
      if (err.length > ERROR_PREVIEW_CHARS) {
        errEl.append(err.slice(0, ERROR_PREVIEW_CHARS).trimEnd() + '… ');
        errEl.append(h('button', {
          type: 'button',
          class: 'btn-link',
          dataset: { focusKey: 'error-' + t.id },
          text: 'Show full error',
          onclick: (ev) => openTextDialog(taskLabel(t) + ' error', t.title || '', err, ev.currentTarget),
        }));
      } else {
        errEl.textContent = err;
      }
      titleCell.append(errEl);
    }

    const actions = [];
    if (t.has_result) {
      actions.push(h('button', {
        type: 'button',
        class: 'btn btn-secondary btn-sm',
        dataset: { focusKey: 'result-' + t.id },
        'aria-label': 'View result of ' + taskLabel(t),
        text: 'View result',
        onclick: (ev) => openResult(t, ev.currentTarget),
      }));
    }
    if (t.status === 'failed' && !coding) {
      actions.push(h('button', {
        type: 'button',
        class: 'btn btn-ghost btn-sm',
        dataset: { focusKey: 'retry-' + t.id },
        'aria-label': 'Retry ' + taskLabel(t),
        text: 'Retry',
        onclick: (ev) => retryTask(t, ev.currentTarget),
      }));
    }

    const finishedText = t.finished_at ? fmtTime(t.finished_at) : '';
    const finishedCell = h('td', { class: 'col-finished' + (finishedText ? '' : ' is-blank') }, [
      h('span', { class: 'cell-label', text: 'Finished' }),
      finishedText
        ? h('time', { datetime: String(t.finished_at), title: fmtDateLong(t.finished_at), text: finishedText })
        : h('span', { 'aria-label': 'not finished', text: '—' }),
    ]);

    const attempts = isNum(t.attempts) ? t.attempts : 0;
    const attemptsCell = h('td', { class: 'col-attempts' + (attempts ? '' : ' is-blank') }, [
      h('span', { class: 'cell-label', text: 'Attempts' }),
      String(attempts),
    ]);

    const isCurrent = status === 'running' || (isLoopActive() && state.loop.current_task_id === t.id);
    return h('tr', { class: isCurrent ? 'is-running' : null, dataset: { taskId: String(t.id) } }, [
      h('td', { class: 'col-pos', text: isNum(t.position) ? String(t.position) : '' }),
      titleCell,
      h('td', { class: 'col-status' }, h('span', { class: 'badge status status-' + status, text: t.status || 'pending' })),
      attemptsCell,
      finishedCell,
      h('td', { class: 'col-actions' }, actions.length ? h('div', { class: 'row-actions' }, actions) : null),
    ]);
  }

  function focusedKeyIn(container) {
    const active = document.activeElement;
    if (!active || !container.contains(active)) return null;
    return active.dataset ? active.dataset.focusKey || null : null;
  }

  function restoreFocus(container, key) {
    if (!key) return false;
    const target = container.querySelector('[data-focus-key="' + CSS.escape(key) + '"]');
    if (target) {
      target.focus();
      return true;
    }
    return false;
  }

  /* ---------------- Run controls ---------------- */

  function renderRunControls() {
    const p = currentProject();
    const general = Boolean(p) && p.kind !== 'coding';
    ui.runControls.hidden = !general;

    const loopActive = isLoopActive();
    const busy = Boolean(state.runInFlight) || loopActive || state.loopRequestInFlight;
    ui.runNext.disabled = busy;
    ui.runLoop.disabled = busy;
    ui.maxTasks.disabled = busy;
    ui.runNext.setAttribute('aria-busy', state.runInFlight ? 'true' : 'false');
    ui.recoverBtn.disabled = Boolean(state.runInFlight) || loopActive;

    if (state.runInFlight) {
      const sameProject = p && state.runInFlight.projectId === p.id;
      ui.runBusyText.textContent = sameProject
        ? 'Running… this can take a while.'
        : 'Running a task in “' + state.runInFlight.name + '”… this can take a while.';
      ui.runBusy.hidden = false;
    } else {
      ui.runBusy.hidden = true;
    }

    let hint = 'Runs the next pending task with a ' + state.percent + '% planning budget.';
    let warn = false;
    if (loopActive) {
      hint = 'A loop is running. Use “Stop loop” above to end it after the current task.';
    } else if (state.memory && state.memory.can_run === false) {
      hint = 'Not enough free RAM right now, so a run would pause. Close other apps, then try again.';
      warn = true;
    }
    ui.runHint.textContent = hint;
    ui.runHint.classList.toggle('is-warn', warn);
  }

  function startRunTimer() {
    state.runStartedAt = Date.now();
    ui.runElapsed.textContent = '0:00';
    window.clearInterval(state.runTimer);
    state.runTimer = window.setInterval(() => {
      ui.runElapsed.textContent = fmtElapsed(Date.now() - state.runStartedAt);
    }, 1000);
  }

  function stopRunTimer() {
    window.clearInterval(state.runTimer);
    state.runTimer = 0;
    ui.runElapsed.textContent = '';
  }

  async function runNext() {
    const p = currentProject();
    if (!p || p.kind === 'coding' || state.runInFlight || isLoopActive()) return;

    state.runInFlight = { projectId: p.id, name: p.name || 'Untitled' };
    clearOutcome();
    startRunTimer();
    renderRunControls();
    renderProjects();
    schedulePoll(POLL_ACTIVE_MS);

    try {
      const data = await api('POST', '/api/projects/' + encodeURIComponent(p.id) + '/run', { percent: state.percent });
      if (data.memory) renderMemory(data.memory);
      showRunOutcome(p, data);
    } catch (err) {
      if (err.status === 409) {
        showOutcome('warn', 'Busy', err.message, 'Another task or loop is already running. Try again when it finishes.');
      } else if (err.network) {
        showOutcome('error', 'Run failed', 'The backend could not be reached. The task may still be running; it will show up after the next refresh.');
      } else {
        showOutcome('error', 'Run failed', err.message);
      }
    } finally {
      state.runInFlight = null;
      stopRunTimer();
      renderRunControls();
      renderProjects();
      refreshAll();
    }
  }

  function showRunOutcome(project, data) {
    const task = data.task || null;
    const title = task && task.title ? task.title : '';
    // Name the project when the user has switched to another one meanwhile.
    const prefix = state.selectedId !== project.id ? '“' + (project.name || 'Untitled') + '”: ' : '';
    const label = prefix + taskLabel(task);
    switch (data.status) {
      case 'done': {
        const actions = task ? [outcomeButton('View result', (ev) => openResult(task, ev.currentTarget), 'btn-secondary')] : [];
        showOutcome('ok', label + ' done', title, null, actions);
        break;
      }
      case 'failed': {
        const actions = task ? [outcomeButton('Retry task', (ev) => retryTask(task, ev.currentTarget), 'btn-secondary')] : [];
        showOutcome('error', label + ' failed' + (title ? ': ' + title : ''),
          data.error || data.reason || 'Unknown error.',
          'If your local model endpoint is down, start it and retry the task.', actions);
        break;
      }
      case 'paused':
        showOutcome('warn', 'Paused – low RAM',
          data.reason || 'Not enough free memory to start a task.',
          'Free some memory (for example, close other apps), then run again.');
        break;
      case 'empty':
        showOutcome('info', 'Nothing to run', data.reason || 'This project has no pending tasks.');
        break;
      default:
        showOutcome('info', 'Run finished', 'Status: ' + String(data.status || 'unknown'));
    }
  }

  function outcomeButton(text, onclick, variant) {
    return h('button', { type: 'button', class: 'btn btn-sm ' + (variant || 'btn-secondary'), text, onclick });
  }

  function showOutcome(tone, title, message, hint, actions) {
    const body = h('div', { class: 'outcome-body' }, [
      h('p', { class: 'outcome-title', text: title }),
      message ? h('p', { class: 'outcome-msg', text: message }) : null,
      hint ? h('p', { class: 'outcome-hint', text: hint }) : null,
    ]);
    const box = h('div', { class: 'outcome outcome-' + tone }, [
      body,
      actions && actions.length ? h('div', { class: 'outcome-actions' }, actions) : null,
    ]);
    ui.outcome.replaceChildren(box);
  }

  function clearOutcome() {
    ui.outcome.replaceChildren();
  }

  /** Screen-reader-only message in the live region (the loop banner shows it visually). */
  function announce(text) {
    ui.outcome.replaceChildren(h('p', { class: 'visually-hidden', text }));
  }

  function readMaxTasks() {
    const raw = ui.maxTasks.value.trim();
    const n = Number(raw);
    if (!raw || !Number.isInteger(n) || n < 1 || n > 100) {
      ui.maxTasks.setAttribute('aria-invalid', 'true');
      ui.maxTasksError.textContent = 'Max tasks must be a whole number from 1 to 100.';
      ui.maxTasksError.hidden = false;
      ui.maxTasks.focus();
      return null;
    }
    ui.maxTasks.removeAttribute('aria-invalid');
    ui.maxTasksError.hidden = true;
    return n;
  }

  async function startLoop() {
    const p = currentProject();
    if (!p || p.kind === 'coding' || state.runInFlight || isLoopActive()) return;
    const maxTasks = readMaxTasks();
    if (maxTasks === null) return;
    storage.set(STORAGE.maxTasks, maxTasks);

    state.loopRequestInFlight = true;
    clearOutcome();
    renderRunControls();
    try {
      const data = await api('POST', '/api/projects/' + encodeURIComponent(p.id) + '/loop', {
        percent: state.percent,
        max_tasks: maxTasks,
      });
      if (data.loop) applyLoop(data.loop, { fromStart: true });
      announce('Loop started: running up to ' + plural(maxTasks, 'task') + ' in “' + (p.name || 'Untitled') + '”, one at a time.');
    } catch (err) {
      showOutcome(err.status === 409 ? 'warn' : 'error', 'Could not start the loop', err.message);
    } finally {
      state.loopRequestInFlight = false;
      renderRunControls();
      schedulePoll(500);
    }
  }

  async function stopLoop() {
    ui.loopStop.disabled = true;
    try {
      const data = await api('POST', '/api/loop/stop', {});
      if (data.loop) applyLoop(data.loop);
    } catch (err) {
      showOutcome('error', 'Could not stop the loop', err.message);
      ui.loopStop.disabled = false;
    }
  }

  async function retryTask(task, trigger) {
    if (trigger) trigger.disabled = true;
    try {
      const data = await api('POST', '/api/tasks/' + encodeURIComponent(task.id) + '/retry', {});
      const t = data.task || task;
      showOutcome('info', taskLabel(t) + ' queued again', (t.title || '') + ' is back in the pending queue.');
    } catch (err) {
      showOutcome('error', 'Could not retry ' + taskLabel(task), err.message);
      if (trigger && trigger.isConnected) trigger.disabled = false;
    } finally {
      refreshAll();
    }
  }

  /* ---------------- Loop status ---------------- */

  async function loadLoop() {
    const data = await api('GET', '/api/loop');
    applyLoop(data.loop || null);
  }

  function applyLoop(loop, opts) {
    const wasActive = isLoopActive();
    state.loop = loop;
    const nowActive = isLoopActive();
    if (opts && opts.fromStart) {
      state.loopDismissed = null;
      storage.remove(STORAGE.loopDismissed);
    }

    if (wasActive && !nowActive && loop) {
      const reason = STOP_REASONS[loop.stopped_because] || { tone: 'info', text: 'Loop finished.' };
      announce('Loop finished. ' + reason.text + ' ' + plural(loop.completed || 0, 'task') +
        ' completed, ' + (loop.failed || 0) + ' failed.');
      refreshAll();
    }
    renderLoop();
    renderRunControls();
    renderProjects();
    renderTasks();
  }

  function loopKey(loop) {
    return loop ? String(loop.started_at || '') + '|' + String(loop.finished_at || '') : '';
  }

  function renderLoop() {
    const loop = state.loop;
    const active = isLoopActive();
    const finishedVisible = Boolean(loop) && !active && Boolean(loop.started_at) && Boolean(loop.finished_at) &&
      state.loopDismissed !== loopKey(loop);
    if (!active && !finishedVisible) {
      ui.loopBanner.hidden = true;
      return;
    }
    ui.loopBanner.hidden = false;

    const project = loop.project_id !== null && loop.project_id !== undefined ? projectById(loop.project_id) : null;
    const projectName = project ? project.name : (loop.project_id !== null && loop.project_id !== undefined ? 'Project ' + loop.project_id : '');
    ui.loopProject.textContent = projectName ? 'Project: ' + projectName : '';
    ui.loopView.hidden = !project || project.id === state.selectedId;

    const completed = loop.completed || 0;
    const failed = loop.failed || 0;
    const max = isNum(loop.max_tasks) && loop.max_tasks > 0 ? loop.max_tasks : null;
    const pct = max ? clamp((completed / max) * 100, 0, 100) : 0;
    ui.loopProgress.style.setProperty('--progress', pct.toFixed(1) + '%');
    ui.loopProgress.setAttribute('aria-valuemax', String(max || 1));
    ui.loopProgress.setAttribute('aria-valuenow', String(max ? Math.min(completed, max) : 0));
    ui.loopProgress.setAttribute('aria-valuetext', completed + (max ? ' of ' + max : '') + ' tasks completed');

    const parts = [completed + (max ? '/' + max : '') + ' completed'];
    if (failed) parts.push(failed + ' failed');

    let classes = 'loop-banner';
    if (active) {
      ui.loopTitle.textContent = loop.stop_requested ? 'Loop stopping…' : 'Loop running';
      if (loop.current_task_id !== null && loop.current_task_id !== undefined) {
        parts.push('Current: ' + describeTask(loop.current_task_id, loop.project_id));
      }
      if (loop.stop_requested) parts.push('Stops after the current task finishes');
      ui.loopStop.hidden = false;
      ui.loopStop.disabled = Boolean(loop.stop_requested);
      ui.loopStop.textContent = loop.stop_requested ? 'Stopping…' : 'Stop loop';
      ui.loopDismiss.hidden = true;
    } else {
      const reason = STOP_REASONS[loop.stopped_because] || { tone: 'info', text: 'Loop finished.' };
      ui.loopTitle.textContent = 'Loop finished';
      parts.push(reason.text);
      if (loop.finished_at) parts.push('Finished ' + fmtTime(loop.finished_at));
      ui.loopStop.hidden = true;
      ui.loopDismiss.hidden = false;
      classes += ' is-finished tone-' + reason.tone;
    }
    ui.loopBanner.className = classes;
    ui.loopStats.textContent = parts.join(' · ');
    ui.loopMessage.textContent = loop.last_message ? String(loop.last_message) : '';
    ui.loopMessage.hidden = !loop.last_message;
  }

  function describeTask(taskId, projectId) {
    if (state.detail && state.detail.project && state.detail.project.id === projectId) {
      const t = state.detail.tasks.find((x) => x.id === taskId);
      if (t) return '#' + t.position + ' ' + (t.title || '');
    }
    return 'task ' + taskId;
  }

  /* ---------------- Results dialog ---------------- */

  function openDialog(title, sub, trigger) {
    state.dialogReturnEl = trigger || document.activeElement;
    state.dialogReturnKey = trigger && trigger.dataset ? trigger.dataset.focusKey || null : null;
    ui.dialogTitle.textContent = title;
    ui.dialogSub.textContent = sub || '';
    ui.dialogMeta.textContent = '';
    ui.dialogCopy.disabled = true;
    ui.dialogCopy.textContent = 'Copy';
    if (!ui.dialog.open) {
      if (typeof ui.dialog.showModal === 'function') ui.dialog.showModal();
      else ui.dialog.setAttribute('open', '');
    }
  }

  function setDialogContent(text, kind) {
    ui.dialogContent.textContent = text;
    ui.dialogContent.className = 'result-pre' + (kind ? ' is-' + kind : '');
    ui.dialogContent.scrollTop = 0;
    const ok = !kind;
    ui.dialogCopy.disabled = !ok;
    if (ok) {
      const lines = text ? text.split('\n').length : 0;
      ui.dialogMeta.textContent = fmtInt(text.length) + ' characters · ' + plural(lines, 'line');
    }
  }

  async function openResult(task, trigger) {
    openDialog('Result · ' + taskLabel(task) + (task.title ? ': ' + task.title : ''), '', trigger);
    setDialogContent('Loading result…', 'loading');
    ui.dialogClose.focus();
    try {
      const data = await api('GET', '/api/tasks/' + encodeURIComponent(task.id) + '/result');
      if (!ui.dialog.open) return;
      ui.dialogSub.textContent = data.path ? String(data.path) : '';
      setDialogContent(typeof data.content === 'string' ? data.content : '', null);
      if (!data.content) ui.dialogMeta.textContent = 'The result file is empty.';
      ui.dialogContent.focus();
    } catch (err) {
      if (!ui.dialog.open) return;
      setDialogContent(err.status === 404 ? 'No result is stored for this task.' : 'Could not load the result: ' + err.message, 'error');
    }
  }

  function openTextDialog(title, sub, text, trigger) {
    openDialog(title, sub, trigger);
    setDialogContent(text, null);
    ui.dialogContent.focus();
  }

  function closeDialog() {
    if (ui.dialog.open) {
      if (typeof ui.dialog.close === 'function') ui.dialog.close();
      else ui.dialog.removeAttribute('open');
    }
  }

  function onDialogClosed() {
    const key = state.dialogReturnKey;
    const el = state.dialogReturnEl;
    state.dialogReturnKey = null;
    state.dialogReturnEl = null;
    if (el && el.isConnected && typeof el.focus === 'function') {
      el.focus();
      return;
    }
    if (key && restoreFocus(document.body, key)) return;
    ui.detail.focus();
  }

  async function copyDialogContent() {
    const text = ui.dialogContent.textContent || '';
    try {
      await navigator.clipboard.writeText(text);
      ui.dialogCopy.textContent = 'Copied';
    } catch (_) {
      // Fallback: select the text so the user can copy it manually.
      const range = document.createRange();
      range.selectNodeContents(ui.dialogContent);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      ui.dialogCopy.textContent = 'Press Ctrl+C';
    }
    window.setTimeout(() => { ui.dialogCopy.textContent = 'Copy'; }, 2000);
  }

  /* ---------------- New project ---------------- */

  function taskLines() {
    return ui.projectTasks.value.split('\n').map((l) => l.trim()).filter(Boolean);
  }

  function updateTaskCount() {
    ui.taskCount.textContent = plural(taskLines().length, 'task');
  }

  function setFormError(message, field) {
    ui.formError.textContent = message;
    ui.formError.hidden = !message;
    for (const input of [ui.projectName, ui.projectTasks]) {
      if (message && input === field) {
        input.setAttribute('aria-invalid', 'true');
        input.setAttribute('aria-errormessage', 'form-error');
      } else {
        input.removeAttribute('aria-invalid');
        input.removeAttribute('aria-errormessage');
      }
    }
  }

  function guessField(message) {
    const m = message.toLowerCase();
    if (m.includes('name')) return ui.projectName;
    if (m.includes('task')) return ui.projectTasks;
    return null;
  }

  async function createProject(ev) {
    ev.preventDefault();
    ui.formStatus.textContent = '';
    const name = ui.projectName.value.trim();
    if (!name) {
      setFormError('Enter a project name.', ui.projectName);
      ui.projectName.focus();
      return;
    }
    if (!taskLines().length) {
      setFormError('Add at least one task, one per line.', ui.projectTasks);
      ui.projectTasks.focus();
      return;
    }
    setFormError('', null);

    ui.createBtn.disabled = true;
    ui.createBtn.textContent = 'Creating…';
    try {
      const data = await api('POST', '/api/projects', { name, tasks: ui.projectTasks.value });
      const project = data.project;
      ui.form.reset();
      updateTaskCount();
      const total = project ? projectTotal(project) : 0;
      ui.formStatus.textContent = 'Created “' + (project && project.name ? project.name : name) + '” with ' + plural(total, 'task') + '.';
      if (project && project.id !== undefined) {
        if (!projectById(project.id)) state.projects.push(project);
        selectProject(project.id, { scroll: true });
      }
      loadProjects().catch(() => {});
    } catch (err) {
      const field = guessField(err.message);
      setFormError(err.message, field);
      if (field) field.focus();
    } finally {
      ui.createBtn.disabled = false;
      ui.createBtn.textContent = 'Create';
    }
  }

  /* ---------------- Recover ---------------- */

  async function recover() {
    ui.recoverBtn.disabled = true;
    ui.recoverStatus.textContent = '';
    try {
      const data = await api('POST', '/api/recover', {});
      const ids = Array.isArray(data.recovered) ? data.recovered : [];
      ui.recoverStatus.textContent = ids.length
        ? 'Marked ' + plural(ids.length, 'stuck task') + ' as failed (' + ids.map((id) => 'id ' + id).join(', ') + '). Retry them when ready.'
        : 'No stuck tasks found.';
    } catch (err) {
      ui.recoverStatus.textContent = 'Recovery failed: ' + err.message;
    } finally {
      renderRunControls();
      refreshAll();
    }
  }

  /* ---------------- Polling ---------------- */

  function refreshAll() {
    return Promise.allSettled([
      loadMemory().catch(() => { renderMemoryUnavailable(); }),
      loadProjects(),
      loadDetail(),
    ]);
  }

  function schedulePoll(delay) {
    window.clearTimeout(state.pollTimer);
    let wait = delay;
    if (wait === undefined) {
      if (state.backendDown) wait = POLL_DOWN_MS;
      else if (isLoopActive() || state.runInFlight) wait = POLL_ACTIVE_MS;
      else wait = POLL_IDLE_MS;
    }
    state.pollTimer = window.setTimeout(tick, wait);
  }

  async function tick() {
    if (state.polling) {
      schedulePoll();
      return;
    }
    state.polling = true;
    try {
      await loadLoop();
      await refreshAll();
    } catch (_) {
      /* banner already reflects network errors */
    } finally {
      state.polling = false;
      schedulePoll();
    }
  }

  /* ---------------- Init ---------------- */

  function bindUi() {
    const ids = {
      ramPill: 'ram-pill', ramPillText: 'ram-pill-text', ramTotal: 'ram-total', ramAvailable: 'ram-available',
      ramUsed: 'ram-used', gauge: 'ram-gauge', legendAvailable: 'ram-legend-available', legendBudget: 'ram-legend-budget',
      ramReason: 'ram-reason', ramMinFree: 'ram-min-free', budgetRange: 'budget-range', budgetOutput: 'budget-output', budgetBytes: 'budget-bytes',
      budgetContext: 'budget-context', budgetTokens: 'budget-tokens',
      banner: 'banner', bannerTitle: 'banner-title', bannerMessage: 'banner-message', bannerAction: 'banner-action',
      projectsRefresh: 'projects-refresh', projectsEmpty: 'projects-empty', projectList: 'project-list',
      loopBanner: 'loop-banner', loopTitle: 'loop-title', loopProject: 'loop-project', loopView: 'loop-view',
      loopStop: 'loop-stop', loopDismiss: 'loop-dismiss', loopProgress: 'loop-progress', loopStats: 'loop-stats',
      loopMessage: 'loop-message',
      detail: 'project-detail', detailTitle: 'detail-title', detailSub: 'detail-sub', detailKind: 'detail-kind',
      detailCreated: 'detail-created', detailCounts: 'detail-counts', detailEmpty: 'detail-empty', detailBody: 'detail-body',
      detailMeta: 'detail-meta', codingNote: 'coding-note', runControls: 'run-controls', runNext: 'run-next',
      maxTasks: 'max-tasks', maxTasksError: 'max-tasks-error', runLoop: 'run-loop', runBusy: 'run-busy',
      runBusyText: 'run-busy-text', runElapsed: 'run-elapsed', runHint: 'run-hint', outcome: 'outcome',
      taskRows: 'task-rows', tasksEmpty: 'tasks-empty',
      form: 'new-project-form', projectName: 'project-name', projectTasks: 'project-tasks', taskCount: 'task-count',
      formError: 'form-error', createBtn: 'create-btn', formStatus: 'form-status',
      recoverBtn: 'recover-btn', recoverStatus: 'recover-status',
      dialog: 'result-dialog', dialogTitle: 'dialog-title', dialogSub: 'dialog-sub', dialogContent: 'dialog-content',
      dialogMeta: 'dialog-meta', dialogCopy: 'dialog-copy', dialogClose: 'dialog-close', dialogX: 'dialog-x',
    };
    for (const key of Object.keys(ids)) ui[key] = $(ids[key]);
  }

  function bindEvents() {
    ui.budgetRange.addEventListener('input', onBudgetInput);
    ui.bannerAction.addEventListener('click', () => {
      if (ui.bannerAction.dataset.action === 'reload') window.location.reload();
      else schedulePoll(0);
    });
    ui.projectsRefresh.addEventListener('click', () => {
      state.projectsKey = '';
      state.tasksKey = '';
      refreshAll();
    });
    ui.runNext.addEventListener('click', runNext);
    ui.runLoop.addEventListener('click', startLoop);
    ui.maxTasks.addEventListener('input', () => {
      if (ui.maxTasks.getAttribute('aria-invalid') === 'true') {
        ui.maxTasks.removeAttribute('aria-invalid');
        ui.maxTasksError.hidden = true;
      }
    });
    ui.maxTasks.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') startLoop();
    });
    ui.loopStop.addEventListener('click', stopLoop);
    ui.loopDismiss.addEventListener('click', () => {
      state.loopDismissed = loopKey(state.loop);
      storage.set(STORAGE.loopDismissed, state.loopDismissed);
      renderLoop();
      if (!ui.detailBody.hidden && !ui.runControls.hidden) ui.runNext.focus();
      else ui.detail.focus();
    });
    ui.loopView.addEventListener('click', () => {
      if (state.loop && state.loop.project_id !== null) selectProject(state.loop.project_id, { scroll: true });
    });
    ui.form.addEventListener('submit', createProject);
    ui.projectTasks.addEventListener('input', () => {
      updateTaskCount();
      if (ui.projectTasks.getAttribute('aria-invalid') === 'true') setFormError('', null);
    });
    ui.projectName.addEventListener('input', () => {
      if (ui.projectName.getAttribute('aria-invalid') === 'true') setFormError('', null);
    });
    ui.recoverBtn.addEventListener('click', recover);
    ui.dialogClose.addEventListener('click', closeDialog);
    ui.dialogX.addEventListener('click', closeDialog);
    ui.dialogCopy.addEventListener('click', copyDialogContent);
    ui.dialog.addEventListener('close', onDialogClosed);
    ui.dialog.addEventListener('click', (ev) => {
      // Click on the backdrop (the dialog element itself, outside its content box) closes it.
      if (ev.target !== ui.dialog) return;
      const r = ui.dialog.getBoundingClientRect();
      const inside = ev.clientX >= r.left && ev.clientX <= r.right && ev.clientY >= r.top && ev.clientY <= r.bottom;
      if (!inside) closeDialog();
    });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) schedulePoll(100);
    });
  }

  function init() {
    bindUi();
    const meta = document.querySelector('meta[name="lowram-token"]');
    state.token = meta ? meta.getAttribute('content') || '' : '';

    const savedPercent = Number(storage.get(STORAGE.percent));
    setPercent(Number.isFinite(savedPercent) && savedPercent > 0 ? savedPercent : 40, false);

    const savedMax = Number(storage.get(STORAGE.maxTasks));
    if (Number.isInteger(savedMax) && savedMax >= 1 && savedMax <= 100) ui.maxTasks.value = String(savedMax);

    state.loopDismissed = storage.get(STORAGE.loopDismissed);

    const savedProject = storage.get(STORAGE.project);
    if (savedProject !== null && savedProject !== '' && Number.isFinite(Number(savedProject))) {
      state.selectedId = Number(savedProject);
    }

    bindEvents();
    updateTaskCount();
    renderProjects();
    renderDetail();

    Promise.allSettled([
      loadMemory().catch(() => { renderMemoryUnavailable(); }),
      loadLoop(),
      loadProjects(),
    ]).then(() => {
      if (state.selectedId !== null) loadDetail().catch(() => {});
      schedulePoll();
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
