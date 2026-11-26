/* LowRAM AI Studio – text recognition (OCR): photos and scanned PDFs become text on this computer
 * (Apple's Vision on the Mac): "Texterkennung: Seite 3 von 12 · noch 10 Seiten · 25 % · noch etwa
 * 30 s". Used by Documents, the chat's attachments and the knowledge base. The recognised text is
 * untrusted: textContent only. */
(function () {
  'use strict';

  const LR = window.LR;
  const t = LR.t;
  const SUFFIXES = ['.jpg', '.jpeg', '.png', '.heic', '.heif', '.tif', '.tiff', '.gif', '.bmp', '.webp'];
  const sleep = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

  const oc = (LR.ocr = { status: null, available: null, suffixes: SUFFIXES });

  /** Whether this computer can recognise text (the Mac): `{available, reason}`. */
  oc.load = async function () {
    try {
      oc.status = await LR.get('/api/ocr');
      oc.available = Boolean(oc.status && oc.status.available);
    } catch (err) {
      if (err.status === 404) { oc.available = false; oc.status = null; }
    }
    LR.emit('ocr', oc.status);
    return oc.status;
  };

  let probed = false;
  LR.on('status', () => {
    if (probed) return;
    probed = true;
    if (LR.mayHave('ocr')) oc.load();
  });

  /** A photo or picture of a page (by name or type) – the server checks the bytes. */
  oc.isImage = (file) => Boolean(file) && (SUFFIXES.some((s) => LR.str(file.name).toLowerCase().endsWith(s)) || /^image\//.test(file.type || ''));

  /** A file picker also offers photos once text recognition is known to work on this computer. */
  oc.offerIn = function (input) {
    const base = input.accept;
    const sync = () => { input.accept = oc.available ? base + ',' + SUFFIXES.join(',') : base; };
    sync();
    LR.on('ocr', sync);
  };

  /** "Texterkennung: Seite 3 von 12 · noch 10 Seiten · 25 % · noch etwa 30 s" – or why it waits. */
  oc.progressText = function (job) {
    if (!job) return '';
    if (job.status === 'running') {
      const progress = LR.progressText({ percent: job.percent > 0 ? job.percent : null, eta_s: job.eta_s });
      if (LR.isNum(job.pages) && job.pages > 1 && LR.isNum(job.page)) {
        return [t('Text recognition: page {n} of {total}', { n: job.page, total: job.pages }),
          LR.leftText(job.pages - job.done, 'page'), progress].filter(Boolean).join(' · ');
      }
      return progress ? t('Text recognition') + ' · ' + progress : t('Recognising the text…');
    }
    if (job.message) return LR.str(job.message);
    return job.status === 'queued' ? t('Waiting to start') : t('Recognising the text…');
  };

  /** Recognise the text of an upload that `needs_ocr`; resolves with the finished upload (meta + text). */
  oc.run = async function (upload, opts) {
    const o = opts || {};
    let job = (await LR.post('/api/ocr/uploads/' + encodeURIComponent(upload.id), {})).job;
    let stopped = false;
    const cancel = () => {
      stopped = true;
      return LR.post('/api/ocr/jobs/' + job.id + '/cancel', {}).catch(() => {});
    };
    if (o.onStart) o.onStart(cancel);
    if (o.onProgress) o.onProgress(job);
    let misses = 0;
    while (!['done', 'failed', 'cancelled'].includes(job.status)) {
      await sleep(800);
      try {
        job = (await LR.get('/api/ocr/jobs/' + job.id)).job;
        misses = 0;
      } catch (err) {
        if (err.status === 404 || ++misses > 30) throw err;
        continue;
      }
      if (o.onProgress && !['done', 'failed', 'cancelled'].includes(job.status)) o.onProgress(job);
    }
    if (job.status === 'done') {
      const done = await LR.get('/api/documents/uploads/' + encodeURIComponent(upload.id));
      return Object.assign({}, done, { preview: LR.str(done.text).slice(0, 1500) });
    }
    const error = new Error(job.status === 'cancelled' ? t('Cancelled') : LR.str(job.error) || t('The text recognition failed.'));
    error.cancelled = job.status === 'cancelled' || stopped;
    throw error;
  };
})();
