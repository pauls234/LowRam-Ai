/* LowRAM AI Studio – diagnostics page (checks, self-test, copyable report)
 * and the once-per-start update banner. Both degrade silently when the
 * backend has no such endpoint (404). Check titles, details, hints and the
 * report are server text: textContent only. */
(function () {
  'use strict';

  const LR = window.LR;
  const h = LR.h;
  const icon = LR.icon;
  const t = LR.t;
  const ui = {};
  const st = { data: null, loading: false };

  const STATUS = {
    ok: { icon: 'check', label: 'OK', tone: 'ok' },
    warn: { icon: 'alert', label: 'Warning', tone: 'warn' },
    fail: { icon: 'x', label: 'Problem found', tone: 'error' },
  };

  /* ------------------------------------------------------------ availability probe */

  let probed = false;
  LR.on('status', () => {
    if (probed) return;
    probed = true;
    window.setTimeout(() => { if (LR.diagnosticsAvailable === undefined) load(false); }, 1500);
  });

  async function load(render) {
    if (!LR.mayHave('diagnostics')) {
      LR.diagnosticsAvailable = false;
      if (LR.updateNav) LR.updateNav();
      if (render !== false) renderChecks();
      return;
    }
    st.loading = true;
    try {
      st.data = await LR.get('/api/diagnostics');
      LR.diagnosticsAvailable = true;
      st.error = null;
    } catch (err) {
      if (err.status === 404 || err.status === 405) {
        LR.diagnosticsAvailable = false;
        if (LR.updateNav) LR.updateNav();
      }
      st.error = err;
    } finally {
      st.loading = false;
    }
    if (render !== false) renderChecks();
  }

  /* ------------------------------------------------------------ view */

  function create() {
    const root = h('section', { class: 'view view-page', 'aria-labelledby': 'diag-title' });
    ui.selftestBtn = LR.button({ text: t('Run self-test'), icon: 'play', variant: 'primary', onclick: runSelftest });
    ui.copyBtn = LR.copyButton({ label: t('Copy diagnostics'), cls: 'btn btn-secondary', getText: () => (st.data && typeof st.data.report === 'string' ? st.data.report : '') });
    ui.copyBtn.addEventListener('click', () => { if (st.data && st.data.report) LR.toast(t('Diagnostics copied – paste them into your bug report.')); });
    ui.againBtn = LR.button({ text: t('Check again'), icon: 'retry', variant: 'ghost', onclick: () => refresh() });
    const head = LR.pageHead({
      title: t('Diagnostics'),
      sub: t('Checks this computer and LowRAM AI and helps you fix problems. Nothing is sent anywhere.'),
      actions: [ui.selftestBtn, ui.copyBtn, ui.againBtn],
    });
    head.querySelector('h1').id = 'diag-title';
    ui.selftest = h('div', { class: 'diag-selftest', 'aria-live': 'polite' });
    ui.summary = h('p', { class: 'diag-summary', role: 'status' });
    ui.list = h('ul', { class: 'diag-list' });
    ui.card = h('section', { class: 'card', 'aria-labelledby': 'diag-checks-title' }, [
      h('div', { class: 'card-head' }, [h('h2', { class: 'card-title', id: 'diag-checks-title', text: t('Checks') }), ui.summary]),
      ui.list,
      h('p', { class: 'muted small diag-privacy' }, [icon('shield'), h('span', { text: t('The report contains no personal files or paths – your home folder is shown as ~.') })]),
    ]);
    ui.page = h('div', { class: 'page' }, [head, ui.selftest, ui.card]);
    root.append(ui.page);
    return root;
  }

  async function refresh() {
    ui.list.replaceChildren(h('li', null, LR.skeleton(5)));
    ui.summary.textContent = '';
    LR.setBusy(ui.againBtn, true);
    await load(true);
    LR.setBusy(ui.againBtn, false);
  }

  function renderChecks() {
    if (!ui.list) return;
    if (LR.diagnosticsAvailable === false) {
      ui.page.replaceChildren(ui.page.firstElementChild, LR.unavailable(t('Diagnostics')));
      return;
    }
    if (st.error && !st.data) {
      ui.list.replaceChildren(h('li', null, LR.callout({
        tone: 'error', title: t('Could not run the checks'), text: st.error.message,
        actions: [LR.button({ text: t('Try again'), icon: 'retry', size: 'sm', onclick: () => refresh() })],
      })));
      return;
    }
    const checks = st.data && Array.isArray(st.data.checks) ? st.data.checks : [];
    const warn = checks.filter((c) => c.status === 'warn').length;
    const fail = checks.filter((c) => c.status === 'fail').length;
    ui.summary.className = 'diag-summary is-' + (fail ? 'error' : warn ? 'warn' : 'ok');
    ui.summary.textContent = fail ? (fail === 1 ? t('1 problem') : t('{n} problems', { n: fail })) : warn ? (warn === 1 ? t('1 warning') : t('{n} warnings', { n: warn })) : t('All checks passed');
    ui.copyBtn.disabled = !(st.data && st.data.report);
    // Problems first, then warnings, then the rest in server order.
    const order = { fail: 0, warn: 1, ok: 2 };
    const sorted = checks.map((c, i) => [c, i]).sort((a, b) => ((order[a[0].status] ?? 3) - (order[b[0].status] ?? 3)) || a[1] - b[1]).map((x) => x[0]);
    ui.list.replaceChildren(...sorted.map((c) => {
      const s = STATUS[c.status] || STATUS.warn;
      return h('li', { class: 'diag-item is-' + s.tone }, [
        h('span', { class: 'diag-icon', title: t(s.label) }, [icon(s.icon), h('span', { class: 'visually-hidden', text: t(s.label) + ': ' })]),
        h('div', { class: 'diag-main' }, [
          h('p', { class: 'diag-title', text: LR.str(c.title, LR.str(c.id)) }),
          c.detail ? h('p', { class: 'diag-detail', text: LR.str(c.detail) }) : null,
          c.hint && c.status !== 'ok' ? h('p', { class: 'diag-hint' }, [icon('info'), h('span', { text: LR.str(c.hint) })]) : null,
        ]),
      ]);
    }));
  }

  async function runSelftest() {
    LR.setBusy(ui.selftestBtn, true);
    ui.selftest.replaceChildren(h('div', { class: 'card diag-running' }, [LR.spinner(), h('span', { text: t('Running the self-test…') })]));
    try {
      const r = await LR.post('/api/diagnostics/selftest', {});
      if (r.ok) {
        const tps = LR.isNum(r.tokens_per_s) ? (r.tokens_per_s < 10 ? r.tokens_per_s.toFixed(1) : String(Math.round(r.tokens_per_s))) : '—';
        ui.selftest.replaceChildren(LR.callout({ tone: 'ok', title: t('Self-test passed: {s} · {tps} tokens/s', { s: LR.fmt.duration(r.seconds) || '—', tps }), text: t('Asks the running model a tiny question and measures the speed.') }));
      } else {
        ui.selftest.replaceChildren(LR.callout({ tone: 'error', role: 'alert', title: t('Self-test failed'), text: LR.str(r.error, t('Unknown error.')) }));
      }
    } catch (err) {
      ui.selftest.replaceChildren(LR.callout({ tone: 'error', role: 'alert', title: t('Self-test failed'), text: err.message }));
    } finally {
      LR.setBusy(ui.selftestBtn, false);
    }
  }

  LR.registerView('diagnostics', {
    title: t('Diagnostics'),
    create,
    show() {
      if (st.data && !st.loading) renderChecks();
      refresh();
    },
    hide() {},
  });

  /* ------------------------------------------------------------ update banner (once per start) */

  let updateChecked = false;
  LR.on('settings', (settings) => {
    if (updateChecked) return;
    updateChecked = true;
    const uiS = (settings && settings.ui) || {};
    if (uiS.check_updates === false || !LR.mayHave('diagnostics')) return;
    checkUpdate();
  });

  async function checkUpdate() {
    let data;
    try {
      data = await LR.get('/api/update');
    } catch (_) {
      return; // 404 / offline / private repo: stay silent
    }
    if (!data || !data.available || typeof data.latest !== 'string') return;
    const url = typeof data.url === 'string' && /^https:\/\/github\.com\//.test(data.url) ? data.url : null;
    const dismissedKey = 'lowram.updateDismissed';
    if (LR.store.get(dismissedKey) === data.latest) return;
    const actions = [];
    if (url) {
      actions.push(h('a', { class: 'btn btn-primary btn-sm', href: url, target: '_blank', rel: 'noopener noreferrer' }, [icon('external'), h('span', { text: t('View release') })]));
    }
    actions.push(LR.button({ text: t('Dismiss'), size: 'sm', variant: 'ghost', onclick: () => {
      LR.store.set(dismissedKey, data.latest);
      if (LR.banner) LR.banner('update', null);
    } }));
    if (LR.banner) {
      LR.banner('update', {
        tone: 'accent',
        icon: 'sparkle',
        title: t('New version {v} available', { v: data.latest }),
        text: url ? t('Download it on GitHub when it suits you.') : '',
        actions,
      });
    }
  }
})();
