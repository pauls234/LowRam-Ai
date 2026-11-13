/* LowRAM AI Studio – chat: conversations, streaming answers, composer and the
 * sidebar history. Answers are rendered with LR.md (DOM only, no innerHTML). */
(function () {
  'use strict';

  const LR = window.LR;
  const h = LR.h;
  const icon = LR.icon;
  const fmt = LR.fmt;

  const EXAMPLES = {
    chat: [
      { title: 'Explain an idea', prompt: 'Explain how a hash map works, with a small example.' },
      { title: 'Write a friendly email', prompt: 'Help me write a short, friendly email declining a meeting next week.' },
      { title: 'Plan a weekend', prompt: 'Plan a relaxed weekend in Lisbon on a small budget.' },
      { title: 'Weigh pros and cons', prompt: 'What are the pros and cons of running AI models locally?' },
    ],
    code: [
      { title: 'Write a function', prompt: 'Write a Python function that removes duplicate lines from a file but keeps the order.' },
      { title: 'Explain a regex', prompt: 'Explain this regular expression step by step: ^(?=.*\\d)[A-Za-z\\d]{8,}$' },
      { title: 'Find the bug', prompt: 'Why does `for i in range(len(items)): items.pop(i)` fail in Python?' },
      { title: 'Shell one-liner', prompt: 'Give me a shell command that finds the 10 largest files in a folder.' },
    ],
  };

  const S = {
    chats: [],
    listLoaded: false,
    currentId: null,
    newMode: LR.store.get('lowram.chatMode') === 'code' ? 'code' : 'chat',
    panes: new Map(),
    newPane: null,
    stream: null,
    stick: true,
    ui: {},
    historyEl: null,
    menu: null,
    draftTimer: 0,
  };

  /* ------------------------------------------------------------ helpers */

  const chatById = (id) => S.chats.find((c) => c.id === id) || null;
  const currentPane = () => (S.currentId === null ? S.newPane : S.panes.get(S.currentId));
  const draftKey = (id) => 'lowram.draft.' + (id === null || id === undefined ? 'new' : id);

  function greeting() {
    const hour = new Date().getHours();
    if (hour >= 5 && hour < 12) return 'Good morning';
    if (hour >= 12 && hour < 18) return 'Good afternoon';
    return 'Good evening';
  }

  function paneMode(pane) {
    return pane && pane.chat && pane.chat.mode === 'code' ? 'code' : pane && pane.id === null ? S.newMode : 'chat';
  }

  function makePane(id, chat) {
    return { id, chat: chat || null, list: h('ol', { class: 'messages', 'aria-label': 'Conversation' }), loaded: id === null, loading: false, count: 0 };
  }

  /* ------------------------------------------------------------ view */

  function create() {
    const ui = S.ui;
    ui.root = h('section', { class: 'view view-chat is-empty', 'aria-labelledby': 'chat-heading' });
    ui.heading = h('h1', { class: 'visually-hidden', id: 'chat-heading', text: 'Chat' });
    ui.greet = h('h2', { class: 'welcome-title' });
    ui.greetSub = h('p', { class: 'welcome-sub' });
    ui.welcome = h('div', { class: 'welcome' }, [
      h('span', { class: 'welcome-mark', 'aria-hidden': 'true' }, icon('logo')),
      ui.greet,
      ui.greetSub,
    ]);
    ui.thread = h('div', { class: 'thread' });
    ui.unavailable = h('div', { class: 'chat-unavailable', hidden: true });
    ui.column = h('div', { class: 'chat-column' }, [ui.welcome, ui.unavailable, ui.thread]);
    ui.scroll = h('div', { class: 'chat-scroll' }, ui.column);
    ui.jump = h('button', { type: 'button', class: 'jump-latest', 'aria-label': 'Jump to the latest message', hidden: true, onclick: () => scrollToBottom(true) }, icon('arrow-down'));
    ui.notice = h('div', { class: 'chat-notice' });
    ui.input = h('textarea', {
      class: 'composer-input',
      id: 'composer-input',
      rows: '1',
      'aria-label': 'Message LowRAM AI',
      'aria-describedby': 'composer-hint',
      autocomplete: 'off',
      spellcheck: 'true',
    });
    ui.mode = LR.segmented({
      label: 'Conversation mode',
      cls: 'segmented-sm',
      options: [
        { value: 'chat', label: 'Chat', icon: 'chat' },
        { value: 'code', label: 'Code', icon: 'code' },
      ],
      value: S.newMode,
      onChange: (mode) => {
        S.newMode = mode;
        LR.store.set('lowram.chatMode', mode);
        renderChrome();
        ui.input.focus();
      },
    });
    ui.send = h('button', { type: 'submit', class: 'send-btn', 'aria-label': 'Send message', title: 'Send (Enter)', disabled: true }, icon('arrow-up'));
    ui.stop = h('button', { type: 'button', class: 'stop-btn', 'aria-label': 'Stop generating', title: 'Stop (Esc)', hidden: true, onclick: stopStream }, icon('stop'));
    ui.form = h('form', { class: 'composer', novalidate: true }, [
      ui.input,
      h('div', { class: 'composer-bar' }, [ui.mode.el, h('span', { class: 'composer-spacer' }), ui.stop, ui.send]),
    ]);
    ui.hint = h('p', { class: 'composer-hint', id: 'composer-hint' }, [
      icon('shield'),
      h('span', { text: 'Runs locally – answers can take a little longer. Nothing leaves this computer.' }),
    ]);
    ui.examples = h('div', { class: 'examples', role: 'list', 'aria-label': 'Example prompts' });
    ui.dock = h('div', { class: 'composer-dock' }, [ui.notice, ui.form, ui.hint, ui.examples]);
    ui.status = h('div', { class: 'visually-hidden', 'aria-live': 'polite', 'aria-atomic': 'true' });
    ui.root.append(ui.heading, ui.scroll, ui.jump, ui.dock, ui.status);

    ui.form.addEventListener('submit', (ev) => {
      ev.preventDefault();
      send();
    });
    ui.input.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing && ev.keyCode !== 229) {
        ev.preventDefault();
        send();
      } else if (ev.key === 'Escape' && S.stream) {
        ev.preventDefault();
        stopStream();
      }
    });
    ui.input.addEventListener('input', () => {
      autosize();
      updateSend();
      window.clearTimeout(S.draftTimer);
      S.draftTimer = window.setTimeout(saveDraft, 300);
    });
    ui.scroll.addEventListener('scroll', onScroll, { passive: true });
    return ui.root;
  }

  function autosize() {
    const el = S.ui.input;
    el.style.setProperty('height', 'auto');
    const max = Math.max(120, Math.round(window.innerHeight * 0.4));
    el.style.setProperty('height', Math.min(el.scrollHeight, max) + 'px');
    el.classList.toggle('is-scrolling', el.scrollHeight > max);
  }

  function updateSend() {
    const ui = S.ui;
    const busy = Boolean(S.stream);
    ui.send.hidden = busy;
    ui.stop.hidden = !busy;
    ui.send.disabled = busy || !ui.input.value.trim() || !LR.has('chat');
    ui.stop.disabled = busy && S.stream.stopRequested;
  }

  function onScroll() {
    const el = S.ui.scroll;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    S.stick = distance < 80;
    const pane = currentPane();
    S.ui.jump.hidden = S.stick || !pane || !pane.list.children.length;
  }

  function scrollToBottom(force) {
    const el = S.ui.scroll;
    if (!el || (!force && !S.stick)) return;
    el.scrollTop = el.scrollHeight;
    S.stick = true;
    S.ui.jump.hidden = true;
  }

  /** Greeting, mode toggle, placeholder, examples and empty state. */
  function renderChrome() {
    const ui = S.ui;
    if (!ui.root) return;
    const pane = currentPane();
    const mode = paneMode(pane);
    const hasMessages = Boolean(pane && pane.list.children.length);
    const available = LR.has('chat');
    ui.root.classList.toggle('is-empty', !hasMessages);
    ui.root.classList.toggle('is-code', mode === 'code');
    ui.welcome.hidden = hasMessages || !available;
    ui.unavailable.hidden = available;
    ui.dock.hidden = !available;
    ui.greet.textContent = mode === 'code' ? "Let's write some code" : greeting();
    ui.greetSub.textContent = mode === 'code'
      ? 'Ask about code, paste an error or describe what you want to build.'
      : 'What can I help you with today?';
    ui.input.placeholder = mode === 'code' ? 'Ask a coding question…' : hasMessages ? 'Reply to LowRAM AI…' : 'How can I help you today?';
    ui.mode.set(mode);
    const fixed = Boolean(pane && pane.id !== null);
    ui.mode.setDisabled(fixed, fixed ? 'The mode is fixed per conversation – start a new chat to switch.' : null);
    ui.examples.hidden = hasMessages;
    if (!hasMessages) {
      ui.examples.replaceChildren(...EXAMPLES[mode].map((ex) => h('button', {
        type: 'button',
        class: 'example',
        role: 'listitem',
        onclick: () => {
          ui.input.value = ex.prompt;
          autosize();
          updateSend();
          ui.input.focus();
        },
      }, [h('span', { class: 'example-title', text: ex.title }), h('span', { class: 'example-text', text: ex.prompt })])));
    }
    const chat = pane && pane.chat;
    const title = chat ? chat.title || 'Untitled chat' : mode === 'code' ? 'New code chat' : 'New chat';
    if (LR.currentRoute && LR.currentRoute() === 'chat') LR.setTitle(title);
    updateSend();
    renderHistoryState();
  }

  function renderUnavailable() {
    const ui = S.ui;
    if (ui.unavailable.childElementCount) return;
    ui.unavailable.append(LR.empty({
      icon: 'chat',
      title: 'Chat is not available in this build',
      text: 'This version of LowRAM AI was built without the chat module. You can still use the task queue and the other tools.',
      actions: [LR.button({ text: 'Open the task queue', variant: 'primary', icon: 'tasks', onclick: () => LR.go('#/tasks') })],
      cls: 'empty-page',
    }));
  }

  /* ------------------------------------------------------------ drafts */

  function saveDraft() {
    const value = S.ui.input ? S.ui.input.value : '';
    if (value.trim()) LR.store.set(draftKey(S.currentId), value);
    else LR.store.remove(draftKey(S.currentId));
  }

  function loadDraft() {
    S.ui.input.value = LR.store.get(draftKey(S.currentId)) || '';
    autosize();
    updateSend();
  }

  /* ------------------------------------------------------------ open / new */

  function showPane(pane) {
    S.ui.thread.replaceChildren(pane.list);
    renderChrome();
    S.stick = true;
    window.requestAnimationFrame(() => scrollToBottom(true));
  }

  function newChat(mode) {
    saveDraft();
    if (mode === 'chat' || mode === 'code') {
      S.newMode = mode;
      LR.store.set('lowram.chatMode', mode);
    }
    S.currentId = null;
    if (!S.newPane || S.newPane.list.children.length) S.newPane = makePane(null);
    showPane(S.newPane);
    loadDraft();
    clearNotice();
  }

  async function openChat(id) {
    if (S.currentId !== id) saveDraft();
    S.currentId = id;
    let pane = S.panes.get(id);
    if (!pane) {
      pane = makePane(id, chatById(id));
      S.panes.set(id, pane);
    }
    showPane(pane);
    loadDraft();
    clearNotice();
    if (!pane.loaded) await loadPane(pane);
  }

  async function loadPane(pane) {
    if (pane.loading) return;
    pane.loading = true;
    pane.list.replaceChildren(h('li', { class: 'msg-skeleton' }, LR.skeleton(4)));
    S.ui.welcome.hidden = true;
    try {
      const data = await LR.get('/api/chats/' + encodeURIComponent(pane.id));
      pane.chat = data.chat || pane.chat;
      const messages = Array.isArray(data.messages) ? data.messages : [];
      pane.list.replaceChildren();
      let lastUser = '';
      let pendingReload = false;
      for (const m of messages) {
        if (m.role === 'user') {
          lastUser = LR.str(m.content);
          pane.list.append(userMessage(lastUser));
        } else {
          const a = assistantMessage();
          const retryText = lastUser;
          a.setRetry(() => send(retryText));
          a.finish(m);
          if (m.status === 'streaming') pendingReload = true;
          pane.list.append(a.li);
        }
      }
      pane.loaded = true;
      if (pendingReload && !(S.stream && S.stream.chatId === pane.id)) {
        window.setTimeout(() => {
          pane.loaded = false;
          if (S.currentId === pane.id) loadPane(pane);
        }, 3000);
      }
    } catch (err) {
      pane.list.replaceChildren();
      if (err.status === 404) {
        S.panes.delete(pane.id);
        LR.toast('That chat no longer exists.', 'info');
        LR.go('#/chat/new');
        return;
      }
      pane.list.append(h('li', { class: 'msg' }, LR.callout({
        tone: 'error',
        title: 'Could not load this chat',
        text: err.message,
        actions: [LR.button({ text: 'Try again', icon: 'retry', size: 'sm', onclick: () => loadPane(pane) })],
      })));
    } finally {
      pane.loading = false;
      if (S.currentId === pane.id) {
        renderChrome();
        window.requestAnimationFrame(() => scrollToBottom(true));
      }
    }
  }

  /* ------------------------------------------------------------ messages */

  function userMessage(content) {
    return h('li', { class: 'msg msg-user' }, [
      h('h2', { class: 'visually-hidden', text: 'You said:' }),
      h('div', { class: 'bubble', text: content }),
      h('div', { class: 'msg-actions' }, [
        LR.copyButton({ getText: () => content, iconOnly: true, cls: 'icon-btn', ariaLabel: 'Copy your message' }),
      ]),
    ]);
  }

  function assistantMessage() {
    const thinkId = LR.uid('think');
    const thinkLabel = h('span', { class: 'thinking-label', text: 'Thinking…' });
    const thinkBody = h('div', { class: 'thinking-body md', id: thinkId, hidden: true });
    const thinkToggle = h('button', { type: 'button', class: 'thinking-toggle', 'aria-expanded': 'false', 'aria-controls': thinkId }, [
      h('span', { class: 'thinking-icon' }, icon('sparkle')),
      thinkLabel,
      icon('chevron-down', 'thinking-chevron'),
    ]);
    const think = h('div', { class: 'thinking', hidden: true }, [thinkToggle, thinkBody]);
    const body = h('div', { class: 'msg-body md' });
    const waitText = h('span', { class: 'wait-text', text: 'Working on it…' });
    const wait = h('div', { class: 'msg-wait' }, [h('span', { class: 'typing', 'aria-hidden': 'true' }, [h('i'), h('i'), h('i')]), waitText]);
    const extra = h('div', { class: 'msg-extra' });
    const meta = h('span', { class: 'msg-meta' });
    let answerText = '';
    const copyBtn = LR.copyButton({ getText: () => answerText, iconOnly: true, cls: 'icon-btn', ariaLabel: 'Copy answer' });
    const retryBtn = h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Retry', title: 'Retry', hidden: true }, icon('retry'));
    const actions = h('div', { class: 'msg-actions', hidden: true }, [copyBtn, retryBtn, meta]);
    const li = h('li', { class: 'msg msg-assistant' }, [
      h('h2', { class: 'visually-hidden', text: 'LowRAM AI said:' }),
      think,
      body,
      wait,
      extra,
      actions,
    ]);
    const renderer = LR.md.createRenderer(body);
    const thinkRenderer = LR.md.createRenderer(thinkBody);
    let userToggled = false;
    let retryFn = null;
    let frame = 0;
    let pending = null;
    let thinkStart = 0;
    let thinkSecs = null;
    const started = Date.now();
    const waitTimer = window.setInterval(() => {
      const sec = (Date.now() - started) / 1000;
      if (sec > 6) waitText.textContent = (statusText || 'Still working – local models can take a moment') + ' · ' + fmt.clock(Date.now() - started);
    }, 1000);

    function setExpanded(open) {
      thinkToggle.setAttribute('aria-expanded', String(open));
      thinkBody.hidden = !open;
      think.classList.toggle('is-open', open);
    }
    thinkToggle.addEventListener('click', () => {
      userToggled = true;
      setExpanded(thinkBody.hidden);
    });
    retryBtn.addEventListener('click', () => { if (retryFn) retryFn(); });

    function apply(reasoning, answer, streaming) {
      if (reasoning && reasoning.trim()) {
        if (!thinkStart) thinkStart = Date.now();
        think.hidden = false;
        thinkRenderer.render(reasoning, streaming && !answer);
        const thinking = streaming && !answer;
        think.classList.toggle('is-active', thinking);
        if (thinking) {
          thinkLabel.textContent = 'Thinking…';
          if (!userToggled) setExpanded(true);
        } else {
          if (thinkSecs === null && streaming) thinkSecs = (Date.now() - thinkStart) / 1000;
          thinkLabel.textContent = thinkSecs !== null && thinkSecs >= 1 ? 'Thought for ' + fmt.duration(thinkSecs) : 'Thoughts';
          if (!userToggled) setExpanded(false);
        }
      }
      if (answer || !streaming) {
        answerText = answer;
        renderer.render(answer, streaming);
      }
      const waiting = streaming && !answer && !(reasoning && reasoning.trim());
      wait.hidden = !waiting;
      li.classList.toggle('is-streaming', streaming);
    }

    let statusText = '';
    return {
      li,
      setRetry(fn) { retryFn = fn; },
      /** Backend progress note ("Loading the model…") shown until tokens arrive. */
      status(text) {
        statusText = text;
        if (text) waitText.textContent = text;
      },
      /** Throttled to one render per animation frame. */
      stream(reasoning, answer) {
        pending = [reasoning, answer];
        if (frame) return;
        frame = window.requestAnimationFrame(() => {
          frame = 0;
          const p = pending;
          pending = null;
          if (p) apply(p[0], p[1], true);
          scrollToBottom(false);
        });
      },
      finish(message, extraReasoning) {
        window.clearInterval(waitTimer);
        if (frame) { window.cancelAnimationFrame(frame); frame = 0; }
        const m = message || {};
        const split = LR.md.splitThinking(LR.str(m.content));
        const reasoning = (extraReasoning || '') + split.reasoning;
        apply(reasoning, split.answer, false);
        wait.hidden = true;
        actions.hidden = false;
        const parts = [];
        if (m.model) parts.push(LR.modelName(m.model));
        if (LR.isNum(m.duration_s)) parts.push(fmt.duration(m.duration_s));
        meta.textContent = parts.join(' · ');
        extra.replaceChildren();
        li.classList.remove('is-error', 'is-stopped');
        if (m.status === 'stopped') {
          li.classList.add('is-stopped');
          extra.append(h('p', { class: 'msg-note' }, [icon('stop'), h('span', { text: split.answer ? 'Stopped – the partial answer was kept.' : 'Stopped before an answer arrived.' })]));
          retryBtn.hidden = !retryFn;
        } else if (m.status === 'error') {
          this.fail(m.error || 'The answer could not be completed.');
        } else if (m.status === 'streaming') {
          extra.append(h('p', { class: 'msg-note' }, [LR.spinner(), h('span', { text: 'Still generating… this view refreshes by itself.' })]));
          actions.hidden = true;
        } else {
          retryBtn.hidden = true;
          if (!split.answer) extra.append(h('p', { class: 'msg-note', text: 'The model returned an empty answer.' }));
        }
        scrollToBottom(false);
      },
      fail(error, partial) {
        window.clearInterval(waitTimer);
        if (frame) { window.cancelAnimationFrame(frame); frame = 0; }
        if (partial !== undefined) apply(partial[0], partial[1], false);
        else li.classList.remove('is-streaming');
        wait.hidden = true;
        li.classList.add('is-error');
        const modelIssue = /model|engine|llama|server|start/i.test(LR.str(error));
        const btns = [];
        if (retryFn) btns.push(LR.button({ text: 'Retry', icon: 'retry', size: 'sm', variant: 'secondary', onclick: () => retryFn() }));
        if (modelIssue && LR.has('engine')) btns.push(LR.button({ text: 'Open Models', icon: 'sliders', size: 'sm', variant: 'ghost', onclick: () => LR.go('#/settings') }));
        extra.replaceChildren(LR.callout({ tone: 'error', title: 'The answer could not be completed', text: LR.str(error, 'Unknown error.'), actions: btns, role: 'alert' }));
        actions.hidden = !answerText;
        retryBtn.hidden = true;
        scrollToBottom(false);
      },
    };
  }

  /* ------------------------------------------------------------ notices */

  function clearNotice() {
    if (S.ui.notice) S.ui.notice.replaceChildren();
  }

  function showNotice(err) {
    const busy = err.status === 409;
    const noModel = err.status === 400 || /model|engine|llama|not running|start/i.test(err.message);
    let title = 'Could not send your message';
    if (err.network) title = 'LowRAM AI is not reachable';
    else if (busy && !noModel) title = 'LowRAM AI is busy right now';
    else if (noModel) title = 'No model is ready yet';
    const text = err.message + (busy ? ' Your message is still in the box – send it again in a moment.' : '');
    const actions = [];
    if (LR.has('engine') && (noModel || busy)) actions.push(LR.button({ text: 'Open Models', icon: 'sliders', size: 'sm', variant: 'primary', onclick: () => LR.go('#/settings') }));
    actions.push(LR.button({ text: 'Try again', icon: 'retry', size: 'sm', onclick: () => send() }));
    actions.push(LR.button({ text: 'Dismiss', size: 'sm', variant: 'ghost', onclick: clearNotice }));
    S.ui.notice.replaceChildren(LR.callout({ tone: busy ? 'warn' : 'error', title, text, actions, role: 'alert', cls: 'notice-pop' }));
  }

  /* ------------------------------------------------------------ sending */

  async function send(retryText) {
    const ui = S.ui;
    if (S.stream || !LR.has('chat')) return;
    const fromComposer = typeof retryText !== 'string';
    const content = (fromComposer ? ui.input.value : retryText).trim();
    if (!content) return;
    clearNotice();
    let pane = currentPane();
    const restore = fromComposer ? ui.input.value : null;
    if (fromComposer) {
      ui.input.value = '';
      autosize();
      LR.store.remove(draftKey(S.currentId));
    }
    const ctx = { chatId: S.currentId, stopRequested: false, gotStart: false, done: false, tokens: '', reasoning: '', controller: new AbortController() };
    S.stream = ctx;
    updateSend();

    if (ctx.chatId === null) {
      try {
        const data = await LR.post('/api/chats', { mode: S.newMode });
        const chat = data.chat;
        pane = S.newPane;
        pane.id = chat.id;
        pane.chat = chat;
        pane.loaded = true;
        S.panes.set(chat.id, pane);
        S.newPane = null;
        S.currentId = chat.id;
        ctx.chatId = chat.id;
        S.chats = [chat, ...S.chats.filter((c) => c.id !== chat.id)];
        window.history.replaceState(null, '', '#/chat/' + chat.id);
      } catch (err) {
        S.stream = null;
        if (restore !== null && !ui.input.value) { ui.input.value = restore; autosize(); }
        showNotice(err);
        updateSend();
        return;
      }
    }

    const userLi = userMessage(content);
    const a = assistantMessage();
    a.setRetry(() => send(content));
    pane.list.append(userLi, a.li);
    renderChrome();
    scrollToBottom(true);
    ui.status.textContent = 'LowRAM AI is answering…';

    const view = () => {
      const split = LR.md.splitThinking(ctx.tokens);
      return [ctx.reasoning + split.reasoning, split.answer];
    };
    const percent = LR.state.settings && LR.isNum(LR.state.settings.budget_percent) ? LR.state.settings.budget_percent : undefined;
    try {
      await LR.stream('/api/chats/' + encodeURIComponent(ctx.chatId) + '/messages', { content, percent }, {
        signal: ctx.controller.signal,
        onEvent(ev) {
          if (ev.type === 'ping') return;
          if (ev.type === 'start') {
            ctx.gotStart = true;
            if (ev.chat && ev.chat.id === ctx.chatId) {
              pane.chat = ev.chat;
              S.chats = [ev.chat, ...S.chats.filter((c) => c.id !== ev.chat.id)];
              renderHistory();
              if (S.currentId === ctx.chatId) renderChrome();
            }
            LR.emit('status-refresh');
          } else if (ev.type === 'status') {
            ctx.gotStart = true;
            a.status(LR.str(ev.text));
          } else if (ev.type === 'token') {
            ctx.gotStart = true;
            ctx.tokens += LR.str(ev.text);
            const v = view();
            a.stream(v[0], v[1]);
          } else if (ev.type === 'reasoning') {
            ctx.gotStart = true;
            ctx.reasoning += LR.str(ev.text);
            const v = view();
            a.stream(v[0], v[1]);
          } else if (ev.type === 'done') {
            ctx.done = true;
            const msg = ev.message || { content: ctx.tokens, status: ctx.stopRequested ? 'stopped' : 'complete' };
            a.finish(msg, ctx.reasoning);
          } else if (ev.type === 'error') {
            ctx.done = true;
            const msg = ev.message;
            const split = LR.md.splitThinking(msg && typeof msg.content === 'string' ? msg.content : ctx.tokens);
            a.fail(ev.error || 'The answer could not be completed.', [ctx.reasoning + split.reasoning, split.answer]);
          }
        },
      });
      if (!ctx.done) {
        const v = view();
        if (ctx.stopRequested) a.finish({ content: ctx.tokens, status: 'stopped' }, ctx.reasoning);
        else a.fail('The answer ended unexpectedly. The model server may have stopped.', v);
      }
    } catch (err) {
      if (err && err.name === 'AbortError') {
        a.finish({ content: ctx.tokens, status: 'stopped' }, ctx.reasoning);
      } else if (!ctx.gotStart && err.status) {
        userLi.remove();
        a.li.remove();
        if (restore !== null && !ui.input.value) {
          ui.input.value = restore;
          autosize();
        }
        showNotice(err);
      } else {
        a.fail(err.message, view());
      }
    } finally {
      S.stream = null;
      ui.status.textContent = 'Answer finished.';
      if (S.currentId === ctx.chatId) renderChrome();
      updateSend();
      refreshList();
      LR.emit('status-refresh');
      window.setTimeout(() => LR.emit('status-refresh'), 900);
    }
  }

  async function stopStream() {
    const ctx = S.stream;
    if (!ctx || ctx.stopRequested) return;
    ctx.stopRequested = true;
    updateSend();
    if (ctx.chatId !== null) {
      try { await LR.post('/api/chats/' + encodeURIComponent(ctx.chatId) + '/stop', {}); } catch (_) { /* aborting below */ }
    }
    window.setTimeout(() => {
      if (S.stream === ctx && !ctx.done) ctx.controller.abort();
    }, 4000);
  }

  /* ------------------------------------------------------------ history (sidebar) */

  async function refreshList() {
    if (!LR.has('chat')) {
      S.chats = [];
      renderHistory();
      return;
    }
    try {
      const data = await LR.get('/api/chats');
      S.chats = Array.isArray(data.chats) ? data.chats : [];
      S.listLoaded = true;
      for (const c of S.chats) {
        const pane = S.panes.get(c.id);
        if (pane) pane.chat = c;
      }
    } catch (_) {
      if (!S.listLoaded) S.listLoaded = true;
    }
    renderHistory();
    if (S.ui.root && LR.currentRoute && LR.currentRoute() === 'chat') renderChrome();
  }

  function groupOf(chat) {
    const d = fmt.parseTime(chat.updated_at || chat.created_at);
    if (!d) return 'Earlier';
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const t = d.getTime();
    if (t >= today.getTime()) return 'Today';
    if (t >= today.getTime() - 86400000) return 'Yesterday';
    return 'Earlier';
  }

  function renderHistory() {
    const el = S.historyEl;
    if (!el) return;
    closeMenu();
    if (!LR.has('chat')) {
      el.replaceChildren(h('p', { class: 'history-empty', text: 'Chat history appears here.' }));
      return;
    }
    if (!S.listLoaded) {
      el.replaceChildren(LR.skeleton(5, 'history-skeleton'));
      return;
    }
    if (!S.chats.length) {
      el.replaceChildren(h('p', { class: 'history-empty', text: 'Your conversations will appear here.' }));
      return;
    }
    const groups = new Map([['Today', []], ['Yesterday', []], ['Earlier', []]]);
    for (const c of S.chats) groups.get(groupOf(c)).push(c);
    const out = [];
    for (const [label, chats] of groups) {
      if (!chats.length) continue;
      const id = LR.uid('hist');
      out.push(h('div', { class: 'history-group' }, [
        h('h3', { class: 'history-label', id, text: label }),
        h('ul', { class: 'history-list', 'aria-labelledby': id }, chats.map(historyItem)),
      ]));
    }
    el.replaceChildren(...out);
    renderHistoryState();
  }

  function historyItem(chat) {
    const title = chat.title || 'Untitled chat';
    const link = h('a', { class: 'hist-item', href: '#/chat/' + chat.id, dataset: { id: String(chat.id) } }, [
      chat.mode === 'code' ? icon('code', 'hist-mode') : null,
      h('span', { class: 'hist-title', text: title }),
      h('span', { class: 'hist-live', 'aria-hidden': 'true' }),
    ]);
    const more = h('button', {
      type: 'button',
      class: 'hist-more',
      'aria-label': 'Options for “' + title + '”',
      'aria-haspopup': 'menu',
      'aria-expanded': 'false',
    }, icon('more'));
    more.addEventListener('click', (ev) => {
      ev.stopPropagation();
      if (S.menu && S.menu.button === more) closeMenu();
      else openMenu(more, chat);
    });
    return h('li', { class: 'hist-li', dataset: { id: String(chat.id) } }, [link, more]);
  }

  function renderHistoryState() {
    if (!S.historyEl) return;
    const onChat = LR.currentRoute && LR.currentRoute() === 'chat';
    for (const a of S.historyEl.querySelectorAll('.hist-item')) {
      const id = Number(a.dataset.id);
      if (onChat && id === S.currentId) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
      a.classList.toggle('is-live', Boolean(S.stream && S.stream.chatId === id));
    }
  }

  function closeMenu(focusButton) {
    const m = S.menu;
    if (!m) return;
    S.menu = null;
    m.el.remove();
    m.button.setAttribute('aria-expanded', 'false');
    document.removeEventListener('mousedown', m.outside, true);
    if (focusButton && m.button.isConnected) m.button.focus();
  }

  function openMenu(button, chat) {
    closeMenu();
    const items = [
      h('button', { type: 'button', role: 'menuitem', class: 'menu-item', onclick: () => { closeMenu(); startRename(chat); } }, [icon('pencil'), h('span', { text: 'Rename' })]),
      h('button', { type: 'button', role: 'menuitem', class: 'menu-item menu-danger', onclick: () => { closeMenu(); deleteChat(chat); } }, [icon('trash'), h('span', { text: 'Delete' })]),
    ];
    const el = h('div', { class: 'menu', role: 'menu', 'aria-label': 'Chat options' }, items);
    el.addEventListener('keydown', (ev) => {
      const idx = items.indexOf(document.activeElement);
      if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
        ev.preventDefault();
        const next = items[(idx + (ev.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length];
        next.focus();
      } else if (ev.key === 'Escape') {
        ev.preventDefault();
        closeMenu(true);
      } else if (ev.key === 'Tab') {
        closeMenu();
      }
    });
    const outside = (ev) => {
      if (!el.contains(ev.target) && ev.target !== button && !button.contains(ev.target)) closeMenu();
    };
    document.addEventListener('mousedown', outside, true);
    button.parentElement.append(el);
    button.setAttribute('aria-expanded', 'true');
    S.menu = { el, button, outside };
    items[0].focus();
  }

  function startRename(chat) {
    const li = S.historyEl.querySelector('.hist-li[data-id="' + CSS.escape(String(chat.id)) + '"]');
    if (!li) return;
    const input = h('input', { class: 'input hist-rename', type: 'text', maxlength: '200', 'aria-label': 'Chat title' });
    input.value = chat.title || '';
    li.replaceChildren(input);
    input.focus();
    input.select();
    let finished = false;
    const finish = async (save) => {
      if (finished) return;
      finished = true;
      const title = input.value.trim();
      if (save && title && title !== chat.title) {
        try {
          const data = await LR.post('/api/chats/' + encodeURIComponent(chat.id) + '/rename', { title });
          Object.assign(chat, data.chat || { title });
          LR.toast('Chat renamed');
        } catch (err) {
          LR.toast('Could not rename: ' + err.message, 'error');
        }
      }
      renderHistory();
      if (S.currentId === chat.id) renderChrome();
      const link = S.historyEl.querySelector('.hist-item[data-id="' + CSS.escape(String(chat.id)) + '"]');
      if (link) link.focus();
    };
    input.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); finish(true); }
      if (ev.key === 'Escape') { ev.preventDefault(); finish(false); }
    });
    input.addEventListener('blur', () => finish(true));
  }

  async function deleteChat(chat) {
    const ok = await LR.confirm({
      title: 'Delete this chat?',
      message: '“' + (chat.title || 'Untitled chat') + '” will be removed from this computer. This cannot be undone.',
      confirmText: 'Delete',
      danger: true,
    });
    if (!ok) return;
    try {
      await LR.post('/api/chats/' + encodeURIComponent(chat.id) + '/delete', {});
      S.chats = S.chats.filter((c) => c.id !== chat.id);
      S.panes.delete(chat.id);
      LR.store.remove(draftKey(chat.id));
      LR.toast('Chat deleted');
      renderHistory();
      if (S.currentId === chat.id) LR.go('#/chat/new');
    } catch (err) {
      LR.toast('Could not delete: ' + err.message, 'error');
    }
  }

  /* ------------------------------------------------------------ registration */

  LR.registerView('chat', {
    title: 'Chat',
    create,
    show(params) {
      if (!LR.has('chat')) {
        renderUnavailable();
        S.currentId = null;
        if (!S.newPane) S.newPane = makePane(null);
        showPane(S.newPane);
        LR.setTitle('Chat');
        return;
      }
      const first = params[0];
      if (first === 'new') {
        newChat(params[1]);
      } else if (first && /^\d+$/.test(first)) {
        const id = Number(first);
        if (id !== S.currentId || !S.panes.has(id)) openChat(id);
        else renderChrome();
      } else if (S.currentId !== null && S.panes.has(S.currentId)) {
        renderChrome();
      } else {
        newChat();
      }
      window.requestAnimationFrame(() => {
        if (window.matchMedia('(min-width: 900px)').matches && !S.ui.dock.hidden) S.ui.input.focus({ preventScroll: true });
      });
    },
    hide() {
      saveDraft();
      renderHistoryState();
    },
  });

  LR.chat = {
    mountHistory(el) {
      S.historyEl = el;
      renderHistory();
    },
    refreshList,
    renderHistory,
    get streaming() { return Boolean(S.stream); },
  };
})();
