(function () {
  'use strict';

  const form = document.getElementById('template-form');
  if (!form) return;
  const source = document.getElementById('body-html');
  const visual = document.getElementById('visual-panel');
  const editor = document.getElementById('visual-editor');
  const status = document.getElementById('editor-status');
  const modeButtons = Array.from(form.querySelectorAll('[data-editor-mode]'));
  const allowed = new Set(['p', 'br', 'div', 'span', 'strong', 'b', 'em', 'i', 'u',
    'h1', 'h2', 'h3', 'blockquote', 'ul', 'ol', 'li', 'a', 'hr']);
  const discarded = new Set(['script', 'style', 'iframe', 'object', 'embed', 'svg',
    'math', 'form', 'input', 'button', 'img', 'video', 'audio', 'link', 'meta']);
  let mode = 'source';

  function safeLink(raw) {
    const value = raw.trim();
    if (!/^(https?:\/\/|mailto:)/i.test(value) || /[\x00-\x20\\]/.test(value)) return '';
    try {
      const url = new URL(value);
      if (!['http:', 'https:', 'mailto:'].includes(url.protocol)) return '';
      if (url.protocol !== 'mailto:' && (!url.hostname || url.username || url.password)) return '';
      return value;
    } catch (_error) {
      return '';
    }
  }

  function cleanHtml(raw) {
    const parsed = document.createElement('template');
    parsed.innerHTML = raw;
    const output = document.createElement('div');
    let changed = false;

    function copy(node, parent) {
      if (node.nodeType === Node.TEXT_NODE) {
        parent.appendChild(document.createTextNode(node.nodeValue));
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) {
        changed = true;
        return;
      }
      const tag = node.tagName.toLowerCase();
      if (discarded.has(tag)) {
        changed = true;
        return;
      }
      if (!allowed.has(tag)) {
        changed = true;
        Array.from(node.childNodes).forEach(child => copy(child, parent));
        return;
      }
      const element = document.createElement(tag);
      if (tag === 'a') {
        const href = safeLink(node.getAttribute('href') || '');
        if (href) element.setAttribute('href', href);
        else if (node.hasAttribute('href')) changed = true;
      }
      Array.from(node.attributes).forEach(attribute => {
        if (tag !== 'a' || attribute.name.toLowerCase() !== 'href') changed = true;
      });
      Array.from(node.childNodes).forEach(child => copy(child, element));
      parent.appendChild(element);
    }

    Array.from(parsed.content.childNodes).forEach(node => copy(node, output));
    return { html: output.innerHTML, changed: changed };
  }

  function setMode(next) {
    if (next === mode) return;
    if (next === 'visual') {
      const cleaned = cleanHtml(source.value);
      if (cleaned.changed && !window.confirm('可视化编辑只保留基础排版和安全链接。切换后再保存会覆盖不支持的 HTML 内容，确定继续吗？')) return;
      editor.innerHTML = cleaned.html;
      source.hidden = true;
      visual.hidden = false;
      mode = 'visual';
      status.textContent = '可直接编辑排版；保存时会同步到 HTML 正文。';
    } else {
      source.value = cleanHtml(editor.innerHTML).html;
      source.hidden = false;
      visual.hidden = true;
      mode = 'source';
    }
    modeButtons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.editorMode === mode)));
  }

  modeButtons.forEach(button => button.addEventListener('click', () => setMode(button.dataset.editorMode)));
  form.querySelectorAll('[data-editor-command], [data-editor-link]').forEach(button => {
    button.addEventListener('mousedown', event => event.preventDefault());
    button.addEventListener('click', () => {
      if (mode !== 'visual') return;
      const selection = window.getSelection();
      if (button.hasAttribute('data-editor-link')) {
        if (!selection || selection.isCollapsed || !editor.contains(selection.anchorNode)) {
          status.textContent = '先选中需要添加链接的文字。';
          return;
        }
        const range = selection.getRangeAt(0).cloneRange();
        const href = window.prompt('输入完整的 https://、http:// 或 mailto: 链接', 'https://');
        if (href === null) return;
        const safe = safeLink(href);
        if (!safe) {
          status.textContent = '链接需以 https://、http:// 或 mailto: 开头。';
          return;
        }
        editor.focus();
        selection.removeAllRanges();
        selection.addRange(range);
        document.execCommand('createLink', false, safe);
      } else {
        editor.focus();
        document.execCommand(button.dataset.editorCommand, false, button.dataset.editorValue || null);
      }
      status.textContent = '';
    });
  });

  editor.addEventListener('paste', event => {
    event.preventDefault();
    const text = event.clipboardData.getData('text/plain');
    if (!document.execCommand('insertText', false, text)) {
      const selection = window.getSelection();
      if (!selection || !selection.rangeCount) return;
      const range = selection.getRangeAt(0);
      range.deleteContents();
      range.insertNode(document.createTextNode(text));
      range.collapse(false);
      selection.removeAllRanges();
      selection.addRange(range);
    }
  });
  editor.addEventListener('drop', event => event.preventDefault());
  form.addEventListener('submit', () => {
    if (mode === 'visual') source.value = cleanHtml(editor.innerHTML).html;
  });

  if (form.dataset.editorDefault === 'visual') setMode('visual');
  else modeButtons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.editorMode === mode)));
}());
