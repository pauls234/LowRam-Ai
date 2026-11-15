/* LowRAM AI Studio – Devices: let paired computers on the local network help
 * with task lists, or join another computer as a helper. */
(function () {
  'use strict';

  const LR = window.LR;
  const h = LR.h;
  const icon = LR.icon;
  const fmt = LR.fmt;
  const ui = {};
  const st = { cluster: null, loaded: false, pairingModal: null, sig: '', nearby: null, nearbySig: '' };
  const easyPairing = () => Boolean(st.cluster && Array.isArray(st.cluster.requests));

  const nearbyPoller = LR.poller(async () => {
    if (!easyPairing()) return 3000;
    try {
      const data = await LR.get('/api/cluster/nearby');
      st.nearby = Array.isArray(data.hosts) ? data.hosts : [];
      st.nearbyError = data.error ? LR.str(data.error) : null;
      renderNearby();
    } catch (_) { /* keep the last list */ }
    return 3000;
  }, 3000);

  /** Allow / deny a pairing request (also used by the global banner in app.js). */
  async function answerRequest(req, allow, btn) {
    if (btn) LR.setBusy(btn, true);
    try {
      await LR.post('/api/cluster/requests/' + encodeURIComponent(req.id) + '/' + (allow ? 'approve' : 'deny'), {});
      LR.toast(allow ? LR.str(req.name, LR.t('The device')) + ' can now help' : LR.t('Request declined'), allow ? null : 'info');
    } catch (err) {
      LR.toast(LR.t('Could not answer the request: ') + err.message, 'error');
      if (btn && btn.isConnected) LR.setBusy(btn, false);
    } finally {
      poller.now();
      LR.refreshStatus();
    }
  }
  LR.cluster = { answerRequest };

  function requestCard(r) {
    const facts = [r.platform, LR.isNum(r.ram_total_bytes) ? fmt.gb(r.ram_total_bytes) + ' RAM' : '', r.address].filter(Boolean).join(' · ');
    return h('div', { class: 'request-card', role: 'group', 'aria-label': LR.t('Pairing request from ') + LR.str(r.name, LR.t('a device')) }, [
      h('div', { class: 'request-head' }, [
        h('span', { class: 'device-icon', 'aria-hidden': 'true' }, icon('laptop')),
        h('div', { class: 'device-main' }, [
          h('p', { class: 'strong', text: LR.str(r.name, LR.t('A computer')) + LR.t(' wants to help') }),
          facts ? h('p', { class: 'muted small', text: facts }) : null,
        ]),
      ]),
      r.verification_code
        ? h('p', { class: 'verify-code', 'aria-label': LR.t('Verification number ') + LR.str(r.verification_code), text: LR.str(r.verification_code) })
        : h('p', { class: 'verify-code is-pending' }, [LR.spinner(), h('span', { class: 'small muted', text: LR.t(' Waiting for the number…') })]),
      h('p', { class: 'verify-help', text: LR.t('Only allow it if the other computer shows exactly the same number.') }),
      h('div', { class: 'row-actions request-actions' }, [
        LR.button({ text: LR.t('Deny'), variant: 'secondary', onclick: (ev) => answerRequest(r, false, ev.currentTarget) }),
        LR.button({ text: LR.t('Allow'), icon: 'check', variant: 'primary', disabled: !r.verification_code, title: r.verification_code ? null : LR.t('Wait until the number is shown'), onclick: (ev) => answerRequest(r, true, ev.currentTarget) }),
      ]),
    ]);
  }

  function renderNearby() {
    if (!ui.nearby) return;
    const hosts = st.nearby;
    const sig = JSON.stringify([hosts, st.nearbyError]);
    if (sig === st.nearbySig && ui.nearby.childElementCount) return;
    st.nearbySig = sig;
    if (hosts === null) {
      ui.nearby.replaceChildren(h('p', { class: 'muted small' }, [LR.spinner(), LR.t(' Looking for computers nearby…')]));
      return;
    }
    const errorNote = st.nearbyError ? LR.callout({ tone: 'warn', title: LR.t('Searching for computers is not working right now'), text: st.nearbyError + LR.t(' You can still join with a code.') }) : null;
    if (!hosts.length) {
      ui.nearby.replaceChildren(errorNote || '', h('div', { class: 'nearby-empty' }, [
        h('span', { class: 'scan', 'aria-hidden': 'true' }, icon('wifi')),
        h('p', { class: 'strong', text: LR.t('No computers found yet') }),
        h('p', { class: 'muted small', text: LR.t('Open LowRAM AI on the other computer and turn on Devices there.') }),
        h('ul', { class: 'hint-list' }, [
          h('li', { text: LR.t('Both computers need to be on the same Wi-Fi or network.') }),
          h('li', { text: LR.t('If it still does not appear, allow LowRAM AI in the firewall – or use a code instead.') }),
        ]),
      ]));
      return;
    }
    ui.nearby.replaceChildren(errorNote || '', h('ul', { class: 'nearby-list', 'aria-label': LR.t('Nearby computers') }, hosts.map((host) => h('li', { class: 'nearby-host' }, [
      h('span', { class: 'device-icon', 'aria-hidden': 'true' }, icon('laptop')),
      h('div', { class: 'device-main' }, [
        h('p', { class: 'strong', text: LR.str(host.name, LR.t('LowRAM AI computer')) }),
        h('p', { class: 'muted small', text: [Array.isArray(host.addresses) ? host.addresses[0] : '', host.fingerprint_short ? 'ID ' + host.fingerprint_short : ''].filter(Boolean).join(' · ') }),
      ]),
      LR.button({ text: LR.t('Join'), icon: 'link', variant: 'primary', cls: 'btn-join', label: LR.t('Join ') + LR.str(host.name, LR.t('this computer')), onclick: (ev) => joinHost(host, ev.currentTarget) }),
    ]))));
  }

  async function joinHost(host, btn) {
    LR.setBusy(btn, true, LR.t('Joining…'));
    try {
      const data = await LR.post('/api/cluster/join', { host_id: host.id });
      if (data.worker && st.cluster) st.cluster.worker = data.worker;
      st.fastUntil = Date.now() + 15000;
      st.sig = '';
      render();
      const verify = ui.worker.el.querySelector('.verify');
      if (verify) verify.scrollIntoView({ behavior: LR.reducedMotion() ? 'auto' : 'smooth', block: 'center' });
    } catch (err) {
      LR.toast(LR.t('Could not join: ') + err.message, 'error');
      if (btn.isConnected) LR.setBusy(btn, false);
      const fallback = ui.worker.el.querySelector('.code-fallback');
      if (fallback) fallback.open = true;
    } finally {
      poller.now();
    }
  }

  const poller = LR.poller(async () => {
    try { await load(); } catch (_) { return 4000; }
    const w = st.cluster && st.cluster.worker;
    const urgent = st.pairingModal || (w && (w.state === 'pairing' || w.state === 'awaiting_approval')) || Date.now() < (st.fastUntil || 0) ||
      (st.cluster && st.cluster.requests && st.cluster.requests.length);
    return urgent ? 1000 : 2000; // devices and the helper state refresh by themselves while this page is open
  }, 2000);

  /** A helper is connected once the host knows it (host / device id) – whatever the exact state. */
  const isConnected = (w) => Boolean(w) && w.state !== 'stopped' && w.state !== 'awaiting_approval' && (Boolean(w.host) || (w.device_id !== null && w.device_id !== undefined));

  async function load() {
    const data = await LR.get('/api/cluster');
    st.cluster = data.cluster || data;
    st.loaded = true;
    const w = st.cluster && st.cluster.worker;
    if (w && st.lastWorkerState === 'awaiting_approval' && w.state !== 'awaiting_approval') st.fastUntil = Date.now() + 10000;
    st.lastWorkerState = w ? w.state : null;
    const sig = JSON.stringify(st.cluster);
    if (sig !== st.sig) {
      st.sig = sig;
      render(true);
    }
    if (st.pairingModal) st.pairingModal.update(st.cluster);
  }

  /* ------------------------------------------------------------ host card */

  function deviceItem(d) {
    const tone = d.status === 'online' ? 'ok' : d.status === 'busy' ? 'busy' : 'idle';
    const facts = [d.platform, LR.isNum(d.ram_total_bytes) ? fmt.gb(d.ram_total_bytes) + ' RAM' : '', d.model ? LR.modelName(d.model) : ''].filter(Boolean).join(' · ');
    const activity = d.current_task_id !== null && d.current_task_id !== undefined
      ? LR.t('Working on task ') + d.current_task_id
      : d.status === 'offline' ? LR.t('Last seen ') + (fmt.relative(d.last_seen) || LR.t('a while ago')) : LR.t('Ready to help');
    return h('li', { class: 'device' }, [
      h('span', { class: 'device-icon', 'aria-hidden': 'true' }, icon('laptop')),
      h('div', { class: 'device-main' }, [
        h('p', { class: 'device-name' }, [h('span', { class: 'dot dot-' + tone + (d.status === 'busy' ? ' is-pulsing' : ''), 'aria-hidden': 'true' }), h('span', { text: LR.str(d.name, LR.t('Unnamed device')) }), h('span', { class: 'visually-hidden', text: ' – ' + LR.str(d.status) })]),
        facts ? h('p', { class: 'muted small', text: facts }) : null,
        h('p', { class: 'device-activity', text: activity + ' · ' + fmt.plural(d.tasks_done || 0, 'task') + LR.t(' done') }),
      ]),
      LR.button({ text: LR.t('Revoke'), size: 'sm', variant: 'ghost', label: LR.t('Revoke ') + LR.str(d.name, 'device'), onclick: (ev) => revoke(d, ev.currentTarget) }),
    ]);
  }

  function renderHost() {
    const c = st.cluster || {};
    const enabled = Boolean(c.enabled);
    const sw = LR.switchRow({
      label: LR.t('Allow other devices on my network'),
      description: LR.t('Paired computers can pick up tasks from your queue. Connections are encrypted and stay on your local network.'),
      checked: enabled,
      onChange: (on, input) => toggle(on, input),
    });
    const out = [];
    const requests = Array.isArray(c.requests) ? c.requests : [];
    if (requests.length) out.push(h('div', { class: 'requests', 'aria-live': 'polite' }, requests.map(requestCard)));
    out.push(sw.el);
    if (enabled && 'discoverable' in c) {
      out.push(LR.switchRow({
        label: LR.t('Show this computer to nearby devices'),
        description: LR.t('Lets other computers find it in their list. When off, they can still join with a code.'),
        checked: c.discoverable !== false,
        onChange: async (on, input) => {
          if (!(await LR.saveSetting('cluster', { discoverable: on }, on ? LR.t('Visible to nearby computers') : LR.t('Hidden from nearby computers')))) input.checked = !on;
          poller.now();
        },
      }).el);
    }
    if (c.host_name && enabled) out.push(h('p', { class: 'muted small' }, [LR.t('Other computers see this one as '), h('strong', { text: LR.str(c.host_name) }), '.']));
    if (c.pairing_error) out.push(LR.callout({ tone: 'warn', title: LR.t('Pairing problem'), text: LR.str(c.pairing_error) }));
    if (c.error) out.push(LR.callout({ tone: 'error', title: LR.t('The device server has a problem'), text: LR.str(c.error), role: 'alert' }));
    if (enabled) {
      const listen = c.listening;
      if (listen && Array.isArray(listen.addresses) && listen.addresses.length) {
        out.push(h('dl', { class: 'facts' }, [
          h('div', null, [h('dt', { text: LR.t('Reachable at') }), h('dd', null, h('ul', { class: 'addr-list' }, listen.addresses.map((a) => h('li', null, h('code', { text: (String(a).includes(':') && !String(a).startsWith('[') ? '[' + a + ']' : a) + (listen.port ? ':' + listen.port : '') })))))]),
          c.fingerprint ? h('div', null, [h('dt', { text: LR.t('Fingerprint') }), h('dd', { class: 'fingerprint' }, [h('code', { text: LR.str(c.fingerprint) }), LR.copyButton({ getText: () => LR.str(c.fingerprint), iconOnly: true, cls: 'icon-btn', ariaLabel: LR.t('Copy fingerprint') })])]) : null,
        ]));
      } else if (!c.error) {
        out.push(h('p', { class: 'muted small' }, [LR.spinner(), LR.t(' Starting the device server…')]));
      }
      const devices = Array.isArray(c.devices) ? c.devices : [];
      out.push(h('div', { class: 'devices-head' }, [
        h('h3', { class: 'section-label', text: LR.t('Paired devices') }),
        LR.button({ text: LR.t('Add device'), icon: 'plus', variant: 'primary', size: 'sm', onclick: openPairing }),
      ]));
      out.push(devices.length
        ? h('ul', { class: 'device-list' }, devices.map(deviceItem))
        : LR.empty({ icon: 'devices', title: LR.t('No helper devices yet'), text: LR.t('Add an older laptop or a second computer – it takes one code.') }));
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
      LR.toast(on ? LR.t('Other devices can now join') : LR.t('Device sharing turned off'), 'info');
    } catch (err) {
      input.checked = !on;
      LR.toast(LR.t('Could not change this: ') + err.message, 'error');
    } finally {
      input.disabled = false;
      await load().catch(() => {});
      render();
    }
  }

  async function revoke(d, btn) {
    const ok = await LR.confirm({ title: LR.t('Revoke ') + LR.str(d.name, LR.t('this device')) + '?', message: LR.t('It can no longer connect or take tasks. You can pair it again later with a new code.'), confirmText: LR.t('Revoke'), danger: true });
    if (!ok) return;
    LR.setBusy(btn, true);
    try {
      await LR.post('/api/cluster/devices/' + encodeURIComponent(d.id) + '/revoke', {});
      LR.toast(LR.t('Device revoked'));
    } catch (err) {
      LR.toast(LR.t('Could not revoke: ') + err.message, 'error');
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
    const copyCode = LR.copyButton({ getText: () => current || '', label: LR.t('Copy code'), cls: 'btn btn-secondary btn-sm' });
    const copyCmd = LR.copyButton({ getText: () => cmd.textContent, iconOnly: true, cls: 'icon-btn', ariaLabel: LR.t('Copy command') });
    const status = h('div', { class: 'pair-status', 'aria-live': 'polite' });
    let current = null;
    let expires = null;
    let timer = 0;
    const startCount = Array.isArray(st.cluster && st.cluster.devices) ? st.cluster.devices.length : 0;
    const body = [
      h('div', { class: 'pair-box' }, [codeEl, h('div', { class: 'pair-row' }, [countdown, copyCode])]),
      h('ol', { class: 'pair-steps' }, [
        h('li', null, [LR.t('On the other computer, open '), h('strong', { text: LR.t('LowRAM AI → Devices') }), LR.t(' and use '), h('strong', { text: LR.t('Join another computer') }), '.']),
        h('li', { text: LR.t('Enter this code and give that computer a name.') }),
        h('li', null, [LR.t('Or run this in a terminal there:'), h('span', { class: 'cmd-line' }, [cmd, copyCmd])]),
      ]),
      status,
    ];
    const newCode = LR.button({ text: LR.t('New code'), icon: 'retry', variant: 'secondary', onclick: () => request() });
    const m = LR.modal({ title: 'Add a device', sub: LR.t('The code works once and expires after 10 minutes.'), body, actions: [newCode, LR.button({ text: LR.t('Done'), variant: 'primary', onclick: () => m.close() })], onClose: () => { window.clearInterval(timer); st.pairingModal = null; } });

    function tick() {
      if (!expires) { countdown.textContent = ''; return; }
      const left = expires.getTime() - Date.now();
      if (left <= 0) {
        countdown.textContent = LR.t('This code has expired.');
        countdown.classList.add('is-expired');
        codeEl.classList.add('is-expired');
        return;
      }
      countdown.classList.remove('is-expired');
      codeEl.classList.remove('is-expired');
      countdown.replaceChildren(icon('clock'), h('span', { text: LR.t('Expires in ') + fmt.clock(left) }));
    }

    async function request() {
      LR.setBusy(newCode, true);
      try {
        const data = await LR.post('/api/cluster/pairing', {});
        const p = data.pairing || {};
        current = LR.str(p.code);
        expires = fmt.parseTime(p.expires_at);
        codeEl.textContent = current;
        codeEl.setAttribute('aria-label', LR.t('Pairing code ') + current.split('').join(' '));
        cmd.textContent = 'python3 lowram.py worker --join ' + current;
        tick();
      } catch (err) {
        codeEl.replaceChildren(h('span', { class: 'pair-error', text: LR.t('Could not create a code') }));
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
          status.replaceChildren(LR.callout({ tone: 'ok', title: LR.str(newest.name, LR.t('A new device')) + LR.t(' joined'), text: LR.t('It will start helping with shared task lists.') }));
          LR.announce(LR.str(newest.name, LR.t('A new device')) + LR.t(' joined'));
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
    const stateText = { idle: LR.t('Not helping anyone'), pairing: LR.t('Connecting…'), working: LR.t('Helping'), error: 'Problem', stopped: LR.t('Stopped') };
    if (w.state === 'awaiting_approval') {
      ui.worker.body.replaceChildren(h('div', { class: 'verify' }, [
        h('p', { class: 'verify-label', text: LR.t('Verification number') }),
        h('p', { class: 'verify-code verify-big', 'aria-label': LR.t('Verification number ') + LR.str(w.verification_code), text: LR.str(w.verification_code, '— — —') }),
        h('p', { class: 'verify-help', text: LR.t('Check that ') + LR.str(w.host, LR.t('the other computer')) + LR.t(' shows the same number, then click Allow there.') }),
        h('p', { class: 'muted small verify-wait' }, [LR.spinner(), LR.t(' Waiting for approval…')]),
        LR.button({ text: LR.t('Cancel'), variant: 'ghost', onclick: (ev) => leave(ev.currentTarget) }),
      ]));
      return;
    }
    if (w.state === 'pairing' || isConnected(w)) {
      const task = w.current_task;
      const taskText = task === null || task === undefined || task === '' ? '' : typeof task === 'object' ? LR.t('Working on ') + LR.str(task.title || (LR.t('task ') + task.id)) : LR.t('Working on task ') + task;
      const host = LR.str(w.host, LR.t('the other computer'));
      const working = w.state === 'working' || Boolean(taskText);
      const title = w.state === 'pairing' && !isConnected(w) ? LR.t('Connecting to ') + host + '…' : working ? LR.t('Helping ') + host : LR.t('Connected to ') + host;
      const detail = [taskText || LR.str(w.message, w.state === 'pairing' ? LR.t('Checking the code…') : LR.t('Waiting for tasks…'))];
      if (isConnected(w)) detail.push(fmt.plural(w.tasks_done || 0, 'task') + LR.t(' done'));
      out.push(h('div', { class: 'worker-state' }, [
        h('span', { class: 'dot dot-' + (working ? 'busy' : w.state === 'error' ? 'error' : 'ok') + (working || w.state === 'pairing' ? ' is-pulsing' : ''), 'aria-hidden': 'true' }),
        h('div', null, [h('p', { class: 'strong', text: title }), h('p', { class: 'muted small', text: detail.join(' · ') })]),
        LR.button({ text: LR.t('Leave'), icon: 'power', size: 'sm', variant: 'secondary', onclick: (ev) => leave(ev.currentTarget) }),
      ]));
      if (w.state === 'error' && w.error) out.push(LR.callout({ tone: 'error', title: LR.t('Connection problem'), text: LR.str(w.error), role: 'alert' }));
      ui.worker.body.replaceChildren(...out);
      return;
    }
    if (w.state === 'error' && w.error) out.push(LR.callout({ tone: 'error', title: LR.t('Could not help'), text: LR.str(w.error), role: 'alert' }));
    else if (w.state === 'stopped' && LR.isNum(w.tasks_done) && w.tasks_done) out.push(h('p', { class: 'muted small', text: LR.t('Last session: ') + fmt.plural(w.tasks_done, 'task') + LR.t(' done for ') + LR.str(w.host, LR.t('another computer')) + '.' }));
    else out.push(h('p', { class: 'muted small', text: stateText[w.state] ? stateText[w.state] + '.' : '' }));
    const code = h('input', { class: 'input mono', type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder: LR.t('Code from the other computer'), maxlength: '512' });
    const name = h('input', { class: 'input', type: 'text', autocomplete: 'off', maxlength: '80', placeholder: LR.t('e.g. Old MacBook') });
    const settingsName = LR.state.settings && LR.state.settings.cluster ? LR.state.settings.cluster.device_name : '';
    if (settingsName) name.value = settingsName;
    const fCode = LR.field({ label: LR.t('Pairing code'), control: code });
    const fName = LR.field({ label: LR.t('Name for this computer'), control: name, optional: true });
    const join = LR.button({ text: LR.t('Join'), icon: 'link', variant: 'primary', type: 'submit' });
    const form = h('form', { class: 'form join-form', novalidate: true }, [h('div', { class: 'form-row' }, [fCode.el, fName.el]), h('div', { class: 'form-actions' }, [join])]);
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      fCode.setError('');
      if (!code.value.trim()) {
        fCode.setError(LR.t('Enter the code shown on the other computer.'));
        code.focus();
        return;
      }
      LR.setBusy(join, true, LR.t('Joining…'));
      try {
        const data = await LR.post('/api/cluster/join', { code: code.value.trim(), name: name.value.trim() || undefined });
        if (data.worker && st.cluster) st.cluster.worker = data.worker;
        st.fastUntil = Date.now() + 15000;
        LR.toast(LR.t('Joining…'), 'info');
        poller.now();
      } catch (err) {
        fCode.setError(err.message);
        LR.setBusy(join, false);
      }
    });
    if (easyPairing()) {
      if (!ui.nearby) ui.nearby = h('div', { class: 'nearby', 'aria-live': 'polite' });
      st.nearbySig = '';
      renderNearby();
      out.push(h('h3', { class: 'section-label', text: LR.t('Nearby computers') }), ui.nearby);
      out.push(h('details', { class: 'more-options code-fallback' }, [
        h('summary', null, [icon('chevron-right', 'summary-chevron'), h('span', { text: LR.t('Use a code instead') })]),
        h('div', { class: 'details-body' }, form),
      ]));
    } else {
      out.push(form);
    }
    ui.worker.body.replaceChildren(...out);
  }

  async function leave(btn) {
    LR.setBusy(btn, true, LR.t('Leaving…'));
    try {
      await LR.post('/api/cluster/leave', {});
      LR.toast(LR.t('This computer stopped helping'), 'info');
    } catch (err) {
      LR.toast(LR.t('Could not leave: ') + err.message, 'error');
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

  function render(incremental) {
    if (!ui.host) return;
    if (!st.loaded) {
      ui.host.body.replaceChildren(LR.skeleton(3));
      ui.worker.body.replaceChildren(LR.skeleton(2));
      return;
    }
    const c = st.cluster || {};
    const hostSig = JSON.stringify({ ...c, worker: null });
    const workerSig = JSON.stringify([c.worker || null, Array.isArray(c.requests)]);
    const focusedLabel = document.activeElement && ui.page.contains(document.activeElement) ? document.activeElement.getAttribute('aria-label') || document.activeElement.textContent : null;
    if (!incremental || hostSig !== ui.hostSig) renderHost();
    if (!incremental || workerSig !== ui.workerSig) renderWorker();
    ui.hostSig = hostSig;
    ui.workerSig = workerSig;
    if (focusedLabel) {
      const again = [...ui.page.querySelectorAll('button, input')].find((el) => (el.getAttribute('aria-label') || el.textContent) === focusedLabel);
      if (again) again.focus();
    }
  }

  function create() {
    const root = h('section', { class: 'view view-page', 'aria-labelledby': 'devices-title' });
    ui.page = h('div', { class: 'page' });
    root.append(ui.page);
    ui.host = card(LR.t('This computer'), LR.t('Share your task queue with other computers you own.'));
    ui.worker = card(LR.t('Join another computer'), LR.t('Lend this computer’s memory to a LowRAM AI on your network.'));
    return root;
  }

  LR.registerView('devices', {
    title: LR.t('Devices'),
    create,
    show() {
      const head = LR.pageHead({ title: LR.t('Devices'), sub: LR.t('Spread long task lists across the computers you already have. Everything stays on your own network.') });
      head.querySelector('h1').id = 'devices-title';
      if (!LR.has('cluster')) {
        ui.page.replaceChildren(head, LR.unavailable(LR.t('Devices')));
        return;
      }
      ui.page.replaceChildren(head, ui.host.el, ui.worker.el, h('p', { class: 'page-note' }, [icon('shield'), h('span', { text: LR.t('Devices pair with a one-time code and an encrypted, pinned connection. Revoke a device at any time.') })]));
      st.sig = '';
      render();
      poller.start();
      nearbyPoller.start();
    },
    hide() {
      poller.stop();
      nearbyPoller.stop();
    },
  });
})();
