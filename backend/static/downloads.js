/* LowRAM AI Studio – download tracking shared by setup, settings and media.
 * Polls GET /api/downloads while someone is interested, computes speed and
 * ETA client-side and renders progress rows that update in place. */
(function () {
  'use strict';

  const LR = window.LR;
  const h = LR.h;
  const fmt = LR.fmt;
  const ACTIVE = new Set(['queued', 'downloading', 'verifying', 'extracting']);
  const STATE_LABEL = {
    queued: [LR.t('Waiting'), 'neutral'],
    downloading: [LR.t('Downloading'), 'accent'],
    verifying: [LR.t('Verifying'), 'accent'],
    extracting: [LR.t('Unpacking'), 'accent'],
    done: [LR.t('Done'), 'ok'],
    failed: [LR.t('Failed'), 'error'],
    cancelled: [LR.t('Cancelled'), 'neutral'],
  };

  const samples = new Map();
  const subscribers = new Set();
  const liveRows = new Set();
  let list = [];
  let loaded = false;

  function record(d) {
    if (!d || !d.id) return;
    const now = Date.now();
    const arr = samples.get(d.id) || [];
    const bytes = LR.isNum(d.bytes_done) ? d.bytes_done : 0;
    if (!arr.length || arr[arr.length - 1].bytes !== bytes || now - arr[arr.length - 1].t > 1500) arr.push({ t: now, bytes });
    while (arr.length > 2 && now - arr[0].t > 8000) arr.shift();
    samples.set(d.id, arr);
  }

  function speed(id) {
    const arr = samples.get(id);
    if (!arr || arr.length < 2) return 0;
    const a = arr[0];
    const b = arr[arr.length - 1];
    const dt = (b.t - a.t) / 1000;
    return dt > 0.5 ? Math.max(0, (b.bytes - a.bytes) / dt) : 0;
  }

  function isActive(d) {
    return Boolean(d) && ACTIVE.has(d.state);
  }

  async function refresh() {
    const data = await LR.get('/api/downloads');
    list = Array.isArray(data.downloads) ? data.downloads : [];
    loaded = true;
    list.forEach(record);
    for (const r of [...liveRows]) {
      if (!r.el.isConnected) {
        if (r.seen) liveRows.delete(r);
        continue;
      }
      r.seen = true;
      const d = list.find((x) => x.id === r.id);
      if (d) r.update(d);
    }
    for (const fn of [...subscribers]) fn(list);
    return list;
  }

  const poller = LR.poller(async () => {
    if (!subscribers.size) return 4000;
    try { await refresh(); } catch (_) { return 3000; }
    return list.some(isActive) ? 800 : 4000;
  }, 4000);

  function subscribe(fn) {
    subscribers.add(fn);
    if (loaded) fn(list);
    poller.start();
    return () => {
      subscribers.delete(fn);
      if (!subscribers.size) poller.stop();
    };
  }

  function summary(d) {
    const parts = [];
    const total = LR.isNum(d.bytes_total) && d.bytes_total > 0 ? d.bytes_total : null;
    const done = LR.isNum(d.bytes_done) ? d.bytes_done : 0;
    if (d.state === 'done') return total ? fmt.bytes(total) + LR.t(' downloaded') : LR.t('Finished');
    if (d.state === 'failed') return d.error ? String(d.error) : LR.t('The download failed.');
    if (d.state === 'cancelled') return LR.t('Cancelled');
    if (d.state === 'queued') return LR.t('Waiting to start…');
    if (d.state === 'verifying') return LR.t('Checking the file…');
    if (d.state === 'extracting') return LR.t('Unpacking…');
    parts.push(total ? fmt.bytes(done) + LR.t(' of ') + fmt.bytes(total) : fmt.bytes(done));
    const bps = speed(d.id);
    if (bps > 0) {
      parts.push(fmt.speed(bps));
      if (total) parts.push(fmt.eta((total - done) / bps));
    }
    return parts.join(' · ');
  }

  function fraction(d) {
    if (d.state === 'done') return 1;
    if (d.state === 'verifying' || d.state === 'extracting' || d.state === 'queued') return null;
    const total = LR.isNum(d.bytes_total) && d.bytes_total > 0 ? d.bytes_total : null;
    return total ? (d.bytes_done || 0) / total : null;
  }

  async function cancel(id, btn) {
    LR.setBusy(btn, true);
    try {
      await LR.post('/api/downloads/' + encodeURIComponent(id) + '/cancel', {});
      LR.toast(LR.t('Download cancelled'), 'info');
    } catch (err) {
      LR.toast(LR.t('Could not cancel: ') + err.message, 'error');
    } finally {
      if (btn && btn.isConnected) LR.setBusy(btn, false);
      poller.now();
    }
  }

  /** A progress row for one download; returns {el, update(d)}. */
  function row(d, opts) {
    const o = opts || {};
    const label = h('span', { class: 'dl-label' });
    const badge = h('span', { class: 'badge' });
    const cancelBtn = LR.button({ icon: 'x', variant: 'ghost', size: 'sm', cls: 'btn-icon', label: LR.t('Cancel download') });
    const bar = LR.progress({ size: 'sm' });
    const meta = h('p', { class: 'dl-meta' });
    const el = h('div', { class: 'dl-row', dataset: { id: d.id } }, [
      h('div', { class: 'dl-head' }, [LR.icon(d.kind && d.kind.includes('runtime') ? 'cpu' : 'download', 'dl-icon'), label, badge, o.noCancel ? null : cancelBtn]),
      bar.el,
      meta,
    ]);
    let current = d;
    cancelBtn.addEventListener('click', () => cancel(current.id, cancelBtn));
    function update(next) {
      current = next;
      label.textContent = o.label || next.label || next.id;
      const st = STATE_LABEL[next.state] || [String(next.state || 'unknown'), 'neutral'];
      badge.textContent = st[0];
      badge.className = 'badge badge-' + st[1];
      cancelBtn.hidden = !isActive(next);
      const f = fraction(next);
      bar.set(f, summary(next));
      bar.el.setAttribute('aria-label', (next.label || LR.t('Download')) + LR.t(' progress'));
      bar.el.classList.toggle('is-error', next.state === 'failed');
      bar.el.classList.toggle('is-done', next.state === 'done');
      meta.textContent = summary(next);
      meta.classList.toggle('is-error', next.state === 'failed');
      el.classList.toggle('is-active', isActive(next));
    }
    update(d);
    const handle = { el, update, id: d.id, seen: false };
    if (o.live !== false) liveRows.add(handle);
    return handle;
  }

  /** Keeps `container` in sync with downloads matching `filter`. */
  function mount(container, filter, opts) {
    const rows = new Map();
    const empty = opts && opts.empty ? opts.empty : null;
    return subscribe((all) => {
      const items = all.filter(filter || (() => true));
      const seen = new Set();
      for (const d of items) {
        seen.add(d.id);
        let r = rows.get(d.id);
        if (!r) {
          r = row(d, opts);
          rows.set(d.id, r);
        }
        r.update(d);
        container.append(r.el);
      }
      for (const [id, r] of rows) {
        if (!seen.has(id)) {
          r.el.remove();
          rows.delete(id);
        }
      }
      if (empty) {
        if (!items.length && !empty.isConnected) container.append(empty);
        if (items.length) empty.remove();
      }
    });
  }

  /** Resolve when download `id` is done; reject when it failed or was cancelled. */
  function wait(id, onUpdate) {
    return new Promise((resolve, reject) => {
      let misses = 0;
      const unsubscribe = subscribe((all) => {
        const d = all.find((x) => x.id === id);
        if (!d) {
          misses++;
          if (misses > 12) {
            unsubscribe();
            resolve(null); // vanished: the caller re-checks what is installed
          }
          return;
        }
        if (onUpdate) onUpdate(d);
        if (d.state === 'done') {
          unsubscribe();
          resolve(d);
        } else if (d.state === 'failed' || d.state === 'cancelled') {
          unsubscribe();
          const err = new Error(d.state === 'cancelled' ? LR.t('The download was cancelled.') : d.error || LR.t('The download failed.'));
          err.download = d;
          reject(err);
        }
      });
      poller.now();
    });
  }

  LR.downloads = {
    get list() { return list; },
    isActive,
    refresh: () => poller.now(),
    subscribe,
    summary,
    fraction,
    row,
    mount,
    wait,
    cancel,
  };
})();
