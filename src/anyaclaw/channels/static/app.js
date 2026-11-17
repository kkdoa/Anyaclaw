'use strict';

/* Anyaclaw web console — plain ES2020, no dependencies, no build step. */

const HINT = 'Enter 发送 · Shift+Enter 换行 · 支持上传附件';

const state = {
  sessionKey: '',
  messages: [],
  sending: false,
  controller: null,
  sessions: [],
  attachments: [],
  approvals: new Map(),
  streamMsg: null,
  dirty: new Set(),
  frame: false,
  drawerToken: 0,
  sessionTimer: 0,
};

const byPart = new WeakMap();
const $ = (id) => document.getElementById(id);

const dom = {
  app: $('app'),
  scrim: $('scrim'),
  sidebarClose: $('sidebarClose'),
  sessionList: $('sessionList'),
  sessionCount: $('sessionCount'),
  newChatBtn: $('newChatBtn'),
  menuBtn: $('menuBtn'),
  healthDot: $('healthDot'),
  healthText: $('healthText'),
  versionText: $('versionText'),
  modelName: $('modelName'),
  sessionTitle: $('sessionTitle'),
  sessionMeta: $('sessionMeta'),
  cancelBtn: $('cancelBtn'),
  toolsBtn: $('toolsBtn'),
  metricsBtn: $('metricsBtn'),
  messages: $('messages'),
  input: $('input'),
  attachBtn: $('attachBtn'),
  fileInput: $('fileInput'),
  sendBtn: $('sendBtn'),
  stopBtn: $('stopBtn'),
  attachmentList: $('attachmentList'),
  composerHint: $('composerHint'),
  drawer: $('drawer'),
  drawerTitle: $('drawerTitle'),
  drawerBody: $('drawerBody'),
  drawerClose: $('drawerClose'),
};


function escapeHtml(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

function safeUrl(raw) {
  const url = String(raw == null ? '' : raw).trim();
  if (/^(?:https?|mailto):/i.test(url)) return url;
  if (/^[/#]/.test(url) || /^\.{1,2}\//.test(url)) return url;
  return '';
}

function inline(text) {
  const codes = [];
  let out = String(text).replace(/`([^`\n]+)`/g, (_m, code) => {
    codes.push(code);
    return `\u0000${codes.length - 1}\u0000`;
  });
  out = out.replace(/\[([^\]\n]+)\]\(([^()\s]+)\)/g, (_m, label, url) => {
    const href = safeUrl(url);
    return href ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>` : label;
  });
  out = out.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  return out.replace(/\u0000(\d+)\u0000/g, (_m, index) => `<code>${codes[Number(index)] || ''}</code>`);
}

