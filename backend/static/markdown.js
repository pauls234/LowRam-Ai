/* LowRAM AI Studio – safe Markdown renderer.
 *
 * Builds DOM nodes only (createElement / textContent), so model output can
 * never inject markup or script. Supports paragraphs, headings, bold/italic/
 * strikethrough, inline code, fenced code blocks (language label, copy button,
 * light highlighting), nested lists, blockquotes, tables, rules and links
 * (http/https/mailto only). The incremental renderer re-renders only the
 * blocks that changed, so streaming answers do not flicker.
 */
(function () {
  'use strict';

  const LR = window.LR;
  const h = LR.h;
  const SAFE_URL = /^(https?:\/\/|mailto:)/i;

  /* ------------------------------------------------------------ block parsing */

  const RE_FENCE = /^ {0,3}(`{3,}|~{3,})\s*([^`\s]*)[^`]*$/;
  const RE_HEADING = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t#]*$/;
  const RE_HR = /^ {0,3}((\*[ \t]*){3,}|(-[ \t]*){3,}|(_[ \t]*){3,})$/;
  const RE_QUOTE = /^ {0,3}>[ ]?(.*)$/;
  const RE_ITEM = /^( *)([-*+]|\d{1,9}[.)])([ \t]+(.*)|$)/;
  const RE_TABLE_SEP = /^ *\|? *:?-{1,}:? *(\| *:?-{1,}:? *)*\|? *$/;

  function isBlank(line) {
    return !line || !line.trim();
  }
  function indentOf(line) {
    const m = /^ */.exec(line);
    return m ? m[0].length : 0;
  }
  function startsBlock(lines, i) {
    const line = lines[i];
    if (RE_FENCE.test(line) || RE_HEADING.test(line) || RE_HR.test(line) || RE_QUOTE.test(line)) return true;
    const item = RE_ITEM.exec(line);
    if (item && item[4] !== undefined && item[4].trim() && indentOf(line) < 4) {
      return !/^\d/.test(item[2]) || /^1[.)]$/.test(item[2]);
    }
    return isTableStart(lines, i);
  }
  function isTableStart(lines, i) {
    return lines[i].includes('|') && i + 1 < lines.length && lines[i + 1].includes('-') && RE_TABLE_SEP.test(lines[i + 1]);
  }

  function splitRow(line) {
    let s = line.trim();
    if (s.startsWith('|')) s = s.slice(1);
    if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
    const cells = [];
    let cur = '';
    let inCode = false;
    for (let i = 0; i < s.length; i++) {
      const c = s[i];
      if (c === '\\' && s[i + 1] === '|') { cur += '|'; i++; continue; }
      if (c === '`') inCode = !inCode;
      if (c === '|' && !inCode) { cells.push(cur.trim()); cur = ''; continue; }
      cur += c;
    }
    cells.push(cur.trim());
    return cells;
  }

  /** Split text into blocks: {type, raw, ...}. */
  function parseBlocks(text) {
    const lines = text.split('\n');
    const blocks = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (isBlank(line)) { i++; continue; }
      const start = i;
      let m;

      if ((m = RE_FENCE.exec(line))) {
        const fence = m[1];
        const lang = (m[2] || '').toLowerCase().replace(/[^a-z0-9+#._-]/g, '').slice(0, 24);
        const fenceIndent = indentOf(line);
        const body = [];
        let closed = false;
        i++;
        while (i < lines.length) {
          const l = lines[i];
          const t = l.trim();
          if (t.startsWith(fence[0].repeat(fence.length)) && /^[`~]+$/.test(t) && t.length >= fence.length) {
            closed = true;
            i++;
            break;
          }
          body.push(fenceIndent ? l.replace(new RegExp('^ {0,' + fenceIndent + '}'), '') : l);
          i++;
        }
        blocks.push({ type: 'code', lang, text: body.join('\n'), closed, raw: lines.slice(start, i).join('\n') });
        continue;
      }
      if ((m = RE_HEADING.exec(line))) {
        blocks.push({ type: 'heading', level: m[1].length, text: m[2] || '', raw: line });
        i++;
        continue;
      }
      if (RE_HR.test(line)) {
        blocks.push({ type: 'hr', raw: line });
        i++;
        continue;
      }
      if (RE_QUOTE.test(line)) {
        const inner = [];
        while (i < lines.length && !isBlank(lines[i])) {
          const q = RE_QUOTE.exec(lines[i]);
          if (q) inner.push(q[1]);
          else if (startsBlock(lines, i)) break;
          else inner.push(lines[i]);
          i++;
        }
        blocks.push({ type: 'quote', text: inner.join('\n'), raw: lines.slice(start, i).join('\n') });
        continue;
      }
      if ((m = RE_ITEM.exec(line)) && indentOf(line) < 4) {
        const list = parseList(lines, i);
        i = list.end;
        list.raw = lines.slice(start, i).join('\n');
        blocks.push(list);
        continue;
      }
      if (isTableStart(lines, i)) {
        const header = splitRow(line);
        const align = splitRow(lines[i + 1]).map((c) => (c.startsWith(':') && c.endsWith(':') ? 'center' : c.endsWith(':') ? 'right' : c.startsWith(':') ? 'left' : null));
        const rows = [];
        i += 2;
        while (i < lines.length && !isBlank(lines[i]) && lines[i].includes('|')) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        blocks.push({ type: 'table', header, align, rows, raw: lines.slice(start, i).join('\n') });
        continue;
      }
      const para = [line];
      i++;
      while (i < lines.length && !isBlank(lines[i]) && !startsBlock(lines, i)) {
        para.push(lines[i]);
        i++;
      }
      blocks.push({ type: 'para', text: para.join('\n'), raw: para.join('\n') });
    }
    return blocks;
  }

  function parseList(lines, i) {
    const first = RE_ITEM.exec(lines[i]);
    const baseIndent = first[1].length;
    const ordered = /^\d/.test(first[2]);
    const start = ordered ? parseInt(first[2], 10) : 1;
    const items = [];
    let loose = false;
    let current = null;
    let sawBlank = false;
    while (i < lines.length) {
      const line = lines[i];
      if (isBlank(line)) {
        sawBlank = true;
        if (current) current.lines.push('');
        i++;
        continue;
      }
      const m = RE_ITEM.exec(line);
      const indent = indentOf(line);
      if (m && indent >= baseIndent && indent < baseIndent + 2 && /^\d/.test(m[2]) === ordered) {
        if (sawBlank && items.length) loose = true;
        const content = m[4] === undefined ? '' : m[4];
        current = { lines: [content], contentIndent: indent + m[2].length + 1 };
        items.push(current);
        sawBlank = false;
        i++;
        continue;
      }
      if (!current) break;
      if (indent >= baseIndent + 2) {
        current.lines.push(line.slice(Math.min(indent, current.contentIndent)));
        if (sawBlank) loose = true;
        sawBlank = false;
        i++;
        continue;
      }
      if (!sawBlank && !startsBlock(lines, i)) {
        current.lines.push(line.trim());
        i++;
        continue;
      }
      break;
    }
    for (const item of items) {
      while (item.lines.length && item.lines[item.lines.length - 1] === '') item.lines.pop();
    }
    return { type: 'list', ordered, start, loose, items: items.map((it) => it.lines.join('\n')), end: i };
  }

  /* ------------------------------------------------------------ inline parsing */

  const PUNCT = /[!-/:-@[-`{-~]/;

  function findClosing(text, delim, from) {
    let j = from;
    while ((j = text.indexOf(delim, j)) !== -1) {
      if (text[j - 1] === '\\') { j += delim.length; continue; }
      const before = text[j - 1];
      if (before && !/\s/.test(before) && j > from) {
        if (delim[0] === '_' && /[A-Za-z0-9]/.test(text[j + delim.length] || '')) { j += delim.length; continue; }
        if (delim === '*' && text[j + 1] === '*') { j += 2; continue; }
        return j;
      }
      j += delim.length;
    }
    return -1;
  }

  function findBracket(text, from) {
    let depth = 0;
    for (let j = from; j < text.length; j++) {
      const c = text[j];
      if (c === '\\') { j++; continue; }
      if (c === '[') depth++;
      else if (c === ']') {
        depth--;
        if (depth === 0) return j;
      }
    }
    return -1;
  }

  function findParen(text, from) {
    let depth = 0;
    for (let j = from; j < text.length; j++) {
      const c = text[j];
      if (c === '\\') { j++; continue; }
      if (c === '(') depth++;
      else if (c === ')') {
        depth--;
        if (depth === 0) return j;
      }
      else if (c === '\n') return -1;
    }
    return -1;
  }

  function link(url, children) {
    return h('a', { href: url, rel: 'noopener noreferrer', target: '_blank' }, children);
  }

  function trimUrl(url) {
    let u = url;
    while (/[.,;:!?'"*_]$/.test(u)) u = u.slice(0, -1);
    while (u.endsWith(')') && (u.match(/\(/g) || []).length < (u.match(/\)/g) || []).length) u = u.slice(0, -1);
    return u;
  }

  /** Append inline content of `text` to `parent`. */
  function inline(text, parent, noLinks) {
    let buf = '';
    const flush = () => {
      if (buf) { parent.append(buf); buf = ''; }
    };
    let i = 0;
    while (i < text.length) {
      const c = text[i];
      const rest = text.slice(i);

      if (c === '\\' && i + 1 < text.length && PUNCT.test(text[i + 1])) {
        buf += text[i + 1];
        i += 2;
        continue;
      }
      if (c === '\n') {
        buf = buf.replace(/ +$/, '');
        flush();
        parent.append(h('br'));
        i++;
        continue;
      }
      if (c === '`') {
        const run = /^`+/.exec(rest)[0];
        const close = text.indexOf(run, i + run.length);
        if (close !== -1 && text[close + run.length] !== '`') {
          let code = text.slice(i + run.length, close).replace(/\n/g, ' ');
          if (code.startsWith(' ') && code.endsWith(' ') && code.trim()) code = code.slice(1, -1);
          flush();
          parent.append(h('code', { class: 'md-code', text: code }));
          i = close + run.length;
          continue;
        }
        buf += run;
        i += run.length;
        continue;
      }
      if (c === '*' || c === '_' || c === '~') {
        const run = /^(\*+|_+|~+)/.exec(rest)[0];
        const next = text[i + run.length];
        const prev = text[i - 1];
        const canOpen = next && !/\s/.test(next) && !(c === '_' && prev && /[A-Za-z0-9]/.test(prev));
        if (canOpen) {
          let handled = false;
          const tries = c === '~' ? (run.length >= 2 ? ['~~'] : []) : run.length >= 3 ? [c.repeat(3), c.repeat(2), c] : run.length === 2 ? [c.repeat(2), c] : [c];
          for (const delim of tries) {
            const close = findClosing(text, delim, i + delim.length);
            if (close === -1) continue;
            const inner = text.slice(i + delim.length, close);
            if (!inner.trim()) continue;
            flush();
            let el;
            if (c === '~') el = h('del');
            else if (delim.length === 3) { el = h('strong'); const em = h('em'); inline(inner, em, noLinks); el.append(em); }
            else el = h(delim.length === 2 ? 'strong' : 'em');
            if (delim.length !== 3) inline(inner, el, noLinks);
            parent.append(el);
            i = close + delim.length;
            handled = true;
            break;
          }
          if (handled) continue;
        }
        buf += run;
        i += run.length;
        continue;
      }
      if (!noLinks && (c === '[' || (c === '!' && text[i + 1] === '['))) {
        const image = c === '!';
        const open = image ? i + 1 : i;
        const closeBracket = findBracket(text, open);
        if (closeBracket !== -1 && text[closeBracket + 1] === '(') {
          const closeParen = findParen(text, closeBracket + 1);
          if (closeParen !== -1) {
            const label = text.slice(open + 1, closeBracket);
            let target = text.slice(closeBracket + 2, closeParen).trim();
            if (target.startsWith('<') && target.includes('>')) target = target.slice(1, target.indexOf('>'));
            else target = target.split(/\s+/)[0] || '';
            flush();
            if (SAFE_URL.test(target)) {
              const a = link(target, null);
              if (image) a.textContent = label || target;
              else inline(label, a, true);
              parent.append(a);
            } else if (image) {
              parent.append(label);
            } else {
              inline(label, parent, true);
            }
            i = closeParen + 1;
            continue;
          }
        }
        buf += c;
        i++;
        continue;
      }
      if (c === '<') {
        const auto = /^<((?:https?:\/\/|mailto:)[^\s<>]+)>/i.exec(rest);
        if (auto && !noLinks) {
          flush();
          parent.append(link(auto[1], auto[1]));
          i += auto[0].length;
          continue;
        }
        const br = /^<br\s*\/?>/i.exec(rest);
        if (br) {
          flush();
          parent.append(h('br'));
          i += br[0].length;
          continue;
        }
      }
      if (!noLinks && (c === 'h' || c === 'H') && /^https?:\/\//i.test(rest) && (!text[i - 1] || /[\s(]/.test(text[i - 1]))) {
        const m = /^https?:\/\/[^\s<>]+/i.exec(rest);
        const url = trimUrl(m[0]);
        if (url.length > 8) {
          flush();
          parent.append(link(url, url));
          i += url.length;
          continue;
        }
      }
      buf += c;
      i++;
    }
    flush();
  }

  /* ------------------------------------------------------------ syntax highlighting (closed blocks) */

  const KEYWORDS = new Set((
    'abstract and as assert async await break case catch class const continue def default defer del delete do elif else enum export ' +
    'extends false final finally fn for from func function go if impl import in instanceof interface is lambda let loop match mod mut ' +
    'new nil none not null or package pass private protected pub public raise return self static struct super switch this throw throws ' +
    'true try type typeof use var void while with yield True False None echo then fi done esac local select where insert update into ' +
    'values create table set join on order by group limit int str bool float string boolean'
  ).split(' '));
  const HASH_COMMENT = new Set(['python', 'py', 'sh', 'bash', 'zsh', 'shell', 'ruby', 'rb', 'yaml', 'yml', 'toml', 'r', 'perl', 'dockerfile', 'makefile', 'ini', 'conf', 'nix', 'elixir']);
  const DASH_COMMENT = new Set(['sql', 'lua', 'haskell', 'hs']);
  const NO_HIGHLIGHT = new Set(['', 'text', 'txt', 'plain', 'plaintext', 'markdown', 'md', 'diff', 'log', 'output', 'console']);

  function highlight(code, lang, target) {
    if (NO_HIGHLIGHT.has(lang) || code.length > 60000) {
      target.textContent = code;
      return;
    }
    const parts = ['\\/\\*[\\s\\S]*?\\*\\/'];
    if (HASH_COMMENT.has(lang)) parts.push('#[^\\n]*');
    else if (DASH_COMMENT.has(lang)) parts.push('--[^\\n]*');
    else parts.push('\\/\\/[^\\n]*');
    if (lang === 'python' || lang === 'py') parts.push('"""[\\s\\S]*?"""', "'''[\\s\\S]*?'''");
    parts.push('"(?:\\\\.|[^"\\\\\\n])*"', "'(?:\\\\.|[^'\\\\\\n])*'", '`(?:\\\\.|[^`\\\\])*`');
    parts.push('\\b\\d+(?:\\.\\d+)?\\b', '[A-Za-z_$][\\w$]*');
    const re = new RegExp(parts.join('|'), 'g');
    let last = 0;
    let m;
    const frag = document.createDocumentFragment();
    while ((m = re.exec(code))) {
      const tok = m[0];
      if (m.index > last) frag.append(code.slice(last, m.index));
      let cls = null;
      const ch = tok[0];
      if (tok.startsWith('/*') || tok.startsWith('//') || (ch === '#' && HASH_COMMENT.has(lang)) || (tok.startsWith('--') && DASH_COMMENT.has(lang))) cls = 'tok-c';
      else if (ch === '"' || ch === "'" || ch === '`') cls = 'tok-s';
      else if (/^\d/.test(tok)) cls = 'tok-n';
      else if (KEYWORDS.has(tok)) cls = 'tok-k';
      else if (code[re.lastIndex] === '(') cls = 'tok-f';
      frag.append(cls ? h('span', { class: cls, text: tok }) : tok);
      last = re.lastIndex;
    }
    if (last < code.length) frag.append(code.slice(last));
    target.replaceChildren(frag);
  }

  /* ------------------------------------------------------------ block rendering */

  function codeBlock(block, final) {
    const codeEl = h('code', { class: block.lang ? 'language-' + block.lang : null });
    const finished = block.closed || final;
    if (finished) highlight(block.text, block.lang, codeEl);
    else codeEl.textContent = block.text;
    const pre = h('pre', { tabindex: '0', 'aria-label': (block.lang || 'Code') + ' code block' }, codeEl);
    const head = h('div', { class: 'code-head' }, [
      h('span', { class: 'code-lang', text: block.lang || 'code' }),
      LR.copyButton({ getText: () => codeEl.textContent, cls: 'code-copy', label: 'Copy', ariaLabel: 'Copy code' }),
    ]);
    const el = h('div', { class: 'code-block' }, [head, pre]);
    el._code = codeEl;
    return el;
  }

  function renderBlock(block, final, tight) {
    switch (block.type) {
      case 'code':
        return codeBlock(block, final);
      case 'heading': {
        const level = Math.min(6, block.level + 1); // h1 in answers becomes h2 (the page owns h1)
        const el = h('h' + level, { class: 'md-h md-h' + block.level });
        inline(block.text, el);
        return el;
      }
      case 'hr':
        return h('hr');
      case 'quote': {
        const el = h('blockquote');
        for (const b of parseBlocks(block.text)) el.append(renderBlock(b, final));
        return el;
      }
      case 'list': {
        const el = h(block.ordered ? 'ol' : 'ul', { start: block.ordered && block.start !== 1 ? String(block.start) : null });
        for (const src of block.items) {
          const li = h('li');
          const inner = parseBlocks(src);
          inner.forEach((b, idx) => {
            if (!block.loose && b.type === 'para' && idx === 0) inline(b.text, li);
            else li.append(renderBlock(b, final, !block.loose));
          });
          el.append(li);
        }
        return el;
      }
      case 'table': {
        const thead = h('thead', null, h('tr', null, block.header.map((cell, idx) => {
          const th = h('th', { scope: 'col', class: block.align[idx] ? 'align-' + block.align[idx] : null });
          inline(cell, th);
          return th;
        })));
        const tbody = h('tbody', null, block.rows.map((row) => h('tr', null, block.header.map((_, idx) => {
          const td = h('td', { class: block.align[idx] ? 'align-' + block.align[idx] : null });
          inline(row[idx] || '', td);
          return td;
        }))));
        return h('div', { class: 'table-wrap', tabindex: '0', role: 'group', 'aria-label': 'Table' }, h('table', null, [thead, tbody]));
      }
      default: {
        const el = h('p', { class: tight ? 'md-tight' : null });
        inline(block.text, el);
        return el;
      }
    }
  }

  function normalize(text) {
    return String(text || '').replace(/\r\n?/g, '\n').replace(/\t/g, '    ');
  }

  const CARET_HOSTS = new Set(['P', 'LI', 'UL', 'OL', 'BLOCKQUOTE', 'H2', 'H3', 'H4', 'H5', 'H6', 'H7', 'CODE', 'PRE', 'DIV', 'STRONG', 'EM', 'DEL']);
  function caretHost(node) {
    let host = node;
    for (;;) {
      if (host.classList && host.classList.contains('code-block')) { host = host._code; break; }
      const last = host.lastElementChild;
      if (!last || !CARET_HOSTS.has(last.tagName) || (last.tagName === 'DIV' && !last.classList.contains('code-block'))) break;
      if (host.lastChild !== last && host.lastChild.nodeType === 3 && host.lastChild.textContent.trim()) break;
      host = last;
    }
    return host;
  }

  /**
   * Incremental renderer bound to `root`. render(text, streaming) updates only
   * the blocks that changed since the previous call.
   */
  function createRenderer(root) {
    let prev = [];
    let caret = null;
    return {
      render(text, streaming) {
        const blocks = parseBlocks(normalize(text));
        const final = !streaming;
        if (caret) { caret.remove(); caret = null; }
        let i = 0;
        while (i < blocks.length && i < prev.length && prev[i].raw === blocks[i].raw && (prev[i].final === final || blocks[i].type !== 'code' || blocks[i].closed)) i++;
        // Fast path: a growing, still-open code block only appends text.
        if (i < blocks.length && i === prev.length - 1 && i === blocks.length - 1 && streaming) {
          const p = prev[i];
          const b = blocks[i];
          if (p.type === 'code' && b.type === 'code' && !p.closed && !b.closed && p.lang === b.lang && b.text.startsWith(p.text)) {
            p.node._code.append(b.text.slice(p.text.length));
            p.raw = b.raw;
            p.text = b.text;
            i = blocks.length;
          }
        }
        for (let j = prev.length - 1; j >= i; j--) prev[j].node.remove();
        prev = prev.slice(0, i);
        for (; i < blocks.length; i++) {
          const b = blocks[i];
          const node = renderBlock(b, final);
          root.append(node);
          prev.push({ raw: b.raw, type: b.type, lang: b.lang, text: b.text, closed: b.closed, final, node });
        }
        if (streaming) {
          caret = h('span', { class: 'caret', 'aria-hidden': 'true' });
          const last = prev.length ? prev[prev.length - 1].node : root;
          caretHost(last).append(caret);
        }
      },
      reset() {
        prev = [];
        caret = null;
        root.replaceChildren();
      },
    };
  }

  /** Split "<think>…</think>answer" into reasoning and answer parts. */
  function splitThinking(text) {
    const s = String(text || '');
    const m = /^\s*<think>/i.exec(s);
    if (!m) return { reasoning: '', answer: s, open: false };
    const rest = s.slice(m[0].length);
    const end = rest.search(/<\/think>/i);
    if (end === -1) return { reasoning: rest, answer: '', open: true };
    return { reasoning: rest.slice(0, end), answer: rest.slice(end + 8).replace(/^\s+/, ''), open: false };
  }

  LR.md = {
    parseBlocks,
    createRenderer,
    splitThinking,
    /** One-shot render of trusted-as-text Markdown into el. */
    render(el, text) {
      el.replaceChildren();
      createRenderer(el).render(text, false);
      return el;
    },
  };
})();
