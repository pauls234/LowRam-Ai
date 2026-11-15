/* LowRAM AI Studio – "Already on this computer": AI models that other apps (Ollama, LM Studio,
 * Jan, GPT4All, Hugging Face) already downloaded and AI programs that already run here. Used by
 * the first-run wizard and by Models & Settings. Everything stays on this computer: found files
 * are used in place, running servers are only reached on 127.0.0.1. */
(function () {
  'use strict';

  const LR = window.LR;
  const h = LR.h;
  const fmt = LR.fmt;
  let cached = null; // { promise, at }
  let seq = 0;

  function load(force) {
    if (!force && cached && Date.now() - cached.at < 30000) return cached.promise;
    const promise = LR.get('/api/localai' + (force ? '?refresh=1' : ''));
    cached = { promise, at: Date.now() };
    promise.catch(() => { cached = null; });
    return promise;
  }

  /**
   * Card with the found models and servers.
   * opts: { context: 'setup' | 'settings', onModel(model, found), onServer(localServer | null) }.
   * In the wizard the card stays hidden while nothing was found.
   */
  function card(opts) {
    const o = opts || {};
    const titleId = 'found-title-' + (++seq);
    const body = h('div', { class: 'found-body', 'aria-live': 'polite' }, LR.skeleton(2));
    const el = h('section', { class: 'card found-card' + (o.context === 'setup' ? ' setup-card' : ''), 'aria-labelledby': titleId }, [
      h('h2', { class: 'card-title', id: titleId, text: LR.t('Already on this computer') }),
      h('p', { class: 'card-sub', text: LR.t('AI models that other apps already downloaded, and AI programs that are running right now. Use them directly – no new download.') }),
      body,
    ]);
    if (o.context === 'setup') el.hidden = true;
    el.refresh = (force) => fill(Boolean(force));
    fill(false);
    return el;

    async function fill(force) {
      let data;
      try {
        data = await load(force);
      } catch (err) {
        if (o.context === 'setup') { el.hidden = true; return; }
        body.replaceChildren(LR.callout({ tone: 'error', title: LR.t('Could not look for local AI'), text: err.message,
          actions: [LR.button({ text: LR.t('Try again'), icon: 'retry', onclick: () => fill(true) })] }));
        return;
      }
      const models = Array.isArray(data.models) ? data.models : [];
      const servers = Array.isArray(data.servers) ? data.servers : [];
      if (o.context === 'setup' && !models.length && !servers.length) { el.hidden = true; return; }
      el.hidden = false;
      const parts = [];
      const busy = servers.filter((s) => Array.isArray(s.loaded) && s.loaded.length && !s.in_use);
      if (busy.length && models.length) {
        parts.push(LR.callout({ tone: 'warn', title: LR.t('Save memory'),
          text: LR.t('{name} holds a model in memory right now. Use it directly below – or quit {name} before you start a model here, so that two models do not fill your memory.', { name: busy[0].name }) }));
      }
      for (const s of servers) parts.push(serverRow(s, data.local_server));
      if (models.length) parts.push(h('ul', { class: 'found-list' }, models.map(modelRow)));
      if (!models.length && !servers.length) {
        parts.push(h('p', { class: 'muted', text: LR.t('Nothing found. Models of Ollama, LM Studio, Jan, GPT4All and the Hugging Face cache – and AI programs running on this computer – show up here automatically.') }));
      }
      if (data.truncated) parts.push(h('p', { class: 'muted small', text: LR.t('The search stopped after a few seconds – not every folder was checked.') }));
      const again = LR.button({ text: LR.t('Search again'), icon: 'retry', variant: 'ghost', size: 'sm', onclick: async () => {
        LR.setBusy(again, true, LR.t('Searching…'));
        await fill(true);
      } });
      parts.push(h('div', { class: 'found-actions' }, [again,
        h('span', { class: 'muted small', text: LR.t('Files stay where they are – nothing is copied or deleted.') })]));
      body.replaceChildren(...parts);
    }

    function modelRow(m) {
      const added = Boolean(m.model_id);
      const facts = [LR.t('from {app}', { app: m.source }), m.params, m.quant, LR.isNum(m.size_bytes) ? fmt.bytes(m.size_bytes) : '']
        .filter(Boolean).join(' · ');
      const btn = LR.button({
        text: added ? LR.t('Start') : LR.t('Use'), icon: added ? 'play' : 'plus',
        variant: m.fits_ram ? 'primary' : 'secondary', size: 'sm',
        title: m.fits_ram ? null : LR.t('Probably too large for the memory of this computer'),
        onclick: async () => {
          LR.setBusy(btn, true, LR.t('Adding…'));
          try {
            const res = await LR.post('/api/localai/use', { id: m.id });
            cached = null;
            if (o.onModel) await o.onModel(res.model, m);
          } catch (err) {
            LR.toast(err.message, 'error');
          } finally {
            if (btn.isConnected) LR.setBusy(btn, false);
          }
        },
      });
      return h('li', { class: 'found-row' }, [
        h('div', { class: 'found-main' }, [
          h('p', { class: 'found-name' }, [
            h('span', { text: m.name }),
            added ? LR.badge(LR.t('Added'), 'ok') : null,
            m.purpose === 'code' ? LR.badge('Code', 'info') : null,
            m.fits_ram ? null : LR.badge(LR.t('too large for this computer'), 'warn'),
          ]),
          h('p', { class: 'found-meta', text: facts }),
          h('p', { class: 'found-path mono', text: m.path, title: m.path }),
        ]),
        btn,
      ]);
    }

    function serverRow(s, current) {
      const inUse = Boolean(s.in_use);
      const models = Array.isArray(s.models) ? s.models : [];
      const loaded = (Array.isArray(s.loaded) ? s.loaded : []).filter((x) => x && x.name);
      const preferred = (inUse && current && current.model) || (loaded[0] && models.includes(loaded[0].name) ? loaded[0].name : models[0]);
      const select = h('select', { class: 'input select found-select', 'aria-label': LR.t('Model of {name}', { name: s.name }) },
        models.map((name) => h('option', { value: name, text: name, selected: name === preferred })));
      const use = LR.button({ text: inUse ? LR.t('Switch model') : LR.t('Use directly'), icon: inUse ? 'retry' : 'zap', variant: inUse ? 'secondary' : 'primary', size: 'sm',
        onclick: async () => {
          LR.setBusy(use, true, LR.t('Connecting…'));
          try {
            const res = await LR.post('/api/localai/server', { id: s.id, model: select.value });
            cached = null;
            LR.toast(LR.t('LowRAM AI now answers with {name} ({model}).', { name: s.name, model: select.value }));
            if (o.onServer) await o.onServer(res.local_server);
          } catch (err) {
            LR.toast(err.message, 'error');
          } finally {
            if (use.isConnected) LR.setBusy(use, false);
          }
        } });
      const back = inUse ? LR.button({ text: LR.t('Use the built-in engine again'), variant: 'ghost', size: 'sm', onclick: async () => {
        LR.setBusy(back, true);
        try {
          await LR.post('/api/localai/server', { id: null });
          cached = null;
          if (o.onServer) await o.onServer(null);
        } catch (err) {
          LR.toast(err.message, 'error');
        } finally {
          if (back.isConnected) LR.setBusy(back, false);
        }
      } }) : null;
      const memory = loaded.length ? LR.t('In memory right now: {models}', { models: loaded.map((x) => x.name + (LR.isNum(x.size_bytes) ? ' (' + fmt.bytes(x.size_bytes) + ')' : '')).join(', ') }) : '';
      return h('div', { class: 'found-server' + (inUse ? ' is-active' : '') }, [
        h('p', { class: 'found-name' }, [
          LR.dot('ok'),
          h('span', { text: LR.t('{name} is running on this computer', { name: s.name }) }),
          inUse ? LR.badge(LR.t('In use'), 'ok') : null,
        ]),
        h('p', { class: 'found-meta', text: LR.t('Chat with its models directly – nothing is downloaded, and the model is not loaded a second time. {name} manages its memory itself.', { name: s.name }) }),
        memory ? h('p', { class: 'found-meta', text: memory }) : null,
        h('div', { class: 'input-row found-server-row' }, [select, use, back]),
      ]);
    }
  }

  LR.localAI = { card, load, reset() { cached = null; } };
})();