function renderMarkdown(raw) {
  const lines = escapeHtml(raw).replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let para = [];
  const flush = () => {
    if (para.length) out.push(`<p>${para.map(inline).join('<br>')}</p>`);
    para = [];
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      flush();
      const lang = line.trim().slice(3).trim();
      const buffer = [];
      for (i += 1; i < lines.length && !/^\s*```/.test(lines[i]); i += 1) buffer.push(lines[i]);
      const attr = lang ? ` data-lang="${escapeHtml(lang)}"` : '';
      out.push(`<pre class="code-block"><code${attr}>${buffer.join('\n')}</code></pre>`);
    } else if (!line.trim()) {
      flush();
    } else if (/^\s{0,3}#{1,3}\s+/.test(line)) {
      flush();
      const level = line.trim().split(/\s/)[0].length;
      out.push(`<h${level}>${inline(line.trim().slice(level).trim())}</h${level}>`);
    } else if (/^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flush();
      out.push('<hr>');
    } else if (/^\s{0,3}[-*+]\s+/.test(line) || /^\s{0,3}\d+[.)]\s+/.test(line)) {
      flush();
      const ordered = /^\s{0,3}\d+[.)]\s+/.test(line);
      const pattern = ordered ? /^\s{0,3}\d+[.)]\s+/ : /^\s{0,3}[-*+]\s+/;
      const items = [];
      while (i < lines.length && pattern.test(lines[i])) {
        items.push(lines[i].replace(pattern, ''));
        i += 1;
      }
      i -= 1;
      const tag = ordered ? 'ol' : 'ul';
      out.push(`<${tag}>${items.map((item) => `<li>${inline(item)}</li>`).join('')}</${tag}>`);
    } else {
      para.push(line);
    }
  }
  flush();
  return out.join('') || '<p></p>';
}

function toText(value) {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map((item) => (item && typeof item === 'object' ? item.text || '' : String(item))).join('\n');
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function basename(path) {
  const parts = String(path == null ? '' : path).split(/[\\/]/);
  return parts[parts.length - 1] || '';
}

function relTime(seconds) {
  const ts = Number(seconds);
  if (!Number.isFinite(ts) || ts <= 0) return '';
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

function fmtDur(seconds) {
  const s = Math.round(Number(seconds) || 0);
  return s < 60 ? `${s} 秒` : s < 3600 ? `${Math.floor(s / 60)} 分 ${s % 60} 秒` : `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`;
}

function uuid() {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  } catch {
    /* insecure context */
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (ch) => {
    const r = (Math.random() * 16) | 0;
    return (ch === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

function newKey() {
  return `web:${uuid()}`;
}

function persist() {
  try {
    localStorage.setItem('currentSession', state.sessionKey);
  } catch {
    /* storage unavailable */
  }
}

function savedKey() {
  try {
    return localStorage.getItem('currentSession') || '';
  } catch {
    return '';
  }
}

function pretty(value) {
  if (value === undefined) return '';
  try {
    const text = JSON.stringify(value, null, 2);
    return text === undefined ? String(value) : text;
  } catch {
    return String(value);
  }
}

function argSummary(args) {
  if (!args || typeof args !== 'object' || Array.isArray(args)) return '';
  return Object.keys(args).slice(0, 3).map((key) => {
    const raw = typeof args[key] === 'string' ? args[key] : pretty(args[key]).replace(/\s+/g, ' ');
    return `${key}=${raw.length > 32 ? `${raw.slice(0, 32)}…` : raw}`;
  }).join(' · ');
}

function friendly(err) {
  if (!err) return '未知错误';
  if (err.name === 'TypeError') return '无法连接后端服务，请确认 anyaclaw web 服务正在运行。';
  return err.message || String(err);
}


async function request(path, options = {}) {
  const init = { ...options, headers: { ...(options.headers || {}) } };
  if (init.body != null && !(init.body instanceof FormData)) init.headers['Content-Type'] = 'application/json';
  const response = await fetch(path, init);
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!response.ok) {
    const detail = errorDetail(data);
    const error = new Error(`HTTP ${response.status} ${response.statusText}${detail ? ` · ${detail}` : ''}`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function errorDetail(data) {
  if (!data) return '';
  if (typeof data === 'string') return data.slice(0, 400);
  const value = data.detail || data.error || data.message;
  if (!value) return '';
  if (typeof value === 'string') return value.slice(0, 400);
  try {
    return JSON.stringify(value).slice(0, 400);
  } catch {
    return String(value).slice(0, 400);
  }
}

async function errorBody(response) {
  try {
    const text = await response.text();
    if (!text) return '';
    try {
      return errorDetail(JSON.parse(text));
    } catch {
      return text.slice(0, 400);
    }
  } catch {
    return '';
  }
}


function h(tag, cls, text, kids) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (Array.isArray(text)) {
    kids = text;
    text = null;
  }
  if (text != null) node.textContent = text;
  for (const kid of kids || []) if (kid) node.append(kid);
  return node;
}

function errorBar(message) {
  return h('div', 'error-bar', [h('span', 'badge', '错误'), h('span', null, message)]);
}

function showError(message) {
  const stick = nearBottom();
  dom.messages.append(errorBar(String(message || '未知错误')));
  if (stick) scrollToBottom(true);
}


function normalizeHistory(raw) {
  const out = [];
  const calls = new Map();
  for (const message of Array.isArray(raw) ? raw : []) {
    if (!message || typeof message !== 'object') continue;
    const role = String(message.role || '');
    if (role === 'tool') {
      const card = calls.get(String(message.tool_call_id || ''));
      if (card) {
        card.result = toText(message.content);
        card.phase = 'done';
      }
      continue;
    }
    if (role === 'user') {
      out.push({ role: 'user', parts: [{ type: 'text', text: toText(message.content) }] });
      continue;
    }
    if (role !== 'assistant') continue;

    const parts = [];
    if (message.reasoning_content) parts.push({ type: 'reasoning', text: String(message.reasoning_content) });
    const content = toText(message.content);
    if (content) parts.push({ type: 'text', text: content });
    for (const call of Array.isArray(message.tool_calls) ? message.tool_calls : []) {
      const fn = (call && call.function) || {};
      let args = fn.arguments;
      if (typeof args === 'string') {
        try {
          args = JSON.parse(args);
        } catch {
          args = {};
        }
      }
      const card = {
        type: 'tool',
        tool: String(fn.name || 'tool'),
        callId: String((call && call.id) || ''),
        arguments: args && typeof args === 'object' ? args : {},
        phase: 'done',
        result: '',
        isError: false,
        durationMs: 0,
      };
      parts.push(card);
      if (card.callId) calls.set(card.callId, card);
    }
    if (parts.length) out.push({ role: 'assistant', parts });
  }
  return out;
}

function toolState(part) {
  if (part.phase === 'running') return '运行中';
  if (part.isError) return '失败';
  return part.durationMs ? `${part.durationMs}ms` : '完成';
}

function pre(text) {
  const node = document.createElement('pre');
  node.textContent = String(text == null ? '' : text);
  return node;
}

function jsonPre(value) {
  return pre(pretty(value) || '（无参数）');
}

function toolSection(label, content, isError) {
  return h('div', `tool-section${isError ? ' error' : ''}`, [
    h('span', 'label', label),
    content,
  ]);
}

function toolNode(part) {
  const card = document.createElement('details');
  card.className = 'tool-card';
  card.dataset.state = part.isError ? 'error' : part.phase || 'done';
  card.append(h('summary', null, [
    h('span', 'tool-chevron', '▶'),
    h('code', 'tool-name', part.tool || 'tool'),
    h('span', 'tool-args', argSummary(part.arguments) || part.callId || ''),
    h('span', 'tool-state', toolState(part)),
  ]));

  const body = h('div', 'tool-body', [toolSection('参数', jsonPre(part.arguments))]);
  if (part.result != null && part.result !== '') {
    body.append(toolSection(part.isError ? '错误输出' : '结果预览', pre(part.result), part.isError));
  } else if (part.phase === 'running') {
    body.append(h('div', 'drawer-note', '执行中…'));
  }
  if (Array.isArray(part.files) && part.files.length) body.append(toolSection('生成文件', pre(part.files.join('\n'))));
  card.append(body);
  return card;
}

function reasoningNode(part) {
  return h('details', 'reasoning', [
    h('summary', null, [h('span', null, '推理过程'), h('span', 'reasoning-count', `${(part.text || '').length} 字`)]),
    h('div', 'reasoning-body', part.text || ''),
  ]);
}

function approvalNode(part) {
  const note = h('div', 'approval-note', part.note || '');
  const allow = h('button', 'btn btn-primary', '允许');
  const deny = h('button', 'btn', '拒绝');
  allow.type = 'button';
  deny.type = 'button';
  const settle = (ok) => {
    if (part.status !== 'pending') return;
    allow.disabled = true;
    deny.disabled = true;
    note.textContent = '提交中…';
    approve(part, ok, note, allow, deny);
  };
  allow.onclick = () => settle(true);
  deny.onclick = () => settle(false);
  if (part.status !== 'pending') allow.disabled = deny.disabled = true;

  const wrap = h('div', 'approval', [
    h('div', 'approval-head', [h('span', null, '⚠ 需要授权'), h('code', 'tool-name', part.tool || 'tool')]),
    h('div', 'approval-reason', part.reason || '该工具调用需要人工确认。'),
    jsonPre(part.arguments),
    h('div', 'approval-actions', [allow, deny, note]),
  ]);
  wrap.dataset.state = part.status || 'pending';
  return wrap;
}

function buildPart(part) {
  if (part.type === 'text') {
    const node = h('div', 'md');
    node.innerHTML = renderMarkdown(part.text || '');
    return node;
  }
  if (part.type === 'reasoning') return reasoningNode(part);
  if (part.type === 'tool') return toolNode(part);
  if (part.type === 'approval') return approvalNode(part);
  return h('div', `note-line${part.kind ? ` ${part.kind}` : ''}`, part.text || '');
}

function nodeForPart(part) {
  const node = buildPart(part);
  byPart.set(part, node);
  return node;
}

function updatePart(part) {
  const node = byPart.get(part);
  if (!node || !node.isConnected) {
    refreshPart(part);
    return;
  }
  if (part.type === 'text') {
    node.innerHTML = renderMarkdown(part.text || '');
    return;
  }
  if (part.type === 'reasoning') {
    const body = node.querySelector('.reasoning-body');
    const count = node.querySelector('.reasoning-count');
    if (body) body.textContent = part.text || '';
    if (count) count.textContent = `${(part.text || '').length} 字`;
    return;
  }
  refreshPart(part);
}

function refreshPart(part) {
  const old = byPart.get(part);
  const node = nodeForPart(part);
  if (old && old.parentNode) {
    old.parentNode.replaceChild(node, old);
    return node;
  }
  if (state.streamMsg && state.streamMsg.bodyEl) {
    const stick = nearBottom();
    state.streamMsg.bodyEl.append(node);
    if (stick) scrollToBottom(true);
  }
  return node;
}

function schedulePart(part) {
  state.dirty.add(part);
  if (state.frame) return;
  state.frame = true;
  const run = () => {
    state.frame = false;
    const items = Array.from(state.dirty);
    state.dirty.clear();
    const stick = nearBottom();
    for (const item of items) updatePart(item);
    if (stick) scrollToBottom(true);
  };
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(run);
  else setTimeout(run, 16);
}

function addPart(msg, part) {
  msg.parts.push(part);
  const node = nodeForPart(part);
  const stick = nearBottom();
  if (msg.bodyEl) msg.bodyEl.append(node);
  if (stick) scrollToBottom(true);
  return part;
}

function addNote(msg, text, kind) {
  return addPart(msg, { type: 'note', text, kind: kind || '' });
}

function setStatus(msg, text) {
  if (!msg) return;
  if (msg.statusEl) msg.statusEl.textContent = text || '';
}

function appendDelta(msg, type, chunk) {
  if (!chunk) return;
  const last = msg.parts[msg.parts.length - 1];
  const part = last && last.type === type ? last : addPart(msg, { type, text: '' });
  part.text += chunk;
  schedulePart(part);
}


function messageNode(msg) {
  const node = h('article', `msg msg-${msg.role}${msg.streaming ? ' streaming' : ''}`);
  msg.node = node;
  const head = h('div', 'msg-head', [h('span', 'who', msg.role === 'user' ? '你' : '助手')]);
  if (msg.role === 'assistant') {
    msg.statusEl = h('span', 'msg-status');
    head.append(msg.statusEl);
  }
  msg.bodyEl = h('div', 'msg-body');
  node.append(head, msg.bodyEl);
  if (msg.role === 'user') {
    msg.bodyEl.append(h('div', 'bubble', (msg.parts[0] && msg.parts[0].text) || ''));
  } else {
    for (const part of msg.parts) msg.bodyEl.append(nodeForPart(part));
  }
  if (msg.usage || msg.durationMs || msg.files) renderFooter(msg);
  return node;
}

function renderFooter(msg) {
  if (!msg.node) return;
  if (msg.footEl) msg.footEl.remove();
  if (msg.filesEl) msg.filesEl.remove();

  const bits = [];
  const usage = msg.usage;
  if (usage && (usage.total_tokens || usage.prompt_tokens || usage.completion_tokens)) {
    bits.push(`tokens: ${Number(usage.total_tokens) || 0}`);
  }
  if (msg.durationMs) bits.push(`${(msg.durationMs / 1000).toFixed(2)}s`);
  if (bits.length) {
    msg.footEl = h('div', 'msg-foot', bits.map((text) => h('span', null, text)));
    if (usage) msg.footEl.title = `prompt ${usage.prompt_tokens || 0} · completion ${usage.completion_tokens || 0}`;
    if (msg.failed) msg.footEl.append(h('span', 'fail', '失败'));
    msg.node.append(msg.footEl);
  }

  if (Array.isArray(msg.files) && msg.files.length) {
    msg.filesEl = h('div', 'files', msg.files.map((path) => {
      const name = basename(path);
      const link = h('a', 'file-link', [h('span', null, name)]);
      link.href = `/api/files/${encodeURIComponent(name)}`;
      link.download = name;
      link.title = String(path);
      return link;
    }));
    msg.node.append(msg.filesEl);
  }
}

function appendMessage(msg) {
  const empty = dom.messages.querySelector('.empty');
  if (empty) empty.remove();
  state.messages.push(msg);
  const stick = nearBottom();
  dom.messages.append(messageNode(msg));
  if (stick) scrollToBottom(true);
}

function renderMessages() {
  dom.messages.textContent = '';
  state.streamMsg = null;
  state.dirty.clear();
  if (!state.messages.length) {
    dom.messages.append(emptyState());
    return;
  }
  for (const msg of state.messages) dom.messages.append(messageNode(msg));
  scrollToBottom(true);
}

function emptyState() {
  const prompts = [
    '帮我梳理这个仓库的目录结构，并指出核心模块。',
    '读取 README.md，列出还没完成的待办事项。',
    '生成一份本周工作周报（docx），包含进展与风险。',
  ];
  const list = h('div', 'examples');
  for (const prompt of prompts) {
    const button = h('button', 'example', prompt);
    button.type = 'button';
    button.onclick = () => {
      dom.input.value = prompt;
      autosize();
      dom.input.focus();
    };
    list.append(button);
  }
  return h('div', 'empty', [h('h2', null, '开始新的对话'), list]);
}

function nearBottom() {
  return dom.messages.scrollHeight - dom.messages.scrollTop - dom.messages.clientHeight < 90;
}

function scrollToBottom(force) {
  if (force || nearBottom()) dom.messages.scrollTop = dom.messages.scrollHeight;
}


function setSending(flag) {
  state.sending = flag;
  dom.sendBtn.disabled = flag;
  dom.stopBtn.hidden = !flag;
  dom.cancelBtn.hidden = !flag;
  dom.attachBtn.disabled = flag;
}

async function sendMessage() {
  if (state.sending) return;
  const text = dom.input.value.trim();
  const files = state.attachments.slice();
  if (!text && !files.length) {
    dom.input.focus();
    return;
  }
  if (!state.sessionKey.startsWith('web:')) {
    showError('该会话来自其他渠道，暂不支持在 Web 控制台回复。');
    return;
  }

  const names = files.map((file) => file.name);
  const wire = (text || (names.length ? '请查看附件。' : '')) + (names.length ? `\n\n[附件] ${names.join(' ')}` : '');

  dom.input.value = '';
  autosize();
  state.attachments = [];
  renderAttachments();

  appendMessage({ role: 'user', parts: [{ type: 'text', text: text || '（仅附件）' }] });
  updateTitle();

  const assistant = { role: 'assistant', parts: [], streaming: true, terminal: false };
  appendMessage(assistant);
  state.streamMsg = assistant;
  state.controller = new AbortController();
  setSending(true);
  setStatus(assistant, '处理中');

  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({ session_key: state.sessionKey, message: wire }),
      signal: state.controller.signal,
    });
    if (!response.ok) {
      const detail = await errorBody(response);
      const error = new Error(`HTTP ${response.status} ${response.statusText}${detail ? ` · ${detail}` : ''}`);
      error.status = response.status;
      throw error;
    }
    if (!response.body) throw new Error('浏览器不支持流式响应（缺少 ReadableStream）。');
    await consumeSSE(response.body, assistant);
    if (!assistant.terminal) showError('连接已结束但未收到 turn_end，本轮可能不完整。');
  } catch (error) {
    if (error && error.name === 'AbortError') {
      if (!assistant.cancelled && !assistant.terminal) addNote(assistant, '已取消本轮生成。', 'warn');
    } else {
      assistant.failed = true;
      assistant.terminal = true;
      showError(friendly(error));
      addNote(assistant, '本轮生成失败。', 'bad');
    }
  } finally {
    finishTurn(assistant);
  }
}

async function consumeSSE(stream, msg) {
  const reader = stream.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  const dispatch = (block) => {
    const data = [];
    for (const line of block.split('\n')) if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
    if (!data.length) return;
    const payload = data.join('\n').trim();
    if (!payload || payload === '[DONE]') return;
    let event;
    try {
      event = JSON.parse(payload);
    } catch {
      showError(`无法解析服务端事件：${payload.slice(0, 200)}`);
      return;
    }
    handleEvent(event, msg);
  };

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer = `${buffer}${decoder.decode(value, { stream: true })}`.replace(/\r\n/g, '\n');
      let index = buffer.indexOf('\n\n');
      while (index !== -1) {
        dispatch(buffer.slice(0, index));
        buffer = buffer.slice(index + 2);
        index = buffer.indexOf('\n\n');
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) dispatch(buffer);
  } finally {
    try {
      reader.releaseLock();
    } catch {
      /* noop */
    }
  }
}

function handleEvent(event, msg) {
  if (!event || typeof event !== 'object') return;
  switch (String(event.type || '')) {
    case 'turn_start':
      setStatus(msg, '处理中');
      return;
    case 'thinking':
      setStatus(msg, `思考中 · 第 ${Number(event.iteration) || 1} 轮`);
      return;
    case 'reasoning_delta':
      appendDelta(msg, 'reasoning', String(event.content == null ? '' : event.content));
      return;
    case 'reasoning': {
      const content = String(event.content == null ? '' : event.content);
      if (!content) return;
      let part = null;
      for (let i = msg.parts.length - 1; i >= 0; i -= 1) {
        if (msg.parts[i].type === 'reasoning') {
          part = msg.parts[i];
          break;
        }
      }
      if (!part) addPart(msg, { type: 'reasoning', text: content });
      else if (content.length >= part.text.length) {
        part.text = content;
        schedulePart(part);
      }
      return;
    }
    case 'content_delta':
      appendDelta(msg, 'text', String(event.content == null ? '' : event.content));
      setStatus(msg, '');
      return;
    case 'tool_call': {
      const part = addPart(msg, {
        type: 'tool',
        tool: String(event.tool || 'tool'),
        callId: String(event.call_id || ''),
        arguments: event.arguments && typeof event.arguments === 'object' ? event.arguments : {},
        phase: 'running',
        result: null,
        isError: false,
        durationMs: 0,
      });
      setStatus(msg, `工具 · ${part.tool}`);
      return;
    }
    case 'tool_result': {
      const callId = String(event.call_id || '');
      let part = null;
      for (let i = msg.parts.length - 1; i >= 0; i -= 1) {
        if (msg.parts[i].type === 'tool' && msg.parts[i].callId === callId) {
          part = msg.parts[i];
          break;
        }
      }
      if (!part) return;
      part.result = String(event.content == null ? '' : event.content);
      part.isError = Boolean(event.is_error);
      part.files = Array.isArray(event.files) ? event.files : [];
      part.durationMs = Number(event.duration_ms) || 0;
      part.phase = 'done';
      refreshPart(part);
      setStatus(msg, '');
      return;
    }
    case 'approval_request': {
      const id = String(event.id || '');
      const part = addPart(msg, {
        type: 'approval',
        id,
        tool: String(event.tool || 'tool'),
        arguments: event.arguments && typeof event.arguments === 'object' ? event.arguments : {},
        reason: String(event.reason || ''),
        status: 'pending',
        note: '',
      });
      state.approvals.set(id, part);
      setStatus(msg, '等待授权');
      return;
    }
    case 'approval_result': {
      const part = state.approvals.get(String(event.id || ''));
      if (!part) return;
      part.status = event.approved ? 'allowed' : 'denied';
      part.note = event.approved ? '已允许。' : '已拒绝，该工具调用被跳过。';
      refreshPart(part);
      setStatus(msg, '');
      return;
    }
    case 'turn_end': {
      msg.terminal = true;
      const content = toText(event.content);
      let textPart = null;
      for (let i = msg.parts.length - 1; i >= 0; i -= 1) {
        if (msg.parts[i].type === 'text') {
          textPart = msg.parts[i];
          break;
        }
      }
      if (content && textPart && content !== textPart.text) {
        textPart.text = content;
        schedulePart(textPart);
      } else if (content && !textPart) {
        addPart(msg, { type: 'text', text: content });
      }
      msg.usage = event.usage && typeof event.usage === 'object' ? event.usage : null;
      msg.durationMs = Number(event.duration_ms) || 0;
      msg.files = Array.isArray(event.files) ? event.files : [];
      msg.streaming = false;
      if (msg.node) msg.node.classList.remove('streaming');
      renderFooter(msg);
      setStatus(msg, '');
      scheduleSessions(120);
      return;
    }
    case 'error': {
      msg.terminal = true;
      msg.failed = true;
      msg.streaming = false;
      if (msg.node) msg.node.classList.remove('streaming');
      const message = String(event.message || '未知错误');
      addNote(msg, `本轮出错：${message}`, 'bad');
      showError(message);
      setStatus(msg, '');
      return;
    }
    case 'cancelled': {
      msg.terminal = true;
      msg.cancelled = true;
      msg.streaming = false;
      if (msg.node) msg.node.classList.remove('streaming');
      const reason = String(event.reason || '');
      addNote(msg, reason ? `已取消：${reason}` : '已取消。', 'warn');
      setStatus(msg, '');
      return;
    }
    default:
      return;
  }
}

function finishTurn(msg) {
  if (!msg) return;
  msg.streaming = false;
  if (msg.node) msg.node.classList.remove('streaming');
  setStatus(msg, '');
  if (!msg.parts.length) addNote(msg, msg.failed ? '本轮没有产生任何输出。' : '（无输出）');
  if (msg.usage || msg.durationMs) renderFooter(msg);
  setSending(false);
  state.controller = null;
  state.streamMsg = null;
  scheduleSessions(80);
  dom.input.focus();
}


async function approve(part, approved, note, allow, deny) {
  try {
    const result = await request(`/api/approvals/${encodeURIComponent(part.id)}`, {
      method: 'POST',
      body: JSON.stringify({ approved: Boolean(approved) }),
    });
    if (result && result.ok === false) throw new Error('审批已失效（可能已超时）');
    part.status = approved ? 'allowed' : 'denied';
    part.note = approved ? '已允许，等待 Agent 继续…' : '已拒绝，等待 Agent 继续…';
    refreshPart(part);
  } catch (error) {
    note.textContent = `提交失败：${friendly(error)}`;
    allow.disabled = false;
    deny.disabled = false;
    showError(`审批提交失败：${friendly(error)}`);
  }
}

async function cancel() {
  if (!state.sending) return;
  const key = state.sessionKey;
  if (state.controller) state.controller.abort();
  try {
    await request(`/api/chat/${encodeURIComponent(key)}/cancel`, {
      method: 'POST',
      body: JSON.stringify({}),
    });
  } catch (error) {
    if (!error || error.status !== 404) showError(`取消请求失败：${friendly(error)}`);
  }
}


function scheduleSessions(delay) {
  if (state.sessionTimer) clearTimeout(state.sessionTimer);
  state.sessionTimer = setTimeout(() => {
    state.sessionTimer = 0;
    renderSessions();
  }, typeof delay === 'number' ? delay : 100);
}

async function renderSessions() {
  try {
    const data = await request('/api/sessions');
    state.sessions = Array.isArray(data && data.sessions) ? data.sessions : [];
  } catch (error) {
    state.sessions = [];
    showError(`会话列表加载失败：${friendly(error)}`);
  }

  dom.sessionCount.textContent = String(state.sessions.length);
  dom.sessionList.textContent = '';
  if (!state.sessions.length) {
    dom.sessionList.append(h('div', 'session-empty', '还没有历史会话，发送第一条消息后会自动保存。'));
    return;
  }

  for (const session of state.sessions) {
    const key = String(session.key || '');
    if (!key) continue;
    const main = h('button', 'session-main', [
      h('span', 'session-title', String(session.title || '').trim() || key),
      h('div', 'session-meta', [
        h('span', null, `${Number(session.messages) || 0} 条`),
        h('span', null, relTime(session.updated_at)),
      ]),
    ]);
    main.type = 'button';
    main.title = String(session.title || key);
    main.onclick = () => selectSession(key);

    const del = h('button', 'session-del', '×');
    del.type = 'button';
    del.title = '删除会话';
    del.onclick = (event) => {
      event.stopPropagation();
      deleteSession(key);
    };
    dom.sessionList.append(h('div', `session-item${key === state.sessionKey ? ' active' : ''}`, [main, del]));
  }
}

async function selectSession(key) {
  dom.app.classList.remove('sidebar-open');
  if (!key || key === state.sessionKey) return;
  if (state.sending) {
    showError('正在生成回复，请先点击「停止」再切换会话。');
    return;
  }
  state.sessionKey = key;
  persist();
  scheduleSessions(0);
  await loadMessages();
}

async function loadMessages() {
  const key = state.sessionKey;
  try {
    const data = await request(`/api/sessions/${encodeURIComponent(key)}/messages`);
    if (key !== state.sessionKey) return;
    state.messages = normalizeHistory(data && data.messages);
    renderMessages();
    updateTitle();
  } catch (error) {
    if (key !== state.sessionKey) return;
    state.messages = [];
    renderMessages();
    if (error && error.status === 404) return;
    showError(`加载会话消息失败：${friendly(error)}`);
  }
}

function newSession() {
  if (state.sending) {
    showError('正在生成回复，请先停止后再新建会话。');
    return;
  }
  state.sessionKey = newKey();
  persist();
  state.messages = [];
  state.attachments = [];
  renderAttachments();
  renderMessages();
  scheduleSessions(0);
  updateTitle();
  dom.input.focus();
  dom.app.classList.remove('sidebar-open');
}

async function deleteSession(key) {
  if (state.sending) {
    showError('正在生成回复，请先停止后再删除会话。');
    return;
  }
  if (!window.confirm('删除这个会话及其全部消息？此操作不可撤销。')) return;
  try {
    await request(`/api/sessions/${encodeURIComponent(key)}`, { method: 'DELETE' });
    if (key === state.sessionKey) newSession();
    else scheduleSessions(0);
  } catch (error) {
    showError(`删除会话失败：${friendly(error)}`);
  }
}

function updateTitle() {
  const info = state.sessions.find((session) => session.key === state.sessionKey);
  const firstUser = state.messages.find((message) => message.role === 'user');
  const fallback = firstUser && firstUser.parts[0] ? firstUser.parts[0].text : '';
  const raw = String((info && info.title) || fallback || '新对话').replace(/\s+/g, ' ').trim() || '新对话';
  dom.sessionTitle.textContent = raw.length > 64 ? `${raw.slice(0, 64)}…` : raw;
  dom.sessionTitle.title = raw;
  const count = info && Number.isFinite(Number(info.messages)) ? Number(info.messages) : state.messages.length;
  dom.sessionMeta.textContent = `${(info && info.channel) || 'web'} · ${count} 条消息`;
}


function renderAttachments() {
  dom.attachmentList.textContent = '';
  dom.attachmentList.hidden = state.attachments.length === 0;
  state.attachments.forEach((file, index) => {
    const chip = h('span', 'chip', [h('span', 'name', file.name)]);
    chip.firstChild.title = file.path || file.name;
    const remove = h('button', 'remove', '×');
    remove.type = 'button';
    remove.setAttribute('aria-label', `移除 ${file.name}`);
    remove.onclick = () => {
      state.attachments.splice(index, 1);
      renderAttachments();
    };
    chip.append(remove);
    dom.attachmentList.append(chip);
  });
}

async function uploadFiles(fileList) {
  const files = Array.from(fileList || []).filter(Boolean);
  if (!files.length) return;
  const form = new FormData();
  for (const file of files) form.append('file', file);

  dom.composerHint.textContent = '上传中…';
  try {
    const response = await fetch('/api/files', { method: 'POST', body: form });
    if (!response.ok) {
      const detail = await errorBody(response);
      throw new Error(`HTTP ${response.status} ${response.statusText}${detail ? ` · ${detail}` : ''}`);
    }
    const data = await response.json();
    const uploaded = data && Array.isArray(data.files) ? data.files : [];
    if (!uploaded.length) throw new Error('服务端没有返回文件信息。');
    for (const item of uploaded) {
      state.attachments.push({
        name: String(item.name || basename(item.path) || 'file'),
        path: String(item.path || ''),
        size: Number(item.size) || 0,
      });
    }
    renderAttachments();
  } catch (error) {
    showError(`附件上传失败：${friendly(error)}`);
  } finally {
    dom.composerHint.textContent = HINT;
    dom.fileInput.value = '';
  }
}


async function loadHealth() {
  try {
    const data = await request('/api/health');
    const ok = data && String(data.status || '').toLowerCase() === 'ok';
    dom.healthDot.className = `dot ${ok ? 'ok' : 'pending'}`;
    dom.healthText.textContent = ok ? '在线' : String((data && data.status) || '未知状态');
    dom.modelName.textContent = String((data && data.model) || '—');
    dom.modelName.title = String((data && data.model) || '');
    dom.versionText.textContent = data && data.version ? `v${data.version}` : '—';
  } catch {
    dom.healthDot.className = 'dot bad';
    dom.healthText.textContent = '离线';
    dom.modelName.textContent = '—';
    dom.versionText.textContent = '—';
  }
}


function openDrawer(title) {
  dom.drawerTitle.textContent = title;
  dom.drawer.classList.add('open');
  dom.drawer.setAttribute('aria-hidden', 'false');
}

function closeDrawer() {
  dom.drawer.classList.remove('open');
  dom.drawer.setAttribute('aria-hidden', 'true');
}

function block(title, content) {
  return h('div', 'drawer-section', [h('h3', null, title), content]);
}

function table(headers, rows, numeric = []) {
  return h('table', 'metric-table', [
    h('thead', null, [h('tr', null, headers.map((text) => h('th', null, text)))]),
    h('tbody', null, rows.map((row) => h('tr', null, row.map((cell, index) => h('td', numeric.includes(index) ? 'num' : null, String(cell)))))),
  ]);
}

async function loadMetrics() {
  openDrawer('指标');
  const token = ++state.drawerToken;
  dom.drawerBody.textContent = '';
  dom.drawerBody.append(h('div', 'loading', '加载指标中…'));
  try {
    const data = await request('/api/metrics');
    if (token !== state.drawerToken) return;
    dom.drawerBody.textContent = '';
    dom.drawerBody.append(h('div', 'drawer-note', `运行时长 ${fmtDur(data && data.uptime_seconds)}`));

    const counters = Object.entries((data && data.counters) || {});
    dom.drawerBody.append(block('计数器', counters.length
      ? table(['名称', '值'], counters, [1])
      : h('div', 'drawer-note', '暂无计数器。')));

    const models = Object.entries((data && data.models) || {});
    const rows = models.map(([model, usage]) => {
      const item = usage || {};
      return [model, item.calls || 0, item.prompt_tokens || 0, item.completion_tokens || 0, item.total_tokens || 0, item.cached_tokens || 0];
    });
    dom.drawerBody.append(block('模型 token 汇总', rows.length
      ? table(['模型', '调用', 'Prompt', 'Completion', '总计', '缓存'], rows, [1, 2, 3, 4, 5])
      : h('div', 'drawer-note', '还没有模型调用记录。')));
  } catch (error) {
    if (token !== state.drawerToken) return;
    dom.drawerBody.textContent = '';
    dom.drawerBody.append(errorBar(`指标加载失败：${friendly(error)}`));
  }
}

async function loadTools() {
  openDrawer('工具');
  const token = ++state.drawerToken;
  dom.drawerBody.textContent = '';
  dom.drawerBody.append(h('div', 'loading', '加载工具列表…'));
  try {
    const data = await request('/api/tools');
    if (token !== state.drawerToken) return;
    dom.drawerBody.textContent = '';
    const tools = Array.isArray(data && data.tools) ? data.tools : [];
    if (!tools.length) {
      dom.drawerBody.append(h('div', 'drawer-note', '当前没有注册任何工具。'));
      return;
    }
    const list = h('div', 'drawer-section', [h('h3', null, `已注册工具（${tools.length}）`)]);
    for (const tool of tools) {
      list.append(h('div', 'tool-row', [
        h('div', 'tool-row-head', [
          h('code', null, String(tool.name || '')),
          h('span', `badge ${tool.readonly ? 'ro' : 'rw'}`, tool.readonly ? '只读' : '写操作'),
        ]),
        h('p', null, String(tool.description || '')),
      ]));
    }
    dom.drawerBody.append(list);
  } catch (error) {
    if (token !== state.drawerToken) return;
    dom.drawerBody.textContent = '';
    dom.drawerBody.append(errorBar(`工具列表加载失败：${friendly(error)}`));
  }
}


function autosize() {
  dom.input.style.height = 'auto';
  dom.input.style.height = `${Math.min(dom.input.scrollHeight, 180)}px`;
}

function bindEvents() {
  dom.newChatBtn.onclick = newSession;
  dom.menuBtn.onclick = () => dom.app.classList.add('sidebar-open');
  dom.sidebarClose.onclick = () => dom.app.classList.remove('sidebar-open');
  dom.scrim.onclick = () => dom.app.classList.remove('sidebar-open');
  dom.sendBtn.onclick = sendMessage;
  dom.stopBtn.onclick = cancel;
  dom.cancelBtn.onclick = cancel;
  dom.attachBtn.onclick = () => dom.fileInput.click();
  dom.fileInput.onchange = () => uploadFiles(dom.fileInput.files);
  dom.input.oninput = autosize;
  dom.input.onkeydown = (event) => {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    sendMessage();
  };
  dom.toolsBtn.onclick = loadTools;
  dom.metricsBtn.onclick = loadMetrics;
  dom.drawerClose.onclick = closeDrawer;
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    closeDrawer();
    dom.app.classList.remove('sidebar-open');
  });
}

function init() {
  const stored = savedKey();
  state.sessionKey = stored && stored.startsWith('web:') ? stored : newKey();
  persist();

  bindEvents();
  renderMessages();
  updateTitle();
  loadHealth();
  renderSessions();
  loadMessages();
}

init();
