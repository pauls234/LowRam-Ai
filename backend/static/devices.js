/* LowRAM AI Studio – Devices: let paired computers on the local network help
 * with task lists, or join another computer as a helper. */
(function () {
  'use strict';

  const LR = window.LR;
  const h = LR.h;
  const icon = LR.icon;
  const fmt = LR.fmt;
  const ui = {};
  const st = { cluster: null, loaded: false, pairingModal: null, sig: '' };

  const poller = LR.poller(async () => {
    try { await load(); } catch (_) { return 5000; }
    return st.pairingModal || (st.cluster && st.cluster.worker && st.cluster.worker.state === 'pairing') ? 1500 : 4000;
  }, 4000);

  async function load() {
    const data = await LR.get('/api/cluster');
    st.cluster = data.cluster || data;
    st.loaded = true;
    const sig = JSON.stringify(st.cluster);
    if (sig !== st.sig) {
      st.sig = sig;
      render();
    }
    if (st.pairingModal) st.pairingModal.update(st.cluster);
  }

  /* ------------------------------------------------------------ host card */

  function deviceItem(d) {
    const tone = d.status === 'online' ? 'ok' : d.status === 'busy' ? 'busy' : 'idle';
    const facts = [d.platform, LR.isNum(d.ram_total_bytes) ? fmt.gb(d.ram_total_bytes) + ' RAM' : '', d.model ? LR.modelName(d.model) : ''].filter(Boolean).join(' · ');
    const activity = d.current_task_id !== null && d.current_task_id !== undefined
      ? 'Working on task ' + d.current_task_id
      : d.status === 'offline' ? 'Last seen ' + (fmt.relative(d.last_seen) || 'a while ago') : 'Ready to help';
    return h('li', { class: 'device' }, [
      h('span', { class: 'device-icon', 'aria-hidden': 'true' }, icon('laptop')),
      h('div', { class: 'device-main' }, [
        h('p', { class: 'device-name' }, [h('span', { class: 'dot dot-' + tone + (d.status === 'busy' ? ' is-pulsing' : ''), 'aria-hidden': 'true' }), h('span', { text: LR.str(d.name, 'Unnamed device') }), h('span', { class: 'visually-hidden', text: ' – ' + LR.str(d.status) })]),
        facts ? h('p', { class: 'muted small', text: facts }) : null,
        h('p', { class: 'device-activity', text: activity + ' · ' + fmt.plural(d.tasks_done || 0, 'task') + ' done' }),
      ]),
      LR.button({ text: 'Revoke', size: 'sm', variant: 'ghost', label: 'Revoke ' + LR.str(d.name, 'device'), onclick: (ev) => revoke(d, ev.currentTarget) }),
    ]);
  }

  function renderHost() {
    const c = st.cluster || {};
    const enabled = Boolean(c.enabled);
    const sw = LR.switchRow({
      label: 'Allow other devices on my network',
      description: 'Paired computers can pick up tasks from your queue. Connections are encrypted and stay on your local network.',
      checked: enabled,
      onChange: (on, input) => toggle(on, input),
    });
    const out = [sw.el];
    if (c.error) out.push(LR.callout({ tone: 'error', title: 'The device server has a problem', text: LR.str(c.error), role: 'alert' }));
    if (enabled) {
      const listen = c.listening;
      if (listen && Array.isArray(listen.addresses) && listen.addresses.length) {
        out.push(h('dl', { class: 'facts' }, [
          h('div', null, [h('dt', { text: 'Reachable at' }), h('dd', null, h('ul', { class: 'addr-list' }, listen.addresses.map((a) => h('li', null, h('code', { text: (String(a).includes(':') && !String(a).startsWith('[') ? '[' + a + ']' : a) + (listen.port ? ':' + listen.port : '') })))))]),
          c.fingerprint ? h('div', null, [h('dt', { text: 'Fingerprint' }), h('dd', { class: 'fingerprint' }, [h('code', { text: LR.str(c.fingerprint) }), LR.copyButton({ getText: () => LR.str(c.fingerprint), iconOnly: true, cls: 'icon-btn', ariaLabel: 'Copy fingerprint' })])]) : null,
        ]));
      } else if (!c.error) {
        out.push(h('p', { class: 'muted small' }, [LR.spinner(), ' Starting the device server…']));
      }
      const devices = Array.isArray(c.devices) ? c.devices : [];
      out.push(h('div', { class: 'devices-head' }, [
        h('h3', { class: 'section-label', text: 'Paired devices' }),
        LR.button({ text: 'Add device', icon: 'plus', variant: 'primary', size: 'sm', onclick: openPairing }),
      ]));
      out.push(devices.length
        ? h('ul', { class: 'device-list' }, devices.map(deviceItem))
        : LR.empty({ icon: 'devices', title: 'No helper devices yet', text: 'Add an older laptop or a second computer – it takes one code.' }));
    }
    ui.host.body.replaceChildren(...out);
  }

  async function toggle(on, input) {
    input.disabled = true;
    try {
      const data = await LR.post('/api/cluster/' + (on ? 'enable' : 'disable'), {});
      if (data.cluster) {
        st.cluster = data.cluster;
        st.sig = JSON.stringify(data.cluster);
      }
      LR.toast(on ? 'Other devices can now join' : 'Device sharing turned off', 'info');
    } catch (err) {
      input.checked = !on;
      LR.toast('Could not change this: ' + err.message, 'error');
    } finally {
      input.disabled = false;
      await load().catch(() => {});
      render();
    }
  }

  async function revoke(d, btn) {
    const ok = await LR.confirm({ title: 'Revoke ' + LR.str(d.name, 'this device') + '?', message: 'It can no longer connect or take tasks. You can pair it again later with a new code.', confirmText: 'Revoke', danger: true });
    if (!ok) return;
    LR.setBusy(btn, true);
    try {
      await LR.post('/api/cluster/devices/' + encodeURIComponent(d.id) + '/revoke', {});
      LR.toast('Device revoked');
    } catch (err) {
      LR.toast('Could not revoke: ' + err.message, 'error');
      if (btn.isConnected) LR.setBusy(btn, false);
    } finally {
      poller.now();
    }
  }

  /* ------------------------------------------------------------ pairing */

  async function openPairing() {
    const codeEl = h('p', { class: 'pair-code', 'aria-live': 'polite' }, LR.spinner());
    const countdown = h('p', { class: 'pair-expiry' });
    const cmd = h('code', { class: 'pair-cmd' });
    const copyCode = LR.copyButton({ getText: () => current || '', label: 'Copy code', cls: 'btn btn-secondary btn-sm' });
    const copyCmd = LR.copyButton({ getText: () => cmd.textContent, iconOnly: true, cls: 'icon-btn', ariaLabel: 'Copy command' });
    const status = h('div', { class: 'pair-status', 'aria-live': 'polite' });
    let current = null;
    let expires = null;
    let timer = 0;
    const startCount = Array.isArray(st.cluster && st.cluster.devices) ? st.cluster.devices.length : 0;
    const body = [
      h('div', { class: 'pair-box' }, [codeEl, h('div', { class: 'pair-row' }, [countdown, copyCode])]),
      h('ol', { class: 'pair-steps' }, [
        h('li', null, ['On the other computer, open ', h('strong', { text: 'LowRAM AI → Devices' }), ' and use ', h('strong', { text: 'Join another computer' }), '.']),
        h('li', { text: 'Enter this code and give that computer a name.' }),
        h('li', null, ['Or run this in a terminal there:', h('span', { class: 'cmd-line' }, [cmd, copyCmd])]),
      ]),
      status,
    ];
    const newCode = LR.button({ text: 'New code', icon: 'retry', variant: 'secondary', onclick: () => request() });
    const m = LR.modal({ title: 'Add a device', sub: 'The code works once and expires after 10 minutes.', body, actions: [newCode, LR.button({ text: 'Done', variant: 'primary', onclick: () => m.close() })], onClose: () => { window.clearInterval(timer); st.pairingModal = null; } });

    function tick() {
      if (!expires) { countdown.textContent = ''; return; }
      const left = expires.getTime() - Date.now();
      if (left <= 0) {
        countdown.textContent = 'This code has expired.';
        countdown.classList.add('is-expired');
        codeEl.classList.add('is-expired');
        return;
      }
      countdown.classList.remove('is-expired');
      codeEl.classList.remove('is-expired');
      countdown.replaceChildren(icon('clock'), h('span', { text: 'Expires in ' + fmt.clock(left) }));
    }

    async function request() {
      LR.setBusy(newCode, true);
      try {
        const data = await LR.post('/api/cluster/pairing', {});
        const p = data.pairing || {};
        current = LR.str(p.code);
        expires = fmt.parseTime(p.expires_at);
        codeEl.textContent = current;
        codeEl.setAttribute('aria-label', 'Pairing code ' + current.split('').join(' '));
        cmd.textContent = 'python3 lowram.py worker --join ' + current;
        tick();
      } catch (err) {
        codeEl.replaceChildren(h('span', { class: 'pair-error', text: 'Could not create a code' }));
        status.replaceChildren(LR.callout({ tone: 'error', text: err.message }));
      } finally {
        LR.setBusy(newCode, false);
      }
    }

    st.pairingModal = {
      update(c) {
        const devices = Array.isArray(c.devices) ? c.devices : [];
        if (devices.length > startCount && !status.dataset.done) {
          status.dataset.done = '1';
          const newest = devices[devices.length - 1];
          status.replaceChildren(LR.callout({ tone: 'ok', title: LR.str(newest.name, 'A new device') + ' joined', text: 'It will start helping with shared task lists.' }));
          LR.announce(LR.str(newest.name, 'A new device') + ' joined');
        }
      },
    };
    timer = window.setInterval(tick, 1000);
    await request();
    poller.now();
  }

  /* ------------------------------------------------------------ worker card */

  function renderWorker() {
    const c = st.cluster || {};
    const w = c.worker || { state: 'idle' };
    const out = [];
    const stateText = { idle: 'Not helping anyone', pairing: 'Connecting…', working: 'Helping', error: 'Problem', stopped: 'Stopped' };
    if (w.state === 'working' || w.state === 'pairing') {
      const task = w.current_task;
      const taskText = task === null || task === undefined || task === '' ? 'Waiting for a task' : typeof task === 'object' ? 'Working on ' + LR.str(task.title || ('task ' + task.id)) : 'Working on task ' + task;
      out.push(h('div', { class: 'worker-state' }, [
        h('span', { class: 'dot dot-' + (w.state === 'working' ? 'busy' : 'warn') + ' is-pulsing', 'aria-hidden': 'true' }),
        h('div', null, [
          h('p', { class: 'strong', text: w.state === 'pairing' ? 'Connecting to ' + LR.str(w.host, 'the host') + '…' : 'Helping ' + LR.str(w.host, 'another computer') }),
          h('p', { class: 'muted small', text: w.state === 'working' ? taskText + ' · ' + fmt.plural(w.tasks_done || 0, 'task') + ' done' : 'Checking the code…' }),
        ]),
        LR.button({ text: 'Leave', icon: 'power', size: 'sm', variant: 'secondary', onclick: (ev) => leave(ev.currentTarget) }),
      ]));
      ui.worker.body.replaceChildren(...out);
      return;
    }
    if (w.state === 'error' && w.error) out.push(LR.callout({ tone: 'error', title: 'Could not help', text: LR.str(w.error), role: 'alert' }));
    else if (w.state === 'stopped' && LR.isNum(w.tasks_done) && w.tasks_done) out.push(h('p', { class: 'muted small', text: 'Last session: ' + fmt.plural(w.tasks_done, 'task') + ' done for ' + LR.str(w.host, 'another computer') + '.' }));
    else out.push(h('p', { class: 'muted small', text: stateText[w.state] ? stateText[w.state] + '.' : '' }));
    const code = h('input', { class: 'input mono', type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder: 'Code from the other computer', maxlength: '512' });
    const name = h('input', { class: 'input', type: 'text', autocomplete: 'off', maxlength: '80', placeholder: 'e.g. Old MacBook' });
    const settingsName = LR.state.settings && LR.state.settings.cluster ? LR.state.settings.cluster.device_name : '';
    if (settingsName) name.value = settingsName;
    const fCode = LR.field({ label: 'Pairing code', control: code });
    const fName = LR.field({ label: 'Name for this computer', control: name, optional: true });
    const join = LR.button({ text: 'Join', icon: 'link', variant: 'primary', type: 'submit' });
    const form = h('form', { class: 'form join-form', novalidate: true }, [h('div', { class: 'form-row' }, [fCode.el, fName.el]), h('div', { class: 'form-actions' }, [join])]);
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      fCode.setError('');
      if (!code.value.trim()) {
        fCode.setError('Enter the code shown on the other computer.');
        code.focus();
        return;
      }
      LR.setBusy(join, true, 'Joining…');
      try {
        const data = await LR.post('/api/cluster/join', { code: code.value.trim(), name: name.value.trim() || undefined });
        if (data.worker && st.cluster) st.cluster.worker = data.worker;
        LR.toast('Joining…', 'info');
        poller.now();
      } catch (err) {
        fCode.setError(err.message);
        LR.setBusy(join, false);
      }
    });
    out.push(form);
    ui.worker.body.replaceChildren(...out);
  }

  async function leave(btn) {
    LR.setBusy(btn, true, 'Leaving…');
    try {
      await LR.post('/api/cluster/leave', {});
      LR.toast('This computer stopped helping', 'info');
    } catch (err) {
      LR.toast('Could not leave: ' + err.message, 'error');
      if (btn.isConnected) LR.setBusy(btn, false);
    } finally {
      poller.now();
    }
  }

  /* ------------------------------------------------------------ view */

  function card(title, sub) {
    const id = LR.uid('dev');
    const body = h('div', { class: 'card-body' });
    return { el: h('section', { class: 'card', 'aria-labelledby': id }, [h('div', { class: 'card-head' }, h('div', null, [h('h2', { class: 'card-title', id, text: title }), sub ? h('p', { class: 'card-sub', text: sub }) : null])), body]), body };
  }

  function render() {
    if (!ui.host) return;
    if (!st.loaded) {
      ui.host.body.replaceChildren(LR.skeleton(3));
      ui.worker.body.replaceChildren(LR.skeleton(2));
      return;
    }
    const focusedLabel = document.activeElement && ui.page.contains(document.activeElement) ? document.activeElement.getAttribute('aria-label') || document.activeElement.textContent : null;
    renderHost();
    renderWorker();
    if (focusedLabel) {
      const again = [...ui.page.querySelectorAll('button, input')].find((el) => (el.getAttribute('aria-label') || el.textContent) === focusedLabel);
      if (again) again.focus();
    }
  }

  function create() {
    const root = h('section', { class: 'view view-page', 'aria-labelledby': 'devices-title' });
    ui.page = h('div', { class: 'page' });
    root.append(ui.page);
    ui.host = card('This computer', 'Share your task queue with other computers you own.');
    ui.worker = card('Join another computer', 'Lend this computer’s memory to a LowRAM AI on your network.');
    return root;
  }

  LR.registerView('devices', {
    title: 'Devices',
    create,
    show() {
      const head = LR.pageHead({ title: 'Devices', sub: 'Spread long task lists across the computers you already have. Everything stays on your own network.' });
      head.querySelector('h1').id = 'devices-title';
      if (!LR.has('cluster')) {
        ui.page.replaceChildren(head, LR.unavailable('Devices'));
        return;
      }
      ui.page.replaceChildren(head, ui.host.el, ui.worker.el, h('p', { class: 'page-note' }, [icon('shield'), h('span', { text: 'Devices pair with a one-time code and an encrypted, pinned connection. Revoke a device at any time.' })]));
      st.sig = '';
      render();
      poller.start();
    },
    hide() {
      poller.stop();
    },
  });
})();
