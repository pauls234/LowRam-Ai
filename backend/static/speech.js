/* LowRAM AI Studio – speech: dictate with the microphone and transcribe recordings on this computer
 * (whisper.cpp). The page records 16-kHz mono WAV and sends it only to LowRAM AI on this computer,
 * which deletes it right after the transcription. While it works, percent and time left come from
 * the server. Transcripts are untrusted text: textContent only. */
(function () {
  'use strict';

  const LR = window.LR;
  const h = LR.h;
  const icon = LR.icon;
  const fmt = LR.fmt;
  const t = LR.t;
  const RATE = 16000;
  const SUFFIXES = ['.wav', '.mp3', '.m4a', '.aac', '.flac', '.ogg', '.oga', '.opus', '.aiff', '.aif', '.caf'];
  const sleep = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

  const sp = (LR.speech = { status: null, available: null, suffixes: SUFFIXES });

  /* ------------------------------------------------------------ status */

  sp.load = async function () {
    try {
      sp.status = await LR.get('/api/speech');
      sp.available = true;
    } catch (err) {
      if (err.status === 404) { sp.available = false; sp.status = null; }
    }
    LR.emit('speech', sp.status);
    return sp.status;
  };
  sp.isAudio = (file) => Boolean(file) && (SUFFIXES.some((s) => LR.str(file.name).toLowerCase().endsWith(s)) || /^audio\//.test(file.type || ''));

  let probed = false;
  LR.on('status', () => {
    if (probed) return;
    probed = true;
    if (LR.mayHave('speech')) sp.load();
  });

  function loadingModel(s) {
    return (s && s.models.find((m) => m.download && LR.downloads.isActive(m.download))) || null;
  }

  /** True when dictation can start now; otherwise it explains why or offers the one-time download. */
  sp.ensureReady = async function () {
    const s = await sp.load();
    if (!s) {
      LR.toast(t('Speech recognition is not part of this build.'), 'error');
      return false;
    }
    if (s.available) return true;
    if (!s.cli) {
      LR.toast(t('The speech recognition program (whisper.cpp) is missing. It is part of the finished app – when you start LowRAM AI from the source code, install it with: brew install whisper-cpp'), 'error');
      return false;
    }
    const loading = loadingModel(s);
    if (loading) {
      LR.toast(t('The speech model is still downloading – {progress}', { progress: LR.downloads.summary(loading.download) }));
      return false;
    }
    const model = s.models.find((m) => m.recommended) || s.models[0];
    const ok = await LR.confirm({
      title: t('Set up dictation?'),
      message: t('LowRAM AI downloads the speech model “{name}” ({size}) once. After that, speech recognition runs completely on this computer – your recordings never leave it.', { name: model.name, size: fmt.bytes(model.size_bytes) }),
      confirmText: t('Download'),
    });
    if (!ok) return false;
    try {
      await LR.post('/api/speech/models/' + model.id + '/install', {});
      LR.toast(t('Download started – you can dictate as soon as it is finished.'));
      LR.downloads.refresh();
      sp.load();
    } catch (err) {
      LR.toast(err.message, 'error');
    }
    return false;
  };

  /* ------------------------------------------------------------ recording */

  function audioContext() {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    try { return new AC({ sampleRate: RATE }); } catch (_) { return new AC(); }
  }

  function micError(err) {
    const name = err && err.name;
    if (name === 'NotAllowedError' || name === 'SecurityError') return t('Microphone access was not allowed. Allow it for LowRAM AI in System Settings → Privacy & Security → Microphone, then try again.');
    if (name === 'NotFoundError' || name === 'OverconstrainedError') return t('No microphone was found.');
    if (name === 'NotReadableError') return t('The microphone is being used by another app.');
    return t('The microphone could not be started ({error}).', { error: (err && err.message) || String(err) });
  }

  /** Averaging resampler to 16 kHz – a box filter is plenty for speech. */
  function resampler(rate) {
    if (Math.abs(rate - RATE) < 1) return (input) => input.slice();
    const step = rate / RATE;
    let rest = new Float32Array(0);
    let pos = 0;
    return function (input) {
      const buf = new Float32Array(rest.length + input.length);
      buf.set(rest);
      buf.set(input, rest.length);
      const out = new Float32Array(Math.max(0, Math.floor((buf.length - pos) / step)));
      let n = 0;
      while (pos + step <= buf.length && n < out.length) {
        const start = Math.floor(pos);
        const end = Math.min(buf.length, Math.max(start + 1, Math.floor(pos + step)));
        let sum = 0;
        for (let i = start; i < end; i++) sum += buf[i];
        out[n++] = sum / (end - start);
        pos += step;
      }
      const keep = Math.floor(pos);
      rest = buf.slice(keep);
      pos -= keep;
      return out.subarray(0, n);
    };
  }

  function wav(chunks, samples) {
    const head = new DataView(new ArrayBuffer(44));
    const text = (at, s) => { for (let i = 0; i < s.length; i++) head.setUint8(at + i, s.charCodeAt(i)); };
    text(0, 'RIFF');
    head.setUint32(4, 36 + samples * 2, true);
    text(8, 'WAVE');
    text(12, 'fmt ');
    head.setUint32(16, 16, true);
    head.setUint16(20, 1, true);  // PCM
    head.setUint16(22, 1, true);  // mono
    head.setUint32(24, RATE, true);
    head.setUint32(28, RATE * 2, true);
    head.setUint16(32, 2, true);
    head.setUint16(34, 16, true);
    text(36, 'data');
    head.setUint32(40, samples * 2, true);
    return new Blob([head.buffer, ...chunks], { type: 'audio/wav' });
  }

  /** Microphone → 16-kHz mono 16-bit PCM in this page's memory. `ctx` must come from the click itself. */
  async function record(ctx, onLevel) {
    if (!ctx || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error(t('This window cannot use the microphone. Open {url} in your web browser – or record with another app and add the file under “Documents”.', { url: window.location.origin }));
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch (err) {
      ctx.close().catch(() => {});
      throw new Error(micError(err));
    }
    let source;
    try {
      source = ctx.createMediaStreamSource(stream);
    } catch (_) {  // some browsers refuse a 16-kHz context for a 48-kHz microphone: resample here instead
      ctx.close().catch(() => {});
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      source = ctx.createMediaStreamSource(stream);
    }
    if (ctx.state === 'suspended') await ctx.resume().catch(() => {});
    const resample = resampler(ctx.sampleRate);
    const chunks = [];
    let samples = 0;
    const take = (input) => {
      let sum = 0;
      for (let i = 0; i < input.length; i++) sum += input[i] * input[i];
      if (onLevel && input.length) onLevel(Math.sqrt(sum / input.length));
      const out = resample(input);
      const pcm = new Int16Array(out.length);
      for (let i = 0; i < out.length; i++) {
        const v = Math.max(-1, Math.min(1, out[i]));
        pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
      }
      chunks.push(pcm);
      samples += pcm.length;
    };
    // The audio thread (AudioWorklet) never drops sound while the page is busy; old browsers: ScriptProcessor.
    let node = null;
    let flushed = null;
    if (ctx.audioWorklet && window.AudioWorkletNode) {
      try {
        await ctx.audioWorklet.addModule('/static/recorder-worklet.js');
        node = new AudioWorkletNode(ctx, 'lowram-recorder', { numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1 });
        node.port.onmessage = (ev) => {
          take(ev.data.samples);
          if (ev.data.last && flushed) flushed();
        };
      } catch (_) {
        node = null;
      }
    }
    if (!node) {
      node = ctx.createScriptProcessor(4096, 1, 1);
      node.onaudioprocess = (ev) => take(ev.inputBuffer.getChannelData(0));
    }
    source.connect(node);
    node.connect(ctx.destination);  // silent: nothing is written to the output
    const started = performance.now();
    const release = () => {
      if (node.port) node.port.onmessage = null;
      else node.onaudioprocess = null;
      try { source.disconnect(); node.disconnect(); } catch (_) { /* already disconnected */ }
      stream.getTracks().forEach((track) => track.stop());
      ctx.close().catch(() => {});
    };
    const flush = () => new Promise((resolve) => {  // the last part of a word is still in the audio thread
      if (!node.port) { resolve(); return; }
      const timer = window.setTimeout(resolve, 400);
      flushed = () => { window.clearTimeout(timer); resolve(); };
      node.port.postMessage('flush');
    });
    return {
      elapsed: () => performance.now() - started,
      seconds: () => samples / RATE,
      async finish() {
        await flush();
        release();
        return wav(chunks, samples);
      },
      discard() { release(); chunks.length = 0; },
    };
  }

  /* ------------------------------------------------------------ transcription */

  /** Send audio to LowRAM AI on this computer and wait for the text (progress via `onProgress`). */
  sp.transcribe = async function (file, opts) {
    const o = opts || {};
    const max = sp.status && sp.status.max_upload_bytes;
    if (LR.isNum(max) && file.size > max) {
      throw new Error(t('{name} is larger than {n} MB.', { name: file.name, n: Math.round(max / 1048576) }));
    }
    const query = o.language ? '?language=' + encodeURIComponent(o.language) : '';
    let job = (await LR.upload('/api/speech/transcribe' + query, file)).job;
    let stopped = false;
    const cancel = () => {
      stopped = true;
      return LR.post('/api/speech/jobs/' + job.id + '/cancel', {}).catch(() => {});
    };
    if (o.onStart) o.onStart(cancel);
    if (o.onProgress) o.onProgress(job);
    let misses = 0;
    while (!['done', 'failed', 'cancelled'].includes(job.status)) {
      await sleep(job.status === 'running' ? 700 : 1200);
      try {
        job = (await LR.get('/api/speech/jobs/' + job.id)).job;
        misses = 0;
      } catch (err) {
        if (err.status === 404 || ++misses > 30) throw err;
        continue;
      }
      if (o.onProgress) o.onProgress(job);
    }
    if (job.status === 'done') return job;
    const error = new Error(job.status === 'cancelled' ? t('Cancelled') : LR.str(job.error) || t('The transcription failed.'));
    error.cancelled = job.status === 'cancelled' || stopped;
    throw error;
  };

  /** "Turning speech into text … 40 % · about 20 s left" – or why it is waiting. */
  sp.progressText = function (job) {
    if (!job) return '';
    if (job.status === 'running') {
      // whisper.cpp works through a long recording in 30-second parts: "Teil 3 von 12 · noch 10 Teile"
      const parts = LR.isNum(job.audio_s) ? Math.ceil(job.audio_s / 30) : 0;
      if (parts > 1) {
        const part = Math.min(parts, Math.floor(((job.percent || 0) / 100) * parts) + 1);
        const progress = LR.progressText({ percent: job.percent > 0 ? job.percent : null, eta_s: job.eta_s });
        return [t('Turning speech into text: part {n} of {total}', { n: part, total: parts }),
          LR.leftText(parts - part + 1, 'part'), progress].filter(Boolean).join(' · ');
      }
      const progress = LR.progressText({ percent: job.percent > 0 ? job.percent : null, eta_s: job.eta_s });
      if (progress) return t('Turning speech into text … {progress}', { progress });
      return job.first_run ? t('Turning speech into text … the first time takes a little longer (preparing the speech recognition)') : t('Turning speech into text …');
    }
    if (job.message) return LR.str(job.message);
    return job.status === 'queued' ? t('Waiting to start') : '';
  };

  /* ------------------------------------------------------------ the chat's microphone */

  /** The microphone button: record → text on this computer → `onText(text)`; `onStatus(text)` for the line below. */
  sp.micButton = function (opts) {
    const o = opts || {};
    const btn = h('button', { type: 'button', class: 'icon-btn mic-btn' });
    const time = h('span', { class: 'mic-time', 'aria-hidden': 'true' });
    const discard = h('button', { type: 'button', class: 'icon-btn mic-discard', title: t('Discard the recording'), 'aria-label': t('Discard the recording') }, icon('x'));
    const el = h('span', { class: 'mic' }, [btn, time, discard]);
    let rec = null;
    let timer = 0;
    let cancelJob = null;
    const say = (text) => { if (o.onStatus) o.onStatus(text || ''); };

    function setState(state) {
      el.dataset.state = state;  // idle | starting | recording | working
      const label = state === 'recording' ? t('Stop and turn into text')
        : state === 'working' ? t('Cancel the transcription')
          : t('Dictate – speech becomes text on this computer');
      btn.replaceChildren(icon(state === 'recording' ? 'stop' : state === 'working' ? 'x' : 'mic'));
      btn.setAttribute('aria-label', label);
      btn.title = label;
      btn.setAttribute('aria-pressed', String(state === 'recording'));
      btn.disabled = state === 'starting';
      time.hidden = state !== 'recording';
      discard.hidden = state !== 'recording';
      if (state !== 'recording') el.style.removeProperty('--level');
    }

    function stopTimer() {
      window.clearInterval(timer);
      timer = 0;
    }

    async function start() {
      const ctx = audioContext();  // inside the click: browsers only start audio after a user gesture
      setState('starting');
      if (!(await sp.ensureReady())) {
        if (ctx) ctx.close().catch(() => {});
        setState('idle');
        return;
      }
      try {
        rec = await record(ctx, (level) => el.style.setProperty('--level', Math.min(1, level * 6).toFixed(2)));
      } catch (err) {
        setState('idle');
        LR.toast(err.message, 'error');
        return;
      }
      setState('recording');
      if (o.onRecording) o.onRecording(true);
      const max = (sp.status && sp.status.max_record_s) || 1800;
      const tick = () => {
        if (!rec) return;
        const sec = rec.elapsed() / 1000;
        time.textContent = fmt.clock(sec * 1000);
        if (sec >= max) finish();
        else if (sec >= max - 60) say(t('The recording stops by itself in {n} s.', { n: Math.ceil(max - sec) }));
      };
      tick();
      timer = window.setInterval(tick, 250);
      say(t('Listening … click the microphone again when you are done.'));
    }

    async function finish() {
      stopTimer();
      const r = rec;
      rec = null;
      if (!r) return;
      if (o.onRecording) o.onRecording(false);
      const blob = await r.finish();
      const seconds = r.seconds();
      if (seconds < 0.5) {
        setState('idle');
        say('');
        LR.toast(t('That was too short – speak a little longer.'), 'error');
        return;
      }
      setState('working');
      say(t('Turning speech into text …'));
      try {
        const job = await sp.transcribe(new File([blob], t('Dictation') + '.wav', { type: 'audio/wav' }), {
          onStart: (cancel) => { cancelJob = cancel; },
          onProgress: (job) => say(sp.progressText(job)),
        });
        say('');
        if (o.onText) o.onText(LR.str(job.text));
      } catch (err) {
        say('');
        if (!err.cancelled) LR.toast(err.message, 'error');
      } finally {
        cancelJob = null;
        setState('idle');
      }
    }

    function drop() {
      stopTimer();
      if (rec) { rec.discard(); rec = null; }
      if (o.onRecording) o.onRecording(false);
      setState('idle');
      say('');
    }

    btn.addEventListener('click', () => {
      const state = el.dataset.state;
      if (state === 'recording') finish();
      else if (state === 'working') { if (cancelJob) cancelJob(); }
      else if (state === 'idle') start();
    });
    discard.addEventListener('click', drop);
    setState('idle');
    const sync = () => { el.hidden = sp.available === false; };
    LR.on('speech', sync);
    sync();
    return { el, discard: drop, busy: () => el.dataset.state !== 'idle' };
  };

  /* ------------------------------------------------------------ settings */

  sp.settingsCard = function () {
    const headingId = LR.uid('sec');
    const body = h('div', { class: 'card-body speech-settings' });
    const el = h('section', { class: 'card', id: 'speech-settings', 'aria-labelledby': headingId, hidden: true }, [
      h('div', { class: 'card-head' }, [h('div', null, [
        h('h2', { class: 'card-title', id: headingId, text: t('Speech & dictation') }),
        h('p', { class: 'card-sub', text: t('Speak instead of typing: the speech recognition (Whisper) runs completely on this computer. Recordings are deleted right after they became text.') }),
      ])]),
      body,
    ]);
    let off = null;
    let sig = null;

    async function refresh() {
      const s = await sp.load();
      el.hidden = !s;
      if (s) render(s);
    }

    async function install(m, btn) {
      LR.setBusy(btn, true);
      try {
        await LR.post('/api/speech/models/' + m.id + '/install', {});
        LR.downloads.refresh();
        await refresh();
      } catch (err) {
        LR.toast(err.message, 'error');
        LR.setBusy(btn, false);
      }
    }

    async function choose(m, btn) {
      LR.setBusy(btn, true);
      try {
        await LR.post('/api/speech/settings', { model: m.id });
        await refresh();
        LR.toast(t('{name} is used for dictation now.', { name: m.name }));
      } catch (err) {
        LR.toast(err.message, 'error');
        LR.setBusy(btn, false);
      }
    }

    async function remove(m, btn) {
      const ok = await LR.confirm({ title: t('Delete this speech model?'), message: t('“{name}” ({size}) is removed from this computer. You can download it again at any time.', { name: m.name, size: fmt.bytes(m.size_bytes) }), confirmText: t('Delete'), danger: true });
      if (!ok) return;
      LR.setBusy(btn, true);
      try {
        await LR.post('/api/speech/models/' + m.id + '/delete', {});
        await refresh();
      } catch (err) {
        LR.toast(err.message, 'error');
        LR.setBusy(btn, false);
      }
    }

    function modelRow(s, m) {
      const inUse = s.available && s.model === m.id;
      const badges = [];
      if (m.recommended) badges.push(LR.badge(t('Recommended'), 'accent'));
      if (inUse) badges.push(LR.badge(t('In use'), 'ok'));
      else if (m.installed) badges.push(LR.badge(t('Installed'), 'neutral'));
      const meta = [t(LR.str(m.quality)), fmt.bytes(m.size_bytes)];
      if (!m.fits) meta.push(t('needs {n} GB of memory', { n: m.min_ram_gb }));
      const d = m.download;
      const actions = [];
      if (d && LR.downloads.isActive(d)) {
        actions.push(LR.downloads.row(d, { label: m.name }).el);
      } else if (!m.installed) {
        actions.push(LR.button({ text: t('Download'), icon: 'download', size: 'sm', variant: m.recommended ? 'primary' : 'secondary', disabled: !m.fits, onclick: (ev) => install(m, ev.currentTarget) }));
      } else {
        if (!inUse) actions.push(LR.button({ text: t('Use'), size: 'sm', variant: 'secondary', onclick: (ev) => choose(m, ev.currentTarget) }));
        actions.push(LR.button({ text: t('Delete'), icon: 'trash', size: 'sm', variant: 'ghost', onclick: (ev) => remove(m, ev.currentTarget) }));
      }
      return h('li', { class: 'speech-model' }, [
        h('div', { class: 'speech-model-head' }, [h('p', { class: 'strong', text: m.name }), ...badges]),
        h('p', { class: 'muted small', text: meta.join(' · ') }),
        d && d.state === 'failed' ? h('p', { class: 'small is-error-text', text: LR.str(d.error) }) : null,
        h('div', { class: 'doc-actions' }, actions),
      ].filter(Boolean));
    }

    function render(s) {
      const nameOf = (id) => (s.models.find((m) => m.id === id) || {}).name || id;
      const langOf = (id) => (s.languages.find((l) => l.id === id) || {}).label || id;
      const [tone, text] = s.available
        ? ['ok', t('Ready – {model}, {language}. Use the microphone next to the chat input.', { model: nameOf(s.model), language: langOf(s.language) })]
        : !s.cli ? ['warn', t('The speech recognition program is missing. When you start LowRAM AI from the source code: brew install whisper-cpp')]
          : ['warn', t('Download a speech model once – then you can dictate.')];
      const select = h('select', { class: 'input select' }, s.languages.map((l) => h('option', { value: l.id, text: LR.str(l.label) })));
      select.value = s.language;
      select.addEventListener('change', async () => {
        try {
          await LR.post('/api/speech/settings', { language: select.value });
          LR.toast(t('Language saved'));
          sp.load();
        } catch (err) { LR.toast(err.message, 'error'); }
      });
      const formats = s.formats.map((f) => f.toUpperCase()).join(', ');
      body.replaceChildren(
        h('p', { class: 'speech-state' }, [LR.dot(tone), h('span', { text })]),
        h('ul', { class: 'speech-models' }, s.models.map((m) => modelRow(s, m))),
        LR.field({ label: t('Spoken language'), control: select, hint: t('A fixed language is faster and more accurate than “recognise automatically”.') }).el,
        h('p', { class: 'muted small', text: t('Recordings can also be added under “Documents” ({formats}) – the text can then be summarised or translated.', { formats }) }),
      );
    }

    return {
      el,
      show() {
        refresh();
        if (off) return;
        off = LR.downloads.subscribe((list) => {
          const now = list.filter((d) => d.kind === 'speech-model').map((d) => d.id + ':' + d.state).join(',');
          if (sig !== null && now !== sig) refresh();
          sig = now;
        });
      },
      hide() {
        if (off) { off(); off = null; }
        sig = null;
      },
    };
  };
})();
