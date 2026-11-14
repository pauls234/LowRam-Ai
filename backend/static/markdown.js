/* LowRAM AI Studio – safe Markdown renderer.
 *
 * Builds DOM nodes only (createElement / textContent), so model output can
 * never inject markup or script. Supports paragraphs, headings, bold/italic/
 * strikethrough, inline code, fenced code blocks (language label, copy button,
 * light highlighting), nested lists, blockquotes, tables, rules and links
 * (http/https/mailto only).
 *
 * Model output is untrusted, so parsing is linear-time by construction:
 * block regexes cannot backtrack (and very long lines skip them), closer and
 * bracket scans are memoised, nesting is capped, and while streaming only the
 * last block is re-parsed.
 */
(function () {
  'use strict';

  const LR = window.LR;
  const h = LR.h;
  const SAFE_URL = /^(https?:\/\/|mailto:)/i;
  const MAX_LINE = 2000; // longer lines are plain paragraph text (no block syntax)
  const MAX_DEPTH = 8; // deeper quotes / lists / inline nesting render as plain text
  const MAX_HIGHLIGHT = 20000;
  const MAX_URL = 2048;

  /* ------------------------------------------------------------ block parsing */

  const RE_FENCE = /^ {0,3}(`{3,}|~{3,})([^`\n]*)$/;
  const RE_HEADING = /^ {0,3}(#{1,6})(?:[ \t]+(.*))?$/;
  const RE_QUOTE = /^ {0,3}>[ ]?(.*)$/;
  const RE_ITEM = /^( *)([-*+]|\d{1,9}[.)])(?:[ \t]+(.*)|$)/;
  const RE_TABLE_SEP = /^ *\|? *:?-+:? *(?:\| *:?-+:? *)*\|? *$/;

  const isBlank = (line) => !line || !line.trim();
  const tooLong = (line) => line.length > MAX_LINE;
  function indentOf(line) {
    let i = 0;
    while (line.charCodeAt(i) === 32) i++;
    return i;
  }
  const isSpace = (c) => c === ' ' || c === '\t';

  /** Thematic break: 3+ of the same of * - _ with optional spaces (checked by hand). */
  function isHr(line) {
    if (tooLong(line) || indentOf(line) > 3) return false;
    let mark = '';
    let count = 0;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (isSpace(c)) continue;
      if (c !== '*' && c !== '-' && c !== '_') return false;
      if (mark && c !== mark) return false;
      mark = c;
      count++;
    }
    return count >= 3;
  }

  /** "## Title ##" -> "Title" without a backtracking regex. */
  function headingText(raw) {
    const t = raw || '';
    let end = t.length;
    while (end && isSpace(t[end - 1])) end--;
    let j = end;
    while (j && t[j - 1] === '#') j--;
    if (j < end && (j === 0 || isSpace(t[j - 1]))) {
      end = j;
      while (end && isSpace(t[end - 1])) end--;
    }
    return t.slice(0, end);
  }

  function fenceOf(line) {
    if (tooLong(line)) return null;
    const m = RE_FENCE.exec(line);
    if (!m) return null;
    const info = m[2].trim().split(/\s+/)[0] || '';
    return { fence: m[1], lang: info.toLowerCase().replace(/[^a-z0-9+#._-]/g, '').slice(0, 24) };
  }

  function itemOf(line) {
    return tooLong(line) ? null : RE_ITEM.exec(line);
  }

  function isTableStart(lines, i) {
    const a = lines[i];
    const b = lines[i + 1];
    return b !== undefined && !tooLong(a) && !tooLong(b) && a.includes('|') && b.includes('-') && RE_TABLE_SEP.test(b);
  }

  function startsBlock(lines, i) {
    const line = lines[i];
    if (tooLong(line)) return false;
    if (fenceOf(line) || RE_HEADING.test(line) || isHr(line) || RE_QUOTE.test(line)) return true;
    const item = itemOf(line);
    if (item && item[3] !== undefined && item[3].trim() && indentOf(line) < 4) {
      return !/^\d/.test(item[2]) || /^1[.)]$/.test(item[2]);
    }
    return isTableStart(lines, i);
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
    return cells.slice(0, 64);
  }

  /** Parse lines[from..] into blocks: {type, raw, line, ...}. */
  function parseLines(lines, from) {
    const blocks = [];
    let i = from || 0;
    while (i < lines.length) {
      const line = lines[i];
      if (isBlank(line)) { i++; continue; }
      const start = i;
      let m;
      const long = tooLong(line);

      const fence = long ? null : fenceOf(line);
      if (fence) {
        const fenceIndent = indentOf(line);
        const body = [];
        let closed = false;
        i++;
        while (i < lines.length) {
          const l = lines[i];
          const t = l.trim();
          if (t.length >= fence.fence.length && t[0] === fence.fence[0] && !t.split('').some((c) => c !== t[0])) {
            closed = true;
            i++;
            break;
          }
          body.push(fenceIndent ? l.slice(Math.min(fenceIndent, indentOf(l))) : l);
          i++;
        }
        blocks.push({ type: 'code', lang: fence.lang, text: body.join('\n'), closed, raw: lines.slice(start, i).join('\n'), line: start });
        continue;
      }
      if (!long && (m = RE_HEADING.exec(line))) {
        blocks.push({ type: 'heading', level: m[1].length, text: headingText(m[2]), raw: line, line: start });
        i++;
        continue;
      }
      if (!long && isHr(line)) {
        blocks.push({ type: 'hr', raw: line, line: start });
        i++;
        continue;
      }
      if (!long && RE_QUOTE.test(line)) {
        const inner = [];
        while (i < lines.length && !isBlank(lines[i])) {
          const q = tooLong(lines[i]) ? null : RE_QUOTE.exec(lines[i]);
          if (q) inner.push(q[1]);
          else if (startsBlock(lines, i)) break;
          else inner.push(lines[i]);
          i++;
        }
        blocks.push({ type: 'quote', text: inner.join('\n'), raw: lines.slice(start, i).join('\n'), line: start });
        continue;
      }
      if (!long && itemOf(line) && indentOf(line) < 4) {
        const list = parseList(lines, i);
        i = list.end;
        list.raw = lines.slice(start, i).join('\n');
        list.line = start;
        blocks.push(list);
        continue;
      }
      if (isTableStart(lines, i)) {
        const header = splitRow(line);
        const align = splitRow(lines[i + 1]).map((c) => (c.startsWith(':') && c.endsWith(':') ? 'center' : c.endsWith(':') ? 'right' : c.startsWith(':') ? 'left' : null));
        const rows = [];
        i += 2;
        while (i < lines.length && !isBlank(lines[i]) && lines[i].includes('|') && !tooLong(lines[i])) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        blocks.push({ type: 'table', header, align, rows, raw: lines.slice(start, i).join('\n'), line: start });
        continue;
      }
      const para = [line];
      i++;
      while (i < lines.length && !isBlank(lines[i]) && !startsBlock(lines, i)) {
        para.push(lines[i]);
        i++;
      }
      blocks.push({ type: 'para', text: para.join('\n'), raw: para.join('\n'), line: start });
    }
    return blocks;
  }

  function parseBlocks(text) {
    return parseLines(text.split('\n'), 0);
  }

  function parseList(lines, i) {
    const first = itemOf(lines[i]);
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
      const m = itemOf(line);
      const indent = indentOf(line);
      if (m && indent >= baseIndent && indent < baseIndent + 2 && /^\d/.test(m[2]) === ordered) {
        if (sawBlank && items.length) loose = true;
        current = { lines: [m[3] === undefined ? '' : m[3]], contentIndent: indent + m[2].length + 1 };
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
  const URL_TRAIL = new Set(['.', ',', ';', ':', '!', '?', "'", '"', '*', '_']);
  const RE_AUTOLINK = /<((?:https?:\/\/|mailto:)[^\s<>]{1,2048})>/iy;
  const RE_BR = /<br\s{0,8}\/?>/iy;
  const RE_URL = /https?:\/\/[^\s<>]{1,2048}/iy;
  const RE_HTTP = /https?:\/\//iy;

  /** Matching closer index for every opener (one linear pass, escapes skipped). */
  function matchPairs(text, open, close, lineBound) {
    const match = new Map();
    const stack = [];
    for (let j = 0; j < text.length; j++) {
      const c = text[j];
      if (c === '\\') { j++; continue; }
      if (lineBound && c === '\n') { stack.length = 0; continue; }
      if (c === open) stack.push(j);
      else if (c === close && stack.length) match.set(stack.pop(), j);
    }
    return match;
  }

  /** First valid closing delimiter at or after `from`; failures are remembered. */
  function findClosing(ctx, delim, from) {
    const failedFrom = ctx.noClose[delim];
    if (failedFrom !== undefined && from >= failedFrom) return -1;
    const text = ctx.text;
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
    ctx.noClose[delim] = failedFrom === undefined ? from : Math.min(failedFrom, from);
    return -1;
  }

  /** Closing backtick run of exactly `len` at or after `from` (memoised failures). */
  function findCodeClose(ctx, len, from) {
    const key = '`' + len;
    const failedFrom = ctx.noClose[key];
    if (failedFrom !== undefined && from >= failedFrom) return -1;
    const text = ctx.text;
    const run = '`'.repeat(len);
    let p = from;
    while ((p = text.indexOf(run, p)) !== -1) {
      if (text[p + len] !== '`' && text[p - 1] !== '`') return p;
      while (text[p] === '`') p++;
    }
    ctx.noClose[key] = failedFrom === undefined ? from : Math.min(failedFrom, from);
    return -1;
  }

  function trimUrl(u) {
    let opens = 0;
    let closes = 0;
    for (let k = 0; k < u.length; k++) {
      if (u[k] === '(') opens++;
      else if (u[k] === ')') closes++;
    }
    let end = u.length;
    for (;;) {
      const c = u[end - 1];
      if (URL_TRAIL.has(c)) { end--; continue; }
      if (c === ')' && closes > opens) { closes--; end--; continue; }
      break;
    }
    return u.slice(0, end);
  }

  function hostOf(url) {
    try {
      return new URL(url).hostname.replace(/^www\./, '').toLowerCase();
    } catch (_) {
      return '';
    }
  }

  /** Host named by a link label that looks like a URL ("example.com/x"), else ''. */
  function labelHost(label) {
    const t = label.trim();
    if (!t || t.length > 300 || /\s/.test(t)) return '';
    const m = /^(?:https?:\/\/)?((?:[a-z0-9-]+\.)+[a-z]{2,})(?:[/:?#].*)?$/i.exec(t);
    return m ? m[1].replace(/^www\./, '').toLowerCase() : '';
  }

  function link(url, label) {
    return h('a', { href: url, rel: 'noopener noreferrer', target: '_blank', title: url.length > 300 ? url.slice(0, 300) + '…' : url }, label === undefined ? null : label);
  }

  /** Append inline content of `text` to `parent`. */
  function inline(text, parent, noLinks, depth) {
    const level = depth || 0;
    if (level > MAX_DEPTH) {
      parent.append(text);
      return;
    }
    const ctx = { text, noClose: Object.create(null), brackets: null, parens: null };
    let buf = '';
    const flush = () => {
      if (buf) { parent.append(buf); buf = ''; }
    };
    let i = 0;
    while (i < text.length) {
      const c = text[i];

      if (c === '\\' && i + 1 < text.length && PUNCT.test(text[i + 1])) {
        buf += text[i + 1];
        i += 2;
        continue;
      }
      if (c === '\n') {
        let end = buf.length;
        while (end && buf[end - 1] === ' ') end--;
        buf = buf.slice(0, end);
        flush();
        parent.append(h('br'));
        i++;
        continue;
      }
      if (c === '`') {
        let k = i;
        while (text[k] === '`') k++;
        const len = k - i;
        const close = findCodeClose(ctx, len, k);
        if (close !== -1) {
          let code = text.slice(k, close).replace(/\n/g, ' ');
          if (code.length > 1 && code[0] === ' ' && code[code.length - 1] === ' ' && code.trim()) code = code.slice(1, -1);
          flush();
          parent.append(h('code', { class: 'md-code', text: code }));
          i = close + len;
          continue;
        }
        buf += text.slice(i, k);
        i = k;
        continue;
      }
      if (c === '*' || c === '_' || c === '~') {
        let k = i;
        while (text[k] === c) k++;
        const runLen = k - i;
        const next = text[k];
        const prev = text[i - 1];
        const canOpen = next && !/\s/.test(next) && !(c === '_' && prev && /[A-Za-z0-9]/.test(prev));
        if (canOpen) {
          let handled = false;
          const tries = c === '~' ? (runLen >= 2 ? ['~~'] : []) : runLen >= 3 ? [c.repeat(3), c.repeat(2), c] : runLen === 2 ? [c.repeat(2), c] : [c];
          for (const delim of tries) {
            const close = findClosing(ctx, delim, i + delim.length);
            if (close === -1) continue;
            const inner = text.slice(i + delim.length, close);
            if (!inner.trim()) continue;
            flush();
            let el;
            if (c === '~') {
              el = h('del');
              inline(inner, el, noLinks, level + 1);
            } else if (delim.length === 3) {
              el = h('strong');
              const em = h('em');
              inline(inner, em, noLinks, level + 1);
              el.append(em);
            } else {
              el = h(delim.length === 2 ? 'strong' : 'em');
              inline(inner, el, noLinks, level + 1);
            }
            parent.append(el);
            i = close + delim.length;
            handled = true;
            break;
          }
          if (handled) continue;
        }
        buf += text.slice(i, k);
        i = k;
        continue;
      }
      if (!noLinks && (c === '[' || (c === '!' && text[i + 1] === '['))) {
        const image = c === '!';
        const open = image ? i + 1 : i;
        if (!ctx.brackets) ctx.brackets = matchPairs(text, '[', ']', false);
        const closeBracket = ctx.brackets.has(open) ? ctx.brackets.get(open) : -1;
        if (closeBracket !== -1 && text[closeBracket + 1] === '(') {
          if (!ctx.parens) ctx.parens = matchPairs(text, '(', ')', true);
          const closeParen = ctx.parens.has(closeBracket + 1) ? ctx.parens.get(closeBracket + 1) : -1;
          if (closeParen !== -1) {
            const label = text.slice(open + 1, closeBracket);
            let target = text.slice(closeBracket + 2, closeParen).trim();
            if (target.startsWith('<') && target.includes('>')) target = target.slice(1, target.indexOf('>'));
            else target = target.split(/\s+/)[0] || '';
            flush();
            if (SAFE_URL.test(target) && target.length <= MAX_URL) {
              const a = link(target);
              if (image) a.textContent = label || target;
              else inline(label, a, true, level + 1);
              parent.append(a);
              // A label that looks like a URL but points elsewhere: show the real destination.
              const shown = labelHost(label);
              const real = hostOf(target);
              if (shown && shown !== real) parent.append(h('span', { class: 'link-host', text: ' (' + (real || target.slice(0, 60)) + ')' }));
            } else if (image) {
              parent.append(label);
            } else {
              inline(label, parent, true, level + 1);
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
        RE_AUTOLINK.lastIndex = i;
        const auto = noLinks ? null : RE_AUTOLINK.exec(text);
        if (auto) {
          flush();
          parent.append(link(auto[1], auto[1]));
          i += auto[0].length;
          continue;
        }
        RE_BR.lastIndex = i;
        const br = RE_BR.exec(text);
        if (br) {
          flush();
          parent.append(h('br'));
          i += br[0].length;
          continue;
        }
      }
      if (!noLinks && (c === 'h' || c === 'H') && (!text[i - 1] || /[\s(]/.test(text[i - 1]))) {
        RE_HTTP.lastIndex = i;
        if (RE_HTTP.test(text)) {
          RE_URL.lastIndex = i;
          const m = RE_URL.exec(text);
          const url = m ? trimUrl(m[0]) : '';
          if (url.length > 8) {
            flush();
            parent.append(link(url, url));
            i += url.length;
            continue;
          }
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
    if (NO_HIGHLIGHT.has(lang) || code.length > MAX_HIGHLIGHT) {
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
    if (block.closed || final) highlight(block.text, block.lang, codeEl);
    else codeEl.textContent = block.text;
    const pre = h('pre', { tabindex: '0', 'aria-label': (block.lang || 'Code') + LR.t(' code block') }, codeEl);
    const head = h('div', { class: 'code-head' }, [
      h('span', { class: 'code-lang', text: block.lang || 'code' }),
      LR.copyButton({ getText: () => codeEl.textContent, cls: 'code-copy', label: LR.t('Copy'), ariaLabel: LR.t('Copy code') }),
    ]);
    const el = h('div', { class: 'code-block' }, [head, pre]);
    el._code = codeEl;
    return el;
  }

  /** Deeply nested content is shown as plain text instead of recursing further. */
  const plain = (text) => h('p', { class: 'md-plain', text });

  function renderBlock(block, final, tight, depth) {
    const level = depth || 0;
    switch (block.type) {
      case 'code':
        return codeBlock(block, final);
      case 'heading': {
        const el = h('h' + Math.min(6, block.level + 1), { class: 'md-h md-h' + block.level }); // the page owns h1
        inline(block.text, el, false, level);
        return el;
      }
      case 'hr':
        return h('hr');
      case 'quote': {
        if (level >= MAX_DEPTH) return plain(block.text);
        const el = h('blockquote');
        for (const b of parseBlocks(block.text)) el.append(renderBlock(b, final, false, level + 1));
        return el;
      }
      case 'list': {
        if (level >= MAX_DEPTH) return plain(block.raw);
        const el = h(block.ordered ? 'ol' : 'ul', { start: block.ordered && block.start !== 1 ? String(block.start) : null });
        for (const src of block.items) {
          const li = h('li');
          parseBlocks(src).forEach((b, idx) => {
            if (!block.loose && b.type === 'para' && idx === 0) inline(b.text, li, false, level + 1);
            else li.append(renderBlock(b, final, !block.loose, level + 1));
          });
          el.append(li);
        }
        return el;
      }
      case 'table': {
        const thead = h('thead', null, h('tr', null, block.header.map((cell, idx) => {
          const th = h('th', { scope: 'col', class: block.align[idx] ? 'align-' + block.align[idx] : null });
          inline(cell, th, false, level);
          return th;
        })));
        const tbody = h('tbody', null, block.rows.map((row) => h('tr', null, block.header.map((_, idx) => {
          const td = h('td', { class: block.align[idx] ? 'align-' + block.align[idx] : null });
          inline(row[idx] || '', td, false, level);
          return td;
        }))));
        return h('div', { class: 'table-wrap', tabindex: '0', role: 'group', 'aria-label': LR.t('Table') }, h('table', null, [thead, tbody]));
      }
      default: {
        const el = h('p', { class: tight ? 'md-tight' : null });
        inline(block.text, el, false, level);
        return el;
      }
    }
  }

  function normalize(text) {
    return String(text || '').replace(/\r\n?/g, '\n').replace(/\t/g, '    ');
  }

  const CARET_HOSTS = new Set(['P', 'LI', 'UL', 'OL', 'BLOCKQUOTE', 'H2', 'H3', 'H4', 'H5', 'H6', 'CODE', 'PRE', 'DIV', 'STRONG', 'EM', 'DEL']);
  function caretHost(node) {
    let host = node;
    for (let guard = 0; guard < 64; guard++) {
      if (host.classList && host.classList.contains('code-block')) return host._code;
      const last = host.lastElementChild;
      if (!last || !CARET_HOSTS.has(last.tagName) || (last.tagName === 'DIV' && !last.classList.contains('code-block'))) break;
      if (host.lastChild !== last && host.lastChild.nodeType === 3 && host.lastChild.textContent.trim()) break;
      host = last;
    }
    return host;
  }

  /**
   * Incremental renderer bound to `root`. render(text, streaming) updates only
   * the blocks that changed. While text only grows (streaming), parsing
   * restarts at the last block instead of the beginning.
   */
  function createRenderer(root) {
    let prev = [];
    let prevText = null;
    let caret = null;
    return {
      render(text, streaming) {
        const norm = normalize(text);
        const lines = norm.split('\n');
        let blocks;
        if (prevText !== null && prev.length > 1 && norm.startsWith(prevText)) {
          const keep = prev.length - 1;
          blocks = prev.slice(0, keep).map((p) => p.block).concat(parseLines(lines, prev[keep].block.line));
        } else {
          blocks = parseLines(lines, 0);
        }
        prevText = norm;
        const final = !streaming;
        if (caret) { caret.remove(); caret = null; }
        let i = 0;
        while (i < blocks.length && i < prev.length && prev[i].block.raw === blocks[i].raw && (prev[i].final === final || blocks[i].type !== 'code' || blocks[i].closed)) i++;
        // Fast path: a growing, still-open code block only appends text.
        if (i < blocks.length && i === prev.length - 1 && i === blocks.length - 1 && streaming) {
          const p = prev[i];
          const b = blocks[i];
          if (p.block.type === 'code' && b.type === 'code' && !p.block.closed && !b.closed && p.block.lang === b.lang && b.text.startsWith(p.block.text)) {
            p.node._code.append(b.text.slice(p.block.text.length));
            p.block = b;
            i = blocks.length;
          }
        }
        for (let j = prev.length - 1; j >= i; j--) prev[j].node.remove();
        prev = prev.slice(0, i);
        for (; i < blocks.length; i++) {
          const b = blocks[i];
          const node = renderBlock(b, final, false, 0);
          root.append(node);
          prev.push({ block: b, final, node });
        }
        if (streaming) {
          caret = h('span', { class: 'caret', 'aria-hidden': 'true' });
          const last = prev.length ? prev[prev.length - 1].node : root;
          if (last !== root && (last.classList.contains('table-wrap') || last.tagName === 'HR')) root.append(caret);
          else caretHost(last).append(caret);
        }
      },
      reset() {
        prev = [];
        prevText = null;
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
    /** One-shot render of untrusted Markdown into el. */
    render(el, text) {
      el.replaceChildren();
      createRenderer(el).render(text, false);
      return el;
    },
  };
})();
