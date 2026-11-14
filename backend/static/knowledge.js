/* LowRAM AI Studio – knowledge base: collections of local text documents the
 * chat can answer with. Document names, snippets and search results are
 * untrusted text and only ever go through textContent. If the backend has no
 * /api/knowledge (404) the nav item, the view and the chat selector stay hidden. */
(function () {
  'use strict';

  const LR = window.LR;
  const h = LR.h;
  const icon = LR.icon;
  const fmt = LR.fmt;
  const t = LR.t;
  const MAX_BYTES = 2 * 1024 * 1024;
  const BINARY_EXT = /\.(png|jpe?g|gif|webp|heic|bmp|ico|tiff?|pdf|zip|gz|tgz|bz2|xz|7z|rar|dmg|pkg|exe|dll|so|dylib|bin|iso|mp3|wav|m4a|flac|ogg|mp4|mov|avi|mkv|webm|docx?|xlsx?|pptx?|key|pages|numbers|sqlite|db|gguf|safetensors|pt|onnx|class|jar|woff2?|ttf|otf)$/i;
  const ui = {};
  const st = { collectionId: null, detail: null, busy: false };

  /* ------------------------------------------------------------ shared state (chat uses it) */

  const kb = (LR.knowledge = { available: null, collections: [] });

  kb.load = async function () {
    try {
      const data = await LR.get('/api/knowledge');
      kb.available = true;
      kb.collections = Array.isArray(data.collections) ? data.collections : [];
    } catch (err) {
      if (err.status === 404 || err.status === 405) {
        kb.available = false;
        kb.collections = [];
      } else if (kb.available === null) {
        return kb.collections;
      }
    }
    LR.knowledgeAvailable = kb.available;
    if (LR.updateNav) LR.updateNav();
    LR.emit('knowledge', kb);
    return kb.collections;
  };

  let probed = false;
  LR.on('status', () => {
    if (probed) return;
    probed = true;
    kb.load();
  });

  /* ------------------------------------------------------------ helpers */

  const collectionName = (c) => LR.str(c && c.name, t('Untitled'));

  async function readFile(file) {
    if (file.size > MAX_BYTES) throw new Error(t('{name} is larger than 2 MB.', { name: file.name }));
    if (BINARY_EXT.test(file.name) || /^(image|audio|video)\//.test(file.type) || /pdf|zip|octet-stream/.test(file.type)) {
      throw new Error(t('{name} is not a text file.', { name: file.name }));
    }
    let text;
    try { text = await file.text(); } catch (_) { throw new Error(t('{name} could not be read.', { name: file.name })); }
    const bad = (text.match(/�/g) || []).length;
    if (text.includes('\u0000') || bad > Math.max(4, text.length / 100)) throw new Error(t('{name} is not a text file.', { name: file.name }));
    return text;
  }

  /* ------------------------------------------------------------ list view */

  function renderList() {
    const cols = kb.collections;
    const nameInput = h('input', { class: 'input', type: 'text', maxlength: '120', placeholder: t('e.g. Project notes'), autocomplete: 'off' });
    const field = LR.field({ label: t('Collection name'), control: nameInput });
    const createBtn = LR.button({ text: t('Create'), icon: 'plus', variant: 'primary', type: 'submit' });
    const row = h('div', { class: 'input-row' });
    nameInput.replaceWith(row);
    row.append(nameInput, createBtn);
    const form = h('form', { class: 'form', novalidate: true }, field.el);
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      field.setError('');
      const name = nameInput.value.trim();
      if (!name) {
        field.setError(t('This field is required.'));
        nameInput.focus();
        return;
      }
      LR.setBusy(createBtn, true);
      try {
        const data = await LR.post('/api/knowledge', { name });
        LR.toast(t('Collection created'));
        await kb.load();
        const id = data && (data.collection ? data.collection.id : data.id);
        if (id !== undefined && id !== null) LR.go('#/knowledge/' + encodeURIComponent(id));
        else renderList();
      } catch (err) {
        field.setError(err.message);
        LR.setBusy(createBtn, false);
      }
    });

    const newCard = h('section', { class: 'card', 'aria-labelledby': 'kb-new-title' }, [
      h('div', { class: 'card-head' }, h('h2', { class: 'card-title', id: 'kb-new-title', text: t('New collection') })),
      form,
    ]);
    const listCard = h('section', { class: 'card', 'aria-labelledby': 'kb-list-title' }, [
      h('div', { class: 'card-head' }, h('h2', { class: 'card-title', id: 'kb-list-title', text: t('Collections') })),
      cols.length
        ? h('ul', { class: 'kb-list' }, cols.map((c) => h('li', null, h('a', { class: 'kb-row', href: '#/knowledge/' + encodeURIComponent(c.id) }, [
          h('span', { class: 'kb-row-icon', 'aria-hidden': 'true' }, icon('book')),
          h('span', { class: 'kb-row-main' }, [
            h('span', { class: 'kb-row-name', text: collectionName(c) }),
            h('span', { class: 'kb-row-meta', text: [LR.plural(LR.isNum(c.documents) ? c.documents : 0, 'document'), LR.isNum(c.chars) ? t('{n} characters', { n: fmt.int(c.chars) }) : ''].filter(Boolean).join(' · ') }),
          ]),
          icon('chevron-right', 'kb-row-chevron'),
        ]))))
        : LR.empty({ icon: 'book', title: t('No collections yet'), text: t('Create a collection and add text, Markdown or code files. In chat, choose it under “Answer with knowledge”.') }),
    ]);
    ui.body.replaceChildren(listCard, newCard);
  }

  /* ------------------------------------------------------------ detail view */

  async function loadDetail(id) {
    ui.body.replaceChildren(h('div', { class: 'card' }, LR.skeleton(5)));
    try {
      const data = await LR.get('/api/knowledge/' + encodeURIComponent(id));
      const col = data.collection || data;
      const docs = Array.isArray(data.documents) ? data.documents : Array.isArray(col.documents) ? col.documents : [];
      st.detail = { id, name: collectionName(col), documents: docs };
      renderDetail();
    } catch (err) {
      ui.body.replaceChildren(LR.callout({
        tone: 'error', title: t('Could not load the knowledge base'), text: err.message,
        actions: [
          LR.button({ text: t('Try again'), icon: 'retry', size: 'sm', onclick: () => loadDetail(id) }),
          LR.button({ text: t('Back to collections'), size: 'sm', variant: 'ghost', onclick: () => LR.go('#/knowledge') }),
        ],
      }));
    }
  }

  function renderDetail() {
    const d = st.detail;
    const base = '/api/knowledge/' + encodeURIComponent(d.id);
    LR.setTitle(d.name);

    // add documents: file picker + drag & drop
    const fileInput = h('input', { type: 'file', multiple: true, class: 'visually-hidden', tabindex: '-1', 'aria-hidden': 'true',
      accept: '.txt,.md,.markdown,.rst,.csv,.tsv,.json,.yaml,.yml,.toml,.ini,.xml,.html,.css,.js,.mjs,.ts,.tsx,.jsx,.py,.rb,.go,.rs,.java,.kt,.swift,.c,.h,.cpp,.hpp,.cs,.php,.sh,.sql,.log,text/*' });
    const status = h('p', { class: 'kb-status', role: 'status', 'aria-live': 'polite' });
    const pickBtn = LR.button({ text: t('Choose files…'), icon: 'file', variant: 'secondary', onclick: () => fileInput.click() });
    const drop = h('div', { class: 'kb-drop' }, [
      icon('download', 'kb-drop-icon'),
      h('p', { class: 'kb-drop-text', text: t('Drop text, Markdown or code files here – up to 2 MB each.') }),
      pickBtn,
      fileInput,
    ]);
    async function addFiles(list) {
      const files = [...(list || [])];
      if (!files.length || st.busy) return;
      st.busy = true;
      drop.classList.add('is-busy');
      const problems = [];
      let added = 0;
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        status.textContent = t('Adding {n} of {total}…', { n: i + 1, total: files.length });
        try {
          const content = await readFile(file);
          await LR.post(base + '/documents', { name: file.name, content });
          added++;
        } catch (err) {
          problems.push(err.status ? t('Could not add {name}: {error}', { name: file.name, error: err.message }) : err.message);
        }
      }
      st.busy = false;
      drop.classList.remove('is-busy');
      status.textContent = problems.join(' ');
      if (added) LR.toast(added === 1 ? t('Added {name}', { name: files.find(Boolean).name }) : t('Added {name}', { name: LR.plural(added, 'document') }));
      if (added) {
        kb.load();
        await loadDetail(d.id);
        if (problems.length && ui.status) ui.status.textContent = problems.join(' ');
      }
    }
    fileInput.addEventListener('change', () => { addFiles(fileInput.files); fileInput.value = ''; });
    let depth = 0;
    const hasFiles = (ev) => ev.dataTransfer && [...ev.dataTransfer.types].includes('Files');
    drop.addEventListener('dragenter', (ev) => { if (!hasFiles(ev)) return; ev.preventDefault(); depth++; drop.classList.add('is-drop'); });
    drop.addEventListener('dragover', (ev) => { if (hasFiles(ev)) ev.preventDefault(); });
    drop.addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth) drop.classList.remove('is-drop'); });
    drop.addEventListener('drop', (ev) => {
      if (!ev.dataTransfer || !ev.dataTransfer.files.length) return;
      ev.preventDefault();
      depth = 0;
      drop.classList.remove('is-drop');
      addFiles(ev.dataTransfer.files);
    });
    ui.status = status;

    const addCard = h('section', { class: 'card', 'aria-labelledby': 'kb-add-title' }, [
      h('div', { class: 'card-head' }, h('h2', { class: 'card-title', id: 'kb-add-title', text: t('Add documents') })),
      drop,
      status,
    ]);

    // documents
    const docs = d.documents;
    const docCard = h('section', { class: 'card', 'aria-labelledby': 'kb-docs-title' }, [
      h('div', { class: 'card-head' }, h('h2', { class: 'card-title', id: 'kb-docs-title', text: t('Documents') + ' (' + docs.length + ')' })),
      docs.length
        ? h('ul', { class: 'kb-list' }, docs.map((doc) => h('li', { class: 'kb-row' }, [
          h('span', { class: 'kb-row-icon', 'aria-hidden': 'true' }, icon('file')),
          h('span', { class: 'kb-row-main' }, [
            h('span', { class: 'kb-row-name', text: LR.str(doc.name, t('(unnamed file)')) }),
            h('span', { class: 'kb-row-meta', text: [LR.isNum(doc.chars) ? t('{n} characters', { n: fmt.int(doc.chars) }) : '', doc.created_at ? fmt.time(doc.created_at) : ''].filter(Boolean).join(' · ') }),
          ]),
          h('button', { type: 'button', class: 'icon-btn', 'aria-label': t('Delete document') + ': ' + LR.str(doc.name), title: t('Delete document'), onclick: () => deleteDoc(doc) }, icon('trash')),
        ])))
        : LR.empty({ icon: 'file', title: t('No documents yet'), text: t('Drop text, Markdown or code files here – up to 2 MB each.') }),
    ]);

    // search
    const q = h('input', { class: 'input', type: 'search', placeholder: t('What are you looking for?'), autocomplete: 'off', 'aria-label': t('Search this collection') });
    const searchBtn = LR.button({ text: t('Search'), icon: 'search', variant: 'secondary', type: 'submit' });
    const results = h('div', { class: 'kb-results', 'aria-live': 'polite' });
    const sRow = h('div', { class: 'input-row' }, [q, searchBtn]);
    const sForm = h('form', { class: 'form', role: 'search', novalidate: true }, sRow);
    sForm.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const query = q.value.trim();
      if (!query) { results.replaceChildren(); return; }
      LR.setBusy(searchBtn, true);
      try {
        const data = await LR.post(base + '/search', { query, k: 8 });
        const list = Array.isArray(data.results) ? data.results : [];
        results.replaceChildren(list.length
          ? h('ol', { class: 'kb-hits' }, list.map((r) => h('li', { class: 'kb-hit' }, [
            h('p', { class: 'kb-hit-doc' }, [icon('file'), h('span', { text: LR.str(r.document, '') })]),
            h('p', { class: 'kb-hit-text', text: LR.str(r.text).slice(0, 600) }),
          ])))
          : h('p', { class: 'muted', text: t('No matches') }));
      } catch (err) {
        results.replaceChildren(LR.callout({ tone: 'error', text: err.message }));
      } finally {
        LR.setBusy(searchBtn, false);
      }
    });
    const searchCard = h('section', { class: 'card', 'aria-labelledby': 'kb-search-title' }, [
      h('div', { class: 'card-head' }, h('h2', { class: 'card-title', id: 'kb-search-title', text: t('Search this collection') })),
      sForm,
      results,
    ]);

    ui.body.replaceChildren(
      h('div', { class: 'kb-detail-head' }, [
        h('a', { class: 'btn btn-ghost btn-sm', href: '#/knowledge' }, [icon('chevron-left'), h('span', { text: t('Back to collections') })]),
        h('h2', { class: 'kb-detail-title', text: d.name }),
        LR.button({ text: t('Delete collection'), icon: 'trash', size: 'sm', variant: 'ghost', onclick: deleteCollection }),
      ]),
      addCard, docCard, searchCard,
    );
  }

  async function deleteDoc(doc) {
    const d = st.detail;
    const ok = await LR.confirm({ title: t('Delete this document?'), message: t('It is removed from the collection. The original file stays untouched.'), confirmText: t('Delete'), danger: true });
    if (!ok) return;
    try {
      await LR.post('/api/knowledge/' + encodeURIComponent(d.id) + '/documents/' + encodeURIComponent(doc.id) + '/delete', {});
      LR.toast(t('Document deleted'));
      kb.load();
      loadDetail(d.id);
    } catch (err) {
      LR.toast(t('Could not delete: ') + err.message, 'error');
    }
  }

  async function deleteCollection() {
    const d = st.detail;
    const ok = await LR.confirm({ title: t('Delete this collection?'), message: t('All documents in it are removed from LowRAM AI. Your original files stay untouched.'), confirmText: t('Delete collection'), danger: true });
    if (!ok) return;
    try {
      await LR.post('/api/knowledge/' + encodeURIComponent(d.id) + '/delete', {});
      LR.toast(t('Collection deleted'));
      await kb.load();
      LR.go('#/knowledge');
    } catch (err) {
      LR.toast(t('Could not delete: ') + err.message, 'error');
    }
  }

  /* ------------------------------------------------------------ view */

  function create() {
    const root = h('section', { class: 'view view-page', 'aria-labelledby': 'kb-title' });
    const head = LR.pageHead({ title: t('Knowledge base'), sub: t('Collect your own documents so LowRAM AI can answer with them. Everything stays on this computer.') });
    head.querySelector('h1').id = 'kb-title';
    ui.body = h('div', { class: 'kb-body' });
    root.append(h('div', { class: 'page' }, [head, ui.body]));
    return root;
  }

  async function show(params) {
    const id = params && params[0] ? params[0] : null;
    st.collectionId = id;
    if (kb.available === null) {
      ui.body.replaceChildren(h('div', { class: 'card' }, LR.skeleton(4)));
      await kb.load();
    }
    if (kb.available === false) {
      ui.body.replaceChildren(LR.unavailable(t('Knowledge base')));
      return;
    }
    if (id) loadDetail(id);
    else {
      renderList();
      kb.load().then(() => { if (!st.collectionId && LR.currentRoute() === 'knowledge') renderList(); });
    }
  }

  LR.registerView('knowledge', { title: t('Knowledge'), create, show, hide() {} });
})();
