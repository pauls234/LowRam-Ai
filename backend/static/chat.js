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
      { title: LR.t('Explain an idea'), prompt: LR.t('Explain how a hash map works, with a small example.') },
      { title: LR.t('Write a friendly email'), prompt: LR.t('Help me write a short, friendly email declining a meeting next week.') },
      { title: LR.t('Plan a weekend'), prompt: LR.t('Plan a relaxed weekend in Lisbon on a small budget.') },
      { title: LR.t('Weigh pros and cons'), prompt: LR.t('What are the pros and cons of running AI models locally?') },
    ],
    code: [
      { title: LR.t('Write a function'), prompt: LR.t('Write a Python function that removes duplicate lines from a file but keeps the order.') },
      { title: LR.t('Explain a regex'), prompt: LR.t('Explain this regular expression step by step: ^(?=.*\\d)[A-Za-z\\d]{8,}$') },
      { title: LR.t('Find the bug'), prompt: LR.t('Why does `for i in range(len(items)): items.pop(i)` fail in Python?') },
      { title: LR.t('Shell one-liner'), prompt: LR.t('Give me a shell command that finds the 10 largest files in a folder.') },
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
    attachments: [],
    attachTimer: 0,
    query: '',
    results: null,
    searchTimer: 0,
    searchSeq: 0,
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
    if (hour >= 5 && hour < 12) return LR.t('Good morning');
    if (hour >= 12 && hour < 18) return LR.t('Good afternoon');
    return LR.t('Good evening');
  }

  function paneMode(pane) {
    return pane && pane.chat && pane.chat.mode === 'code' ? 'code' : pane && pane.id === null ? S.newMode : 'chat';
  }

  function makePane(id, chat) {
    return { id, chat: chat || null, list: h('ol', { class: 'messages', 'aria-label': LR.t('Conversation') }), loaded: id === null, loading: false, count: 0 };
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
    ui.jump = h('button', { type: 'button', class: 'jump-latest', 'aria-label': LR.t('Jump to the latest message'), hidden: true, onclick: () => scrollToBottom(true) }, icon('arrow-down'));
    ui.notice = h('div', { class: 'chat-notice' });
    ui.input = h('textarea', {
      class: 'composer-input',
      id: 'composer-input',
      rows: '1',
      'aria-label': LR.t('Message LowRAM AI'),
      'aria-describedby': 'composer-hint',
      autocomplete: 'off',
      spellcheck: 'true',
    });
    ui.mode = LR.segmented({
      label: LR.t('Conversation mode'),
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
    ui.send = h('button', { type: 'submit', class: 'send-btn', 'aria-label': LR.t('Send message'), title: LR.t('Send (Enter)'), disabled: true }, icon('arrow-up'));
    ui.stop = h('button', { type: 'button', class: 'stop-btn', 'aria-label': LR.t('Stop generating'), title: LR.t('Stop (Esc)'), hidden: true, onclick: stopStream }, icon('stop'));
    ui.fileInput = h('input', {
      type: 'file', multiple: true, class: 'visually-hidden', tabindex: '-1', 'aria-hidden': 'true',
      accept: '.txt,.md,.markdown,.csv,.tsv,.json,.yaml,.yml,.toml,.ini,.xml,.html,.css,.js,.mjs,.ts,.tsx,.jsx,.py,.rb,.go,.rs,.java,.kt,.swift,.c,.h,.cpp,.hpp,.cs,.php,.sh,.zsh,.sql,.log,.env.example,text/*',
    });
    ui.fileInput.addEventListener('change', () => {
      addFiles(ui.fileInput.files);
      ui.fileInput.value = '';
    });
    ui.attach = h('button', { type: 'button', class: 'icon-btn attach-btn', 'aria-label': LR.t('Attach text or code files'), title: LR.t('Attach files (text or code, up to 200 KB)'), onclick: () => ui.fileInput.click() }, icon('paperclip'));
    ui.atts = h('ul', { class: 'att-list composer-atts', 'aria-label': LR.t('Files to send'), hidden: true });
    ui.attachMsg = h('p', { class: 'attach-msg', role: 'status', hidden: true });
    ui.form = h('form', { class: 'composer', novalidate: true }, [
      ui.atts,
      ui.input,
      h('div', { class: 'composer-bar' }, [ui.attach, ui.mode.el, buildKnowledgePicker(), h('span', { class: 'composer-spacer' }), buildBigJobButton(), ui.stop, ui.send]),
      ui.fileInput,
    ]);
    let dragDepth = 0;
    ui.form.addEventListener('dragenter', (ev) => {
      if (!ev.dataTransfer || ![...ev.dataTransfer.types].includes(LR.t('Files'))) return;
      ev.preventDefault();
      dragDepth++;
      ui.form.classList.add('is-drop');
    });
    ui.form.addEventListener('dragover', (ev) => {
      if (ev.dataTransfer && [...ev.dataTransfer.types].includes(LR.t('Files'))) ev.preventDefault();
    });
    ui.form.addEventListener('dragleave', () => {
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) ui.form.classList.remove('is-drop');
    });
    ui.form.addEventListener('drop', (ev) => {
      if (!ev.dataTransfer || !ev.dataTransfer.files.length) return;
      ev.preventDefault();
      dragDepth = 0;
      ui.form.classList.remove('is-drop');
      addFiles(ev.dataTransfer.files);
    });
    ui.hint = h('p', { class: 'composer-hint', id: 'composer-hint' }, [
      icon('shield'),
      h('span', { text: LR.t('Runs locally – answers can take a little longer. Nothing leaves this computer.') }),
    ]);
    ui.examples = h('div', { class: 'examples', role: 'list', 'aria-label': LR.t('Example prompts') });
    ui.dock = h('div', { class: 'composer-dock' }, [ui.notice, ui.form, ui.attachMsg, ui.hint, ui.examples]);
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

  /* ------------------------------------------------------------ knowledge picker (v0.3.1) */

  function buildKnowledgePicker() {
    const ui = S.ui;
    const id = 'composer-kb';
    ui.kbSelect = h('select', { class: 'kb-select', id });
    ui.kbSelect.addEventListener('change', () => {
      S.collectionId = ui.kbSelect.value || null;
      if (S.collectionId) LR.store.set('lowram.chatCollection', S.collectionId);
      else LR.store.remove('lowram.chatCollection');
      ui.kbWrap.classList.toggle('is-on', Boolean(S.collectionId));
    });
    ui.kbWrap = h('div', { class: 'kb-picker', hidden: true }, [
      icon('book'),
      h('label', { class: 'kb-label', for: id, text: LR.t('Answer with knowledge') + ':' }),
      ui.kbSelect,
    ]);
    S.collectionId = LR.store.get('lowram.chatCollection') || null;
    LR.on('knowledge', renderKnowledgePicker);
    window.setTimeout(renderKnowledgePicker, 0);
    return ui.kbWrap;
  }

  function renderKnowledgePicker() {
    const ui = S.ui;
    const kb = LR.knowledge;
    const cols = kb && kb.available && Array.isArray(kb.collections) ? kb.collections : [];
    if (!ui.kbWrap) return;
    ui.kbWrap.hidden = !cols.length;
    if (S.collectionId && !cols.some((c) => String(c.id) === String(S.collectionId))) S.collectionId = null;
    ui.kbSelect.replaceChildren(
      h('option', { value: '', text: LR.t('No knowledge') }),
      ...cols.map((c) => h('option', { value: String(c.id), text: LR.str(c.name, LR.t('Untitled')) })),
    );
    ui.kbSelect.value = S.collectionId ? String(S.collectionId) : '';
    ui.kbWrap.classList.toggle('is-on', Boolean(S.collectionId));
  }

  /* ------------------------------------------------------------ long-chat memory + big jobs (v0.4) */

  /** "Older messages were summarised" note with the summary behind a disclosure. */
  function ensureSummary(pane, summary) {
    const text = LR.str(summary).trim();
    let li = pane.list.querySelector(':scope > .msg-summary');
    if (!text) {
      if (li) li.remove();
      return;
    }
    if (!li) {
      li = h('li', { class: 'msg msg-summary' });
      pane.list.prepend(li);
    } else if (li.dataset.text === text) return;
    li.dataset.text = text;
    li.replaceChildren(h('details', { class: 'summary-note' }, [
      h('summary', null, [icon('info'), h('span', { text: LR.t('Older messages were summarised') }), icon('chevron-down', 'summary-chevron')]),
      h('div', { class: 'summary-body' }, [
        h('p', { class: 'summary-text', text }),
        h('p', { class: 'muted small', text: LR.t('The model keeps this summary instead of the older messages, so even long chats fit into memory.') }),
      ]),
    ]));
  }

  function buildBigJobButton() {
    const ui = S.ui;
    ui.bigJob = h('button', {
      type: 'button',
      class: 'icon-btn bigjob-btn',
      'aria-label': LR.t('Run as a big job'),
      title: LR.t('Run as a big job – split into subtasks that run one after another'),
      hidden: true,
      onclick: () => { if (LR.bigJob) LR.bigJob.open(ui.input.value.trim()); },
    }, icon('merge'));
    return ui.bigJob;
  }

  function sourcesBlock(sources) {
    if (!Array.isArray(sources) || !sources.length) return null;
    const id = LR.uid('src');
    return h('section', { class: 'msg-sources', 'aria-labelledby': id }, [
      h('h3', { class: 'msg-sources-title', id }, [icon('book'), h('span', { text: LR.t('Sources') + ' (' + sources.length + ')' })]),
      h('ol', { class: 'msg-sources-list' }, sources.map((src) => h('li', { class: 'msg-source' }, [
        h('p', { class: 'msg-source-doc', text: LR.str(src && src.document, LR.t('(unnamed file)')) }),
        src && src.snippet ? h('p', { class: 'msg-source-snippet', text: LR.str(src.snippet).slice(0, 400) }) : null,
      ]))),
    ]);
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
    ui.send.disabled = busy || !(ui.input.value.trim() || S.attachments.length) || !LR.has('chat');
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
    ui.greet.textContent = mode === 'code' ? LR.t("Let's write some code") : greeting();
    ui.greetSub.textContent = mode === 'code'
      ? LR.t('Ask about code, paste an error or describe what you want to build.')
      : LR.t('What can I help you with today?');
    ui.input.placeholder = mode === 'code' ? LR.t('Ask a coding question…') : hasMessages ? LR.t('Reply to LowRAM AI…') : LR.t('How can I help you today?');
    ui.mode.set(mode);
    const fixed = Boolean(pane && pane.id !== null);
    ui.mode.setDisabled(fixed, fixed ? LR.t('The mode is fixed per conversation – start a new chat to switch.') : null);
    if (ui.bigJob) ui.bigJob.hidden = !(LR.has('autosplit') && LR.bigJob);
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
    const title = chat ? chat.title || LR.t('Untitled chat') : mode === 'code' ? LR.t('New code chat') : LR.t('New chat');
    if (LR.currentRoute && LR.currentRoute() === 'chat') LR.setTitle(title);
    updateSend();
    renderHistoryState();
  }

  function renderUnavailable() {
    const ui = S.ui;
    if (ui.unavailable.childElementCount) return;
    ui.unavailable.append(LR.empty({
      icon: 'chat',
      title: LR.t('Chat is not available in this build'),
      text: LR.t('This version of LowRAM AI was built without the chat module. You can still use the task queue and the other tools.'),
      actions: [LR.button({ text: LR.t('Open the task queue'), variant: 'primary', icon: 'tasks', onclick: () => LR.go('#/tasks') })],
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
      ensureSummary(pane, (data.chat && data.chat.summary) || data.summary);
      let lastUser = '';
      let pendingReload = false;
      for (const m of messages) {
        if (m.role === 'user') {
          lastUser = LR.str(m.content);
          pane.list.append(userMessage(lastUser, m.attachments));
        } else {
          const a = assistantMessage();
          wireAssistant(pane, a, lastUser);
          a.finish(m);
          if (m.status === 'streaming') pendingReload = true;
          pane.list.append(a.li);
        }
      }
      pane.loaded = true;
      renderLastActions(pane);
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
        LR.toast(LR.t('That chat no longer exists.'), 'info');
        LR.go('#/chat/new');
        return;
      }
      pane.list.append(h('li', { class: 'msg' }, LR.callout({
        tone: 'error',
        title: LR.t('Could not load this chat'),
        text: err.message,
        actions: [LR.button({ text: LR.t('Try again'), icon: 'retry', size: 'sm', onclick: () => loadPane(pane) })],
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

  function attachmentChips(list) {
    if (!Array.isArray(list) || !list.length) return null;
    return h('ul', { class: 'att-list', 'aria-label': LR.t('Attached files') }, list.map((a) => h('li', { class: 'att-chip' }, [
      icon('file'),
      h('span', { class: 'att-name', text: LR.str(a.name, 'file') }),
      LR.isNum(a.size) ? h('span', { class: 'att-size', text: fmt.bytes(a.size) })
        : LR.isNum(a.chars) ? h('span', { class: 'att-size', text: fmt.int(a.chars) + LR.t(' chars') }) : null,
    ])));
  }

  function userMessage(content, attachments) {
    return h('li', { class: 'msg msg-user' }, [
      h('h2', { class: 'visually-hidden', text: LR.t('You said:') }),
      content ? h('div', { class: 'bubble', text: content }) : null,
      attachmentChips(attachments),
      h('div', { class: 'msg-actions' }, [
        LR.copyButton({ getText: () => content, iconOnly: true, cls: 'icon-btn', ariaLabel: LR.t('Copy your message') }),
      ]),
    ]);
  }

  /** v0.3.2 backends report token stats; that is also our signal for regenerate/continue. */
  const isSmart = (m) => Boolean(m) && ('finish_reason' in m || 'tokens_per_s' in m || 'truncated' in m);

  function assistantMessage() {
    const thinkId = LR.uid('think');
    const thinkLabel = h('span', { class: 'thinking-label', text: LR.t('Thinking…') });
    const thinkBody = h('div', { class: 'thinking-body md', id: thinkId, hidden: true });
    const thinkToggle = h('button', { type: 'button', class: 'thinking-toggle', 'aria-expanded': 'false', 'aria-controls': thinkId }, [
      h('span', { class: 'thinking-icon' }, icon('sparkle')),
      thinkLabel,
      icon('chevron-down', 'thinking-chevron'),
    ]);
    const think = h('div', { class: 'thinking', hidden: true }, [thinkToggle, thinkBody]);
    const body = h('div', { class: 'msg-body md' });
    const waitText = h('span', { class: 'wait-text', text: LR.t('Working on it…') });
    const wait = h('div', { class: 'msg-wait' }, [h('span', { class: 'typing', 'aria-hidden': 'true' }, [h('i'), h('i'), h('i')]), waitText]);
    const extra = h('div', { class: 'msg-extra' });
    const cont = h('div', { class: 'msg-continue', hidden: true });
    const meta = h('span', { class: 'msg-meta' });
    let answerText = '';
    let reasoningText = '';
    const copyBtn = LR.copyButton({ getText: () => answerText, iconOnly: true, cls: 'icon-btn', ariaLabel: LR.t('Copy answer') });
    const retryBtn = h('button', { type: 'button', class: 'icon-btn', 'aria-label': LR.t('Retry'), title: LR.t('Retry'), hidden: true }, icon('retry'));
    const regenBtn = h('button', { type: 'button', class: 'icon-btn', 'aria-label': LR.t('Regenerate answer'), title: LR.t('Regenerate'), hidden: true }, icon('retry'));
    const actions = h('div', { class: 'msg-actions', hidden: true }, [copyBtn, retryBtn, regenBtn, meta]);
    const li = h('li', { class: 'msg msg-assistant' }, [
      h('h2', { class: 'visually-hidden', text: LR.t('LowRAM AI said:') }),
      think,
      body,
      wait,
      extra,
      cont,
      actions,
    ]);
    let renderer = LR.md.createRenderer(body);
    let thinkRenderer = LR.md.createRenderer(thinkBody);
    let userToggled = false;
    let retryFn = null;
    let handlers = {};
    let message = null;
    let extraReasoning = '';
    let isLast = false;
    let frame = 0;
    let throttle = 0;
    let lastRender = 0;
    let pending = null;
    let thinkStart = 0;
    let thinkSecs = null;
    let statusText = '';
    let started = Date.now();
    let waitTimer = 0;
    function startWait() {
      started = Date.now();
      statusText = '';
      waitText.textContent = LR.t('Working on it…');
      window.clearInterval(waitTimer);
      waitTimer = window.setInterval(() => {
        if ((Date.now() - started) / 1000 > 6) waitText.textContent = (statusText || LR.t('Still working – local models can take a moment')) + ' · ' + fmt.clock(Date.now() - started);
      }, 1000);
    }
    function stopTimers() {
      window.clearInterval(waitTimer);
      if (frame) { window.cancelAnimationFrame(frame); frame = 0; }
      if (throttle) { window.clearTimeout(throttle); throttle = 0; }
    }
    startWait();

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
    regenBtn.addEventListener('click', () => { if (handlers.regenerate) handlers.regenerate(); });

    function apply(reasoning, answer, streaming) {
      if (reasoning && reasoning.trim()) {
        if (!thinkStart) thinkStart = Date.now();
        reasoningText = reasoning;
        think.hidden = false;
        thinkRenderer.render(reasoning, streaming && !answer);
        const thinking = streaming && !answer;
        think.classList.toggle('is-active', thinking);
        if (thinking) {
          thinkLabel.textContent = LR.t('Thinking…');
          if (!userToggled) setExpanded(true);
        } else {
          if (thinkSecs === null && streaming) thinkSecs = (Date.now() - thinkStart) / 1000;
          thinkLabel.textContent = thinkSecs !== null && thinkSecs >= 1 ? LR.t('Thought for ') + fmt.duration(thinkSecs) : LR.t('Thoughts');
          if (!userToggled) setExpanded(false);
        }
      }
      if (answer || !streaming) {
        answerText = answer;
        renderer.render(answer, streaming);
      }
      wait.hidden = !(streaming && !answer && !(reasoning && reasoning.trim()));
      li.classList.toggle('is-streaming', streaming);
    }

    function syncLast() {
      const busy = li.classList.contains('is-streaming') || Boolean(S.stream);
      regenBtn.hidden = !(isLast && !busy && handlers.regenerate && isSmart(message) && message.status !== 'streaming');
      const truncated = Boolean(message && (message.truncated || message.finish_reason === 'length'));
      cont.hidden = !(isLast && !busy && truncated && handlers.cont);
    }

    const api = {
      li,
      get message() { return message; },
      get answer() { return answerText; },
      get reasoning() { return reasoningText; },
      get extraReasoning() { return extraReasoning; },
      setRetry(fn) { retryFn = fn; },
      setHandlers(next) {
        handlers = next || {};
        syncLast();
      },
      setLast(last) {
        isLast = last;
        syncLast();
      },
      /** Backend progress note ("Loading the model…") shown until tokens arrive. */
      status(text) {
        statusText = text;
        if (text) waitText.textContent = text;
      },
      /** Clear the bubble before a regenerated answer streams in. */
      reset() {
        stopTimers();
        body.replaceChildren();
        thinkBody.replaceChildren();
        renderer = LR.md.createRenderer(body);
        thinkRenderer = LR.md.createRenderer(thinkBody);
        answerText = '';
        reasoningText = '';
        thinkStart = 0;
        thinkSecs = null;
        userToggled = false;
        think.hidden = true;
        extra.replaceChildren();
        cont.hidden = true;
        actions.hidden = true;
        wait.hidden = false;
        li.classList.remove('is-error', 'is-stopped');
        li.classList.add('is-streaming');
        startWait();
      },
      /** Keep the text and stream more of it (continue). */
      resume() {
        stopTimers();
        startWait();
        cont.hidden = true;
        regenBtn.hidden = true;
        li.classList.add('is-streaming');
      },
      /**
       * Coalesced to one render per animation frame (≤10/s for long answers).
       * `getView` is only evaluated when a frame actually renders.
       */
      stream(getView) {
        pending = getView;
        if (frame || throttle) return;
        const run = () => {
          throttle = 0;
          frame = window.requestAnimationFrame(() => {
            frame = 0;
            const g = pending;
            pending = null;
            if (!g) return;
            const v = g();
            lastRender = performance.now();
            apply(v[0], v[1], true);
            scrollToBottom(false);
          });
        };
        const delay = answerText.length > 20000 ? Math.max(0, 100 - (performance.now() - lastRender)) : 0;
        if (delay > 0) throttle = window.setTimeout(run, delay);
        else run();
      },
      finish(msg, extraR) {
        stopTimers();
        const m = msg || {};
        message = m;
        extraReasoning = extraR || '';
        const split = LR.md.splitThinking(LR.str(m.content));
        apply(extraReasoning + split.reasoning, split.answer, false);
        wait.hidden = true;
        actions.hidden = false;
        // Quiet stats line: "14 tok/s · 38 s · Qwen3 4B"
        const parts = [];
        if (LR.isNum(m.tokens_per_s) && m.tokens_per_s > 0) parts.push((m.tokens_per_s < 10 ? m.tokens_per_s.toFixed(1) : Math.round(m.tokens_per_s)) + LR.t(' tok/s'));
        if (LR.isNum(m.duration_s)) parts.push(fmt.duration(m.duration_s));
        if (m.model) parts.push(LR.modelName(m.model));
        meta.textContent = parts.join(' · ');
        meta.title = LR.isNum(m.tokens) ? fmt.int(m.tokens) + LR.t(' tokens') : '';
        extra.replaceChildren();
        li.classList.remove('is-error', 'is-stopped');
        retryBtn.hidden = true;
        if (m.status === 'stopped') {
          li.classList.add('is-stopped');
          extra.append(h('p', { class: 'msg-note' }, [icon('stop'), h('span', { text: split.answer ? LR.t('Stopped – the partial answer was kept.') : LR.t('Stopped before an answer arrived.') })]));
          retryBtn.hidden = !retryFn || isSmart(m);
        } else if (m.status === 'error') {
          api.fail(m.error || LR.t('The answer could not be completed.'));
        } else if (m.status === 'streaming') {
          extra.append(h('p', { class: 'msg-note' }, [LR.spinner(), h('span', { text: LR.t('Still generating… this view refreshes by itself.') })]));
          actions.hidden = true;
        } else if (!split.answer) {
          extra.append(h('p', { class: 'msg-note', text: LR.t('The model returned an empty answer.') }));
        }
        const srcEl = sourcesBlock(m.sources);
        if (srcEl) extra.append(srcEl);
        cont.replaceChildren(
          h('p', { class: 'msg-note' }, [icon('info'), h('span', { text: LR.t('The answer reached the length limit.') })]),
          LR.button({ text: LR.t('Continue'), icon: 'arrow-down', size: 'sm', variant: 'secondary', onclick: () => { if (handlers.cont) handlers.cont(); } }),
        );
        syncLast();
        scrollToBottom(false);
      },
      fail(error, partial) {
        stopTimers();
        if (partial !== undefined) apply(partial[0], partial[1], false);
        else li.classList.remove('is-streaming');
        wait.hidden = true;
        li.classList.add('is-error');
        const modelIssue = /model|engine|llama|server|start/i.test(LR.str(error));
        const btns = [];
        if (handlers.regenerate && isSmart(message)) btns.push(LR.button({ text: LR.t('Regenerate'), icon: 'retry', size: 'sm', variant: 'secondary', onclick: () => handlers.regenerate() }));
        else if (retryFn) btns.push(LR.button({ text: LR.t('Retry'), icon: 'retry', size: 'sm', variant: 'secondary', onclick: () => retryFn() }));
        if (modelIssue && LR.has('engine')) btns.push(LR.button({ text: LR.t('Open Models'), icon: 'sliders', size: 'sm', variant: 'ghost', onclick: () => LR.go('#/settings') }));
        extra.replaceChildren(LR.callout({ tone: 'error', title: LR.t('The answer could not be completed'), text: LR.str(error, LR.t('Unknown error.')), actions: btns, role: 'alert' }));
        actions.hidden = !answerText;
        retryBtn.hidden = true;
        cont.hidden = true;
        scrollToBottom(false);
      },
    };
    li._ctx = api;
    return api;
  }

  /** Regenerate / continue are offered on the last assistant message only. */
  function renderLastActions(pane) {
    if (!pane) return;
    const items = pane.list.querySelectorAll('.msg-assistant');
    items.forEach((el, idx) => { if (el._ctx) el._ctx.setLast(idx === items.length - 1); });
  }

  /* ------------------------------------------------------------ notices */

  function clearNotice() {
    if (S.ui.notice) S.ui.notice.replaceChildren();
  }

  function showNotice(err, retry) {
    const busy = err.status === 409;
    const noModel = err.status === 400 || /model|engine|llama|not running|start/i.test(err.message);
    let title = LR.t('Could not send your message');
    if (err.network) title = LR.t('LowRAM AI is not reachable');
    else if (busy && !noModel) title = LR.t('LowRAM AI is busy right now');
    else if (noModel) title = LR.t('No model is ready yet');
    const text = err.message + (busy && !retry ? LR.t(' Your message is still in the box – send it again in a moment.') : '');
    const actions = [];
    if (LR.has('engine') && (noModel || busy)) actions.push(LR.button({ text: LR.t('Open Models'), icon: 'sliders', size: 'sm', variant: 'primary', onclick: () => LR.go('#/settings') }));
    actions.push(LR.button({ text: LR.t('Try again'), icon: 'retry', size: 'sm', onclick: () => { clearNotice(); if (retry) retry(); else send(); } }));
    actions.push(LR.button({ text: LR.t('Dismiss'), size: 'sm', variant: 'ghost', onclick: clearNotice }));
    S.ui.notice.replaceChildren(LR.callout({ tone: busy ? 'warn' : 'error', title, text, actions, role: 'alert', cls: 'notice-pop' }));
  }

  /* ------------------------------------------------------------ attachments */

  const MAX_FILES = 5;
  const MAX_FILE_BYTES = 200 * 1024;
  const BINARY_EXT = /\.(png|jpe?g|gif|webp|heic|bmp|ico|tiff?|pdf|zip|gz|tgz|bz2|xz|7z|rar|dmg|pkg|exe|dll|so|dylib|bin|iso|mp3|wav|m4a|flac|ogg|mp4|mov|avi|mkv|webm|docx?|xlsx?|pptx?|key|pages|numbers|sqlite|db|gguf|safetensors|pt|onnx|class|jar|o|a|woff2?|ttf|otf)$/i;

  function attachNote(text) {
    const el = S.ui.attachMsg;
    el.textContent = text;
    el.hidden = !text;
    window.clearTimeout(S.attachTimer);
    if (text) S.attachTimer = window.setTimeout(() => attachNote(''), 7000);
  }

  async function addFiles(fileList) {
    const files = [...(fileList || [])];
    const problems = [];
    for (const file of files) {
      if (S.attachments.length >= MAX_FILES && !S.attachments.some((a) => a.name === file.name)) {
        problems.push(LR.t('You can attach up to ') + MAX_FILES + LR.t(' files.'));
        break;
      }
      if (file.size > MAX_FILE_BYTES) { problems.push(file.name + LR.t(' is larger than 200 KB.')); continue; }
      if (BINARY_EXT.test(file.name) || /^(image|audio|video)\//.test(file.type) || /pdf|zip|octet-stream/.test(file.type)) {
        problems.push(file.name + LR.t(' is not a text file.'));
        continue;
      }
      let text;
      try { text = await file.text(); } catch (_) { problems.push(file.name + LR.t(' could not be read.')); continue; }
      const bad = (text.match(/�/g) || []).length;
      if (text.includes('\u0000') || bad > Math.max(4, text.length / 100)) { problems.push(file.name + LR.t(' looks like a binary file.')); continue; }
      S.attachments = S.attachments.filter((a) => a.name !== file.name).concat({ name: file.name, size: file.size, content: text });
    }
    attachNote(problems.length ? problems.join(' ') + LR.t(' Only text and code files up to 200 KB work.') : '');
    renderAttachments();
  }

  function renderAttachments() {
    const ui = S.ui;
    ui.atts.replaceChildren(...S.attachments.map((a) => h('li', { class: 'att-chip is-removable' }, [
      icon('file'),
      h('span', { class: 'att-name', text: a.name }),
      h('span', { class: 'att-size', text: fmt.bytes(a.size) }),
      h('button', { type: 'button', class: 'att-remove', 'aria-label': LR.t('Remove ') + a.name, onclick: () => {
        S.attachments = S.attachments.filter((x) => x !== a);
        renderAttachments();
        ui.input.focus();
      } }, icon('x')),
    ])));
    ui.atts.hidden = !S.attachments.length;
    updateSend();
  }

  /* ------------------------------------------------------------ streaming */

  const budget = () => (LR.state.settings && LR.isNum(LR.state.settings.budget_percent) ? LR.state.settings.budget_percent : undefined);

  /** Stream an answer into assistant bubble `o.a` (send, continue and regenerate). */
  async function runStream(o) {
    const ui = S.ui;
    const a = o.a;
    const pane = o.pane;
    const ctx = { chatId: o.chatId, stopRequested: false, gotStart: false, done: false, tokens: '', reasoning: '', controller: new AbortController() };
    S.stream = ctx;
    updateSend();
    renderHistoryState();
    renderLastActions(pane);
    ui.status.textContent = LR.t('LowRAM AI is answering…');
    const base = o.base || { reasoning: '', answer: '' };
    const extraBefore = o.extra || '';
    const view = () => {
      const split = LR.md.splitThinking(ctx.tokens);
      return [base.reasoning + ctx.reasoning + split.reasoning, base.answer + split.answer];
    };
    try {
      await LR.stream(o.path, o.body, {
        signal: ctx.controller.signal,
        onEvent(ev) {
          if (ev.type === 'ping') return;
          if (ev.type === 'start') {
            ctx.gotStart = true;
            if (ev.chat && ev.chat.id === ctx.chatId) {
              pane.chat = ev.chat;
              if (ev.chat.summary) ensureSummary(pane, ev.chat.summary);
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
            a.stream(view);
          } else if (ev.type === 'reasoning') {
            ctx.gotStart = true;
            ctx.reasoning += LR.str(ev.text);
            a.stream(view);
          } else if (ev.type === 'done') {
            ctx.done = true;
            a.finish(ev.message || { content: base.answer + ctx.tokens, status: ctx.stopRequested ? 'stopped' : 'complete' }, extraBefore + ctx.reasoning);
          } else if (ev.type === 'error') {
            ctx.done = true;
            const msg = ev.message;
            const split = LR.md.splitThinking(msg && typeof msg.content === 'string' ? msg.content : base.answer + ctx.tokens);
            a.fail(ev.error || LR.t('The answer could not be completed.'), [extraBefore + ctx.reasoning + split.reasoning, split.answer]);
          }
        },
      });
      if (!ctx.done) {
        if (ctx.stopRequested) a.finish({ content: base.answer + ctx.tokens, status: 'stopped' }, extraBefore + ctx.reasoning);
        else a.fail(LR.t('The answer ended unexpectedly. The model server may have stopped.'), view());
      }
    } catch (err) {
      if (err && err.name === 'AbortError') {
        a.finish({ content: base.answer + ctx.tokens, status: 'stopped' }, extraBefore + ctx.reasoning);
      } else if (!ctx.gotStart && err.status) {
        o.onRejected(err);
      } else {
        a.fail(err.message, view());
      }
    } finally {
      S.stream = null;
      ui.status.textContent = LR.t('Answer finished.');
      if (S.currentId === ctx.chatId) renderChrome();
      updateSend();
      renderLastActions(pane);
      refreshList();
      LR.emit('status-refresh');
      window.setTimeout(() => LR.emit('status-refresh'), 900);
    }
  }

  function wireAssistant(pane, a, userText) {
    a.setRetry(() => send(userText));
    a.setHandlers({
      regenerate: () => regenerate(pane, a),
      cont: () => continueAnswer(pane, a),
    });
  }

  function regenerate(pane, a) {
    if (S.stream || pane.id === null) return;
    clearNotice();
    const prev = a.message;
    const prevExtra = a.extraReasoning;
    a.reset();
    runStream({
      pane, a, chatId: pane.id,
      path: '/api/chats/' + encodeURIComponent(pane.id) + '/regenerate',
      body: { percent: budget() },
      onRejected: (err) => {
        a.finish(prev, prevExtra);
        showNotice(err, () => regenerate(pane, a));
      },
    });
  }

  function continueAnswer(pane, a) {
    if (S.stream || pane.id === null) return;
    clearNotice();
    const prev = a.message;
    const prevExtra = a.extraReasoning;
    const base = { reasoning: a.reasoning, answer: a.answer + (/\s$/.test(a.answer) ? '' : '') };
    a.resume();
    runStream({
      pane, a, chatId: pane.id, base, extra: prevExtra,
      path: '/api/chats/' + encodeURIComponent(pane.id) + '/continue',
      body: { percent: budget() },
      onRejected: (err) => {
        a.finish(prev, prevExtra);
        showNotice(err, () => continueAnswer(pane, a));
      },
    });
  }

  /* ------------------------------------------------------------ sending */

  async function send(retryText) {
    const ui = S.ui;
    if (S.stream || !LR.has('chat')) return;
    const fromComposer = typeof retryText !== 'string';
    const attachments = fromComposer ? S.attachments.slice() : [];
    let content = (fromComposer ? ui.input.value : retryText).trim();
    if (!content && !attachments.length) return;
    if (!content) content = attachments.length === 1 ? LR.t('Please take a look at the attached file.') : LR.t('Please take a look at the attached files.');
    clearNotice();
    let pane = currentPane();
    const restore = fromComposer ? ui.input.value : null;
    if (fromComposer) {
      ui.input.value = '';
      autosize();
      LR.store.remove(draftKey(S.currentId));
      S.attachments = [];
      renderAttachments();
    }
    const restoreComposer = () => {
      if (restore !== null && !ui.input.value) { ui.input.value = restore; autosize(); }
      if (attachments.length && !S.attachments.length) { S.attachments = attachments; renderAttachments(); }
    };
    S.stream = { chatId: S.currentId, stopRequested: false, done: false, controller: new AbortController() };
    updateSend();

    if (pane.id === null) {
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
        S.chats = [chat, ...S.chats.filter((c) => c.id !== chat.id)];
        window.history.replaceState(null, '', '#/chat/' + chat.id);
      } catch (err) {
        S.stream = null;
        restoreComposer();
        showNotice(err);
        updateSend();
        return;
      }
    }
    S.stream = null;

    const userLi = userMessage(content, attachments);
    const a = assistantMessage();
    wireAssistant(pane, a, content);
    pane.list.append(userLi, a.li);
    renderChrome();
    scrollToBottom(true);

    const body = { content, percent: budget() };
    if (S.collectionId && LR.knowledge && LR.knowledge.available) body.collection_id = isNaN(Number(S.collectionId)) ? S.collectionId : Number(S.collectionId);
    if (attachments.length) body.attachments = attachments.map((x) => ({ name: x.name, content: x.content }));
    await runStream({
      pane, a, chatId: pane.id, body,
      path: '/api/chats/' + encodeURIComponent(pane.id) + '/messages',
      onRejected: (err) => {
        userLi.remove();
        a.li.remove();
        restoreComposer();
        renderChrome();
        renderLastActions(pane);
        showNotice(err);
      },
    });
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
    if (!d) return LR.t('Earlier');
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const t = d.getTime();
    if (t >= today.getTime()) return LR.t('Today');
    if (t >= today.getTime() - 86400000) return LR.t('Yesterday');
    return LR.t('Earlier');
  }

  function renderHistory() {
    const el = S.historyEl;
    if (!el) return;
    closeMenu();
    if (S.searchBox) S.searchBox.hidden = !LR.has('chat') || (S.listLoaded && !S.chats.length && S.results === null);
    if (!LR.has('chat')) {
      el.replaceChildren(h('p', { class: 'history-empty', text: LR.t('Chat history appears here.') }));
      return;
    }
    if (!S.listLoaded) {
      el.replaceChildren(LR.skeleton(5, 'history-skeleton'));
      return;
    }
    if (S.results !== null) {
      if (!S.results.length) {
        el.replaceChildren(h('p', { class: 'history-empty', text: LR.t('No chats match “') + S.query + '”.' }));
        return;
      }
      const id = LR.uid('hist');
      el.replaceChildren(h('div', { class: 'history-group' }, [
        h('h3', { class: 'history-label', id, text: fmt.plural(S.results.length, 'result') }),
        h('ul', { class: 'history-list', 'aria-labelledby': id }, S.results.map(historyItem)),
      ]));
      renderHistoryState();
      return;
    }
    if (!S.chats.length) {
      el.replaceChildren(h('p', { class: 'history-empty', text: LR.t('Your conversations will appear here.') }));
      return;
    }
    const groups = new Map([[LR.t('Today'), []], [LR.t('Yesterday'), []], [LR.t('Earlier'), []]]);
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
    const title = chat.title || LR.t('Untitled chat');
    const link = h('a', { class: 'hist-item', href: '#/chat/' + chat.id, dataset: { id: String(chat.id) } }, [
      chat.mode === 'code' ? icon('code', 'hist-mode') : null,
      h('span', { class: 'hist-title' }, S.results !== null ? highlight(title, S.query) : title),
      h('span', { class: 'hist-live', 'aria-hidden': 'true' }),
    ]);
    const more = h('button', {
      type: 'button',
      class: 'hist-more',
      'aria-label': LR.t('Options for “') + title + '”',
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

  /** Title with case-insensitive matches wrapped in <mark> (text nodes only). */
  function highlight(text, query) {
    const q = query.trim().toLowerCase();
    if (!q) return text;
    const lower = text.toLowerCase();
    const out = [];
    let i = 0;
    let j;
    while ((j = lower.indexOf(q, i)) !== -1 && out.length < 40) {
      if (j > i) out.push(text.slice(i, j));
      out.push(h('mark', { text: text.slice(j, j + q.length) }));
      i = j + q.length;
    }
    out.push(text.slice(i));
    return out;
  }

  function onSearch() {
    const q = S.searchInput.value.trim();
    S.searchClear.hidden = !S.searchInput.value;
    window.clearTimeout(S.searchTimer);
    if (!q) {
      S.query = '';
      S.results = null;
      renderHistory();
      return;
    }
    S.searchTimer = window.setTimeout(async () => {
      const seq = ++S.searchSeq;
      try {
        const data = await LR.get('/api/chats?q=' + encodeURIComponent(q));
        if (seq !== S.searchSeq) return;
        let chats = Array.isArray(data.chats) ? data.chats : [];
        // A backend without search returns everything: then filter titles here.
        const matches = (c) => LR.str(c.title).toLowerCase().includes(q.toLowerCase());
        if (S.chats.length && chats.length === S.chats.length && !chats.every(matches)) chats = chats.filter(matches);
        S.query = q;
        S.results = chats;
        renderHistory();
        LR.announce(fmt.plural(chats.length, 'chat') + LR.t(' found'));
      } catch (_) { /* keep the previous list */ }
    }, 250);
  }

  function mountSearch(el) {
    S.searchInput = h('input', { type: 'search', class: 'history-search-input', placeholder: LR.t('Search chats'), 'aria-label': LR.t('Search chats'), autocomplete: 'off', spellcheck: 'false' });
    S.searchClear = h('button', { type: 'button', class: 'icon-btn search-clear', 'aria-label': LR.t('Clear search'), hidden: true, onclick: () => {
      S.searchInput.value = '';
      onSearch();
      S.searchInput.focus();
    } }, icon('x'));
    S.searchInput.addEventListener('input', onSearch);
    S.searchInput.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && S.searchInput.value) {
        ev.preventDefault();
        S.searchInput.value = '';
        onSearch();
      }
    });
    S.searchBox = h('div', { class: 'history-search', role: 'search' }, [icon('search'), S.searchInput, S.searchClear]);
    el.before(S.searchBox);
  }

  async function exportChat(chat) {
    try {
      const res = await fetch('/api/chats/' + encodeURIComponent(chat.id) + '/export', { credentials: 'same-origin', cache: 'no-store' });
      if (res.status === 404) {
        LR.toast(LR.t('Export is not available in this version'), 'info');
        return;
      }
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const blob = await res.blob();
      const url = URL.createObjectURL(new Blob([blob], { type: 'text/markdown' }));
      const name = (LR.str(chat.title, 'chat').replace(/[^\w\- .]+/g, '').trim().slice(0, 60) || 'chat') + '.md';
      const a = h('a', { href: url, download: name, class: 'visually-hidden' });
      document.body.append(a);
      a.click();
      a.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 4000);
      LR.toast(LR.t('Exported ') + name);
    } catch (err) {
      LR.toast(LR.t('Could not export: ') + err.message, 'error');
    }
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
      h('button', { type: 'button', role: 'menuitem', class: 'menu-item', onclick: () => { closeMenu(); startRename(chat); } }, [icon('pencil'), h('span', { text: LR.t('Rename') })]),
      h('button', { type: 'button', role: 'menuitem', class: 'menu-item', onclick: () => { closeMenu(); exportChat(chat); } }, [icon('export'), h('span', { text: LR.t('Export as Markdown') })]),
      h('button', { type: 'button', role: 'menuitem', class: 'menu-item menu-danger', onclick: () => { closeMenu(); deleteChat(chat); } }, [icon('trash'), h('span', { text: LR.t('Delete') })]),
    ];
    const el = h('div', { class: 'menu', role: 'menu', 'aria-label': LR.t('Chat options') }, items);
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
    const input = h('input', { class: 'input hist-rename', type: 'text', maxlength: '200', 'aria-label': LR.t('Chat title') });
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
          const c = data.chat && typeof data.chat === 'object' ? data.chat : { title };
          for (const key of ['id', 'title', 'mode', 'updated_at', 'message_count']) {
            if (key in c) chat[key] = c[key];
          }
          LR.toast(LR.t('Chat renamed'));
        } catch (err) {
          LR.toast(LR.t('Could not rename: ') + err.message, 'error');
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
      title: LR.t('Delete this chat?'),
      message: '“' + (chat.title || LR.t('Untitled chat')) + LR.t('” will be removed from this computer. This cannot be undone.'),
      confirmText: LR.t('Delete'),
      danger: true,
    });
    if (!ok) return;
    try {
      await LR.post('/api/chats/' + encodeURIComponent(chat.id) + '/delete', {});
      S.chats = S.chats.filter((c) => c.id !== chat.id);
      S.panes.delete(chat.id);
      LR.store.remove(draftKey(chat.id));
      LR.toast(LR.t('Chat deleted'));
      renderHistory();
      if (S.currentId === chat.id) LR.go('#/chat/new');
    } catch (err) {
      LR.toast(LR.t('Could not delete: ') + err.message, 'error');
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
      mountSearch(el);
      renderHistory();
    },
    refreshList,
    renderHistory,
    get streaming() { return Boolean(S.stream); },
  };
})();
