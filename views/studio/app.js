/* HDS 角色工作室 — AstrBot Plugin View
 * 通过 window.AstrBotPluginView bridge 调用插件后端 studio/action。
 * 路由使用 hash，避免 iframe 内 history 刷新 404。
 */

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage || null;

const state = {
  data: null,
  route: { name: 'overview', id: '' },
  character: null,
  characterDirty: false,
  charTab: 'identity',
  book: null,
  bookDirty: false,
  promptsDraft: null,
  promptsDirty: false,
  search: '',
  charFilter: 'all',
  entryQuery: '',
  preview: null,
  previewQuery: '',
  saving: false,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const view = $('#view');
const modalRoot = $('#modal-root');
const drawerRoot = $('#drawer-root');
const toastStack = $('#toast-stack');

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[char]));
}

function fmtDate(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function initials(name) {
  const text = String(name || '?').trim();
  return esc(text.slice(0, 1).toUpperCase());
}

function parseTags(value) {
  return String(value || '')
    .split(/[,，\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function parseLines(value) {
  return String(value || '')
    .split('\n')
    .map((item) => item.trim())
    .filter(Boolean);
}

function toast(message, kind = 'success') {
  const node = document.createElement('div');
  node.className = `toast ${kind === 'success' ? '' : kind}`.trim();
  node.innerHTML = `<span class="toast-dot"></span><div>${esc(message)}</div>`;
  toastStack.appendChild(node);
  setTimeout(() => {
    node.classList.add('leaving');
    setTimeout(() => node.remove(), 200);
  }, 3200);
}

function setSaveState(kind, text) {
  const node = $('#save-state');
  if (!node) return;
  node.dataset.state = kind;
  node.textContent = text;
}

async function api(action, payload = {}) {
  if (!bridge || typeof bridge.apiPost !== 'function') {
    throw new Error('未检测到 AstrBot 插件页面桥接。请从 AstrBot WebUI 的插件详情页打开本视图。');
  }
  const result = await bridge.apiPost('studio/action', { action, payload });
  if (result && typeof result === 'object' && result.status === 'ok' && 'data' in result) {
    return result.data;
  }
  return result;
}

/* ------------------------------------------------------------------ */
/* 路由与外壳                                                          */
/* ------------------------------------------------------------------ */

function currentRoute() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [name, ...rest] = raw.split('/');
  return { name: name || 'overview', id: rest.join('/') || '' };
}

function navigate(name, id = '') {
  const target = `#/${name}${id ? `/${id}` : ''}`;
  if (location.hash === target) {
    render();
    return;
  }
  location.hash = target;
}

function updateChrome() {
  const route = state.route;
  $$('.nav-item').forEach((item) => {
    item.classList.toggle('active', item.dataset.nav === route.name
      || (route.name === 'characters' && item.dataset.nav === 'characters')
      || (route.name === 'lorebooks' && item.dataset.nav === 'lorebooks'));
  });

  const crumbs = [];
  const labels = {
    overview: '概览', characters: '角色身份', lorebooks: '世界书',
    prompts: '叙事提示词', binding: '故事绑定', import: '导入中心', about: '说明',
  };
  crumbs.push(labels[route.name] || '概览');
  if (route.id && state.character && route.name === 'characters') crumbs.push(state.character.name || '未命名');
  if (route.id && state.book && route.name === 'lorebooks') crumbs.push(state.book.name || '未命名');
  $('#crumbs').innerHTML = crumbs.map((item) => `<span class="crumb">${esc(item)}</span>`).join('');

  const data = state.data;
  if (!data) return;
  $('#nav-character-count').textContent = String(data.characters.length);
  $('#nav-lorebook-count').textContent = String(data.lorebooks.length);

  const active = data.characters.find((item) => item.id === data.stats.activeCharacterId);
  const chip = $('#sidebar-active');
  chip.classList.toggle('is-active', Boolean(active));
  chip.innerHTML = `<span class="dot"></span><span>${active ? `当前：${esc(active.name)}` : '未选择当前角色'}</span>`;
}

async function loadState() {
  const data = await api('state.get');
  state.data = data;
  if (!state.promptsDraft) state.promptsDraft = { ...(data.prompts || {}) };
  updateChrome();
  return data;
}

/* ------------------------------------------------------------------ */
/* 渲染入口                                                            */
/* ------------------------------------------------------------------ */

async function render() {
  state.route = currentRoute();
  updateChrome();
  const route = state.route;
  try {
    if (route.name === 'characters' && route.id) {
      await renderCharacterEditor(route.id);
    } else if (route.name === 'lorebooks' && route.id) {
      await renderLorebookEditor(route.id);
    } else if (route.name === 'characters') {
      renderCharacters();
    } else if (route.name === 'lorebooks') {
      renderLorebooks();
    } else if (route.name === 'prompts') {
      renderPrompts();
    } else if (route.name === 'binding') {
      renderBinding();
    } else if (route.name === 'import') {
      renderImport();
    } else if (route.name === 'about') {
      renderAbout();
    } else {
      renderOverview();
    }
  } catch (error) {
    view.innerHTML = `<div class="empty"><div class="empty-icon">!</div><h3>加载失败</h3><p>${esc(error.message || error)}</p><button class="btn" onclick="location.reload()">重试</button></div>`;
  }
  updateChrome();
}

/* ------------------------------------------------------------------ */
/* 概览                                                                */
/* ------------------------------------------------------------------ */

function statCard(label, value, hint, tone = '') {
  return `<div class="card stat ${tone}">
    <span class="label">${esc(label)}</span>
    <span class="value">${esc(value)}</span>
    <span class="hint">${esc(hint)}</span>
  </div>`;
}

function charCard(card, { compact = false } = {}) {
  const active = state.data.stats.activeCharacterId === card.id;
  const source = (card.source && card.source.type) || 'manual';
  const sourceLabel = source === 'type0' ? 'Type0' : source === 'sillytavern' ? '酒馆导入' : '手动';
  return `<article class="char-card ${active ? 'is-active' : ''}" data-action="open-character" data-id="${esc(card.id)}" tabindex="0">
    <div class="char-top">
      <div class="avatar">${card.avatar ? `<img src="${esc(card.avatar)}" alt="">` : initials(card.name)}</div>
      <div style="min-width:0">
        <div class="char-name">${esc(card.name || '未命名角色')}</div>
        <div class="char-sub">${active ? '当前角色 · ' : ''}${esc(sourceLabel)}${card.updatedAt ? ` · ${fmtDate(card.updatedAt)}` : ''}</div>
      </div>
      <div class="spacer"></div>
      ${active ? '<span class="badge success">使用中</span>' : ''}
    </div>
    <p class="char-desc">${esc(card.description || '尚未填写角色描述。')}</p>
    <div class="row wrap">
      ${(card.tags || []).slice(0, 4).map((tag) => `<span class="chip">${esc(tag)}</span>`).join('')}
      ${card.hasWorld ? '<span class="badge teal">含世界设定</span>' : ''}
    </div>
    <div class="char-footer">
      <span class="muted" style="font-size:11.5px">${compact ? '' : '点击编辑'}</span>
      <div class="char-actions">
        ${active ? '' : `<button class="icon-btn" title="设为当前" data-action="activate-character" data-id="${esc(card.id)}">✓</button>`}
        <button class="icon-btn" title="复制" data-action="duplicate-character" data-id="${esc(card.id)}">⧉</button>
        <button class="icon-btn" title="删除" data-action="delete-character" data-id="${esc(card.id)}">✕</button>
      </div>
    </div>
  </article>`;
}

function renderOverview() {
  const data = state.data;
  const active = data.characters.find((item) => item.id === data.stats.activeCharacterId);
  const recent = [...data.characters]
    .sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)))
    .slice(0, 3);
  const entryCount = data.stats.entries || 0;

  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1>角色工作室</h1>
        <p>集中管理角色身份、世界书与叙事提示词。API Key、模型连接与平台账号仍保留在插件配置页。</p>
      </div>
      <div class="page-actions">
        <button class="btn" data-action="goto-import">导入中心</button>
        <button class="btn btn-primary" data-action="new-character">＋ 新建角色</button>
      </div>
    </div>

    <section class="hero">
      <div>
        <span class="badge accent">当前叙事身份</span>
        <h2 style="margin-top:12px">${active ? esc(active.name) : '还没有选择角色'}</h2>
        <p>${active ? esc(active.description || '该角色尚未填写描述。') : '创建或导入一个角色身份，把它设为当前角色后，新故事会使用它的人设与世界设定。'}</p>
        <div class="hero-meta">
          ${active ? `<button class="btn btn-primary" data-action="open-character" data-id="${esc(active.id)}">编辑角色</button>
          <button class="btn" data-action="goto-binding">故事绑定</button>` : `<button class="btn btn-primary" data-action="new-character">创建第一个角色</button>`}
          <button class="btn btn-ghost" data-action="goto-import">从 Type0 导入</button>
        </div>
      </div>
      <div class="hero-panel">
        <span class="label">世界书</span>
        <strong class="name">${data.stats.lorebooks} 本 · ${entryCount} 条</strong>
        <span class="desc">常驻条目每回合注入，关键词条目只在相关上下文出现时激活；按插入顺序与字符预算裁剪。</span>
        <button class="btn btn-sm" data-action="goto-lorebooks">管理世界书</button>
      </div>
    </section>

    <div class="section-title"><h2>工作区</h2><span>内容层与连接层已分离</span></div>
    <div class="grid grid-3">
      ${statCard('角色身份', String(data.characters.length), '酒馆 V2 角色卡')}
      ${statCard('世界书条目', String(entryCount), `${data.lorebooks.length} 本世界书`, 'teal')}
      ${statCard('故事绑定', String(Object.keys(data.bindings || {}).length), '按故事覆盖角色与世界书', 'amber')}
    </div>

    <div class="section-title"><h2>最近更新</h2><span>${recent.length} 个角色</span></div>
    ${recent.length ? `<div class="char-grid">${recent.map((card) => charCard(card, { compact: true })).join('')}</div>`
      : `<div class="empty"><div class="empty-icon">◐</div><h3>还没有角色</h3><p>从 Type0 导入预设，或手动填写一张角色卡。支持酒馆 V2 JSON / PNG 角色卡。</p><button class="btn btn-primary" data-action="new-character">新建角色</button></div>`}

    <div class="section-title"><h2>三步开始</h2><span>内容改动无需重载插件</span></div>
    <div class="grid grid-3">
      <div class="card" style="padding:18px">
        <span class="badge">01</span>
        <h3 style="margin:10px 0 6px;font-size:14px">建立角色身份</h3>
        <p class="muted" style="margin:0;font-size:12.5px">手动填写，或从 Type0 分享码自动填充；导入后设为当前角色。</p>
      </div>
      <div class="card" style="padding:18px">
        <span class="badge teal">02</span>
        <h3 style="margin:10px 0 6px;font-size:14px">补充世界书</h3>
        <p class="muted" style="margin:0;font-size:12.5px">把地点、组织、人物关系写成条目；常驻或按关键词激活。</p>
      </div>
      <div class="card" style="padding:18px">
        <span class="badge amber">03</span>
        <h3 style="margin:10px 0 6px;font-size:14px">绑定到故事</h3>
        <p class="muted" style="margin:0;font-size:12.5px">默认使用当前角色；需要时可为单个故事绑定不同的角色或世界书。</p>
      </div>
    </div>
  `;
}

/* ------------------------------------------------------------------ */
/* 角色列表                                                            */
/* ------------------------------------------------------------------ */

function renderCharacters() {
  const data = state.data;
  const query = state.search.trim().toLowerCase();
  const filtered = data.characters.filter((card) => {
    if (state.charFilter !== 'all') {
      const type = (card.source && card.source.type) || 'manual';
      if (type !== state.charFilter) return false;
    }
    if (!query) return true;
    return `${card.name} ${card.description} ${(card.tags || []).join(' ')}`.toLowerCase().includes(query);
  });

  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1>角色身份</h1>
        <p>角色卡兼容酒馆 V2 JSON 与 PNG；字段会映射到故事设定，未填写的项回退到插件配置。</p>
      </div>
      <div class="page-actions">
        <button class="btn" data-action="goto-import">导入</button>
        <button class="btn btn-primary" data-action="new-character">＋ 新建角色</button>
      </div>
    </div>

    <div class="row wrap" style="margin-bottom:16px">
      <div class="segmented">
        ${[['all', '全部'], ['manual', '手动'], ['type0', 'Type0'], ['sillytavern', '酒馆导入']]
          .map(([key, label]) => `<button class="${state.charFilter === key ? 'active' : ''}" data-action="filter-characters" data-filter="${key}">${label}</button>`).join('')}
      </div>
      <span class="muted" style="font-size:12px">${filtered.length} / ${data.characters.length}</span>
    </div>

    ${filtered.length ? `<div class="char-grid">${filtered.map((card) => charCard(card)).join('')}</div>`
      : `<div class="empty"><div class="empty-icon">◐</div><h3>没有匹配的角色</h3><p>${query ? '换个关键词试试，或清空搜索。' : '创建第一个角色身份。'}</p><button class="btn btn-primary" data-action="new-character">新建角色</button></div>`}
  `;
}

/* ------------------------------------------------------------------ */
/* 角色编辑器                                                          */
/* ------------------------------------------------------------------ */

async function ensureCharacter(id) {
  if (state.character && state.character.id === id) return;
  state.character = null;
  if (!id) {
    state.character = emptyCharacter();
    return;
  }
  const result = await api('character.get', { id });
  state.character = result.character;
  state.characterDirty = false;
  state.charTab = 'identity';
  state.preview = null;
}

function emptyCharacter() {
  return {
    id: '', name: '', description: '', personality: '', scenario: '',
    first_mes: '', mes_example: '', creator_notes: '', system_prompt: '',
    post_history_instructions: '', alternate_greetings: [], tags: [],
    creator: '', character_version: '', avatar: '',
    extensions: {}, character_book: null,
    hds: {
      perspective: '', perspectives: [], supplementaryFacts: [], userProfile: '',
      relationship: '', world: '', supportingCast: '', location: '', style: '',
      timezone: 'Asia/Shanghai',
    },
    source: { type: 'manual', code: '', title: '', revision: 0, sections: [] },
  };
}

function field(label, key, value, { type = 'text', rows = 4, hint = '', required = false, placeholder = '', scope = 'character' } = {}) {
  const id = `f-${scope}-${key.replace(/\./g, '-')}`;
  const common = `id="${id}" data-scope="${scope}" data-field="${esc(key)}"`;
  let control = '';
  if (type === 'textarea') {
    control = `<textarea class="textarea" rows="${rows}" ${common} placeholder="${esc(placeholder)}">${esc(value)}</textarea>`;
  } else if (type === 'lines') {
    control = `<textarea class="textarea" rows="${rows}" ${common} placeholder="${esc(placeholder)}">${esc((value || []).join('\n'))}</textarea>`;
  } else if (type === 'tags') {
    control = `<input class="input" ${common} value="${esc((value || []).join(', '))}" placeholder="用逗号分隔" />`;
  } else {
    control = `<input class="input" ${common} value="${esc(value)}" placeholder="${esc(placeholder)}" />`;
  }
  return `<div class="field ${type === 'textarea' || type === 'lines' ? 'span-2' : ''}">
    <label for="${id}">${esc(label)}${required ? ' <span class="req">*</span>' : ''}</label>
    ${control}
    ${hint ? `<span class="hint">${esc(hint)}</span>` : ''}
  </div>`;
}

function renderCharacterEditor(id) {
  return ensureCharacter(id).then(() => {
    const card = state.character;
    if (!card) {
      view.innerHTML = `<div class="empty"><h3>角色不存在</h3><p>它可能已被删除。</p><button class="btn" data-action="goto-characters">返回列表</button></div>`;
      return;
    }
    const isNew = !card.id;
    const tabs = [
      ['identity', '身份'], ['dialogue', '对话'], ['prompting', '提示词'],
      ['hds', 'HDS 映射'], ['preview', '提示词预览'],
    ];
    const hds = card.hds || {};

    let body = '';
    if (state.charTab === 'identity') {
      body = `<div class="form-grid">
        ${field('角色名', 'name', card.name, { required: true, placeholder: '例如：林晚' })}
        ${field('作者', 'creator', card.creator, { placeholder: '可选' })}
        ${field('角色描述', 'description', card.description, { type: 'textarea', rows: 7, hint: '身份、经历、作息、习惯、压力与行为边界；会映射为 storyDefaults.characterProfile。' })}
        ${field('性格', 'personality', card.personality, { type: 'textarea', rows: 4, hint: '简短性格摘要；与描述重复时不会再次注入。' })}
        ${field('场景', 'scenario', card.scenario, { type: 'textarea', rows: 4, hint: '时代、地点与处境；HDS 世界未填写时作为 world 回退。' })}
        ${field('标签', 'tags', card.tags, { type: 'tags' })}
      </div>`;
    } else if (state.charTab === 'dialogue') {
      body = `<div class="form-grid">
        ${field('开场白 first_mes', 'first_mes', card.first_mes, { type: 'textarea', rows: 5, hint: '酒馆格式字段，仅作开场氛围参考，不作为已发生剧情注入。' })}
        ${field('备用开场', 'alternate_greetings', card.alternate_greetings, { type: 'lines', rows: 4, hint: '每行一条。' })}
        ${field('对话示例 mes_example', 'mes_example', card.mes_example, { type: 'textarea', rows: 7, hint: '作为语气参考注入系统提示词，并要求不得照抄。' })}
        ${field('作者注记', 'creator_notes', card.creator_notes, { type: 'textarea', rows: 3 })}
      </div>`;
    } else if (state.charTab === 'prompting') {
      body = `<div class="form-grid">
        ${field('角色系统提示', 'system_prompt', card.system_prompt, { type: 'textarea', rows: 7, hint: '角色级权威指令，优先于叙事文风，但不能覆盖 JSON 输出合约。' })}
        ${field('历史后置指令', 'post_history_instructions', card.post_history_instructions, { type: 'textarea', rows: 7, hint: '酒馆 post_history_instructions，按原义注入。' })}
      </div>`;
    } else if (state.charTab === 'hds') {
      body = `<div class="form-grid">
        ${field('个体视角', 'perspective', hds.perspective, { type: 'textarea', rows: 4, scope: 'hds', hint: '对应 storyDefaults.perspective。' })}
        ${field('多条视角', 'perspectives', hds.perspectives, { type: 'lines', rows: 4, scope: 'hds', hint: '每行一条，独立注入。' })}
        ${field('世界设定', 'world', hds.world, { type: 'textarea', rows: 5, scope: 'hds', hint: '优先于 scenario 映射为 storyDefaults.world。' })}
        ${field('配角', 'supportingCast', hds.supportingCast, { type: 'textarea', rows: 4, scope: 'hds' })}
        ${field('用户默认背景', 'userProfile', hds.userProfile, { type: 'textarea', rows: 4, scope: 'hds' })}
        ${field('初始关系', 'relationship', hds.relationship, { type: 'textarea', rows: 3, scope: 'hds' })}
        ${field('主要地点', 'location', hds.location, { scope: 'hds' })}
        ${field('时区', 'timezone', hds.timezone, { scope: 'hds', placeholder: 'Asia/Shanghai' })}
        ${field('故事级文风', 'style', hds.style, { type: 'textarea', rows: 4, scope: 'hds' })}
      </div>`;
    } else {
      body = `<div class="notice info" style="margin-bottom:14px">保存后此处显示该角色卡实际注入主叙事提示词的片段（角色系统提示 / 对话示例 / 激活的世界书条目）。</div>
        <pre class="code-preview" id="char-preview">${esc(state.preview?.characterBlock || '编辑任意字段后将自动生成预览…')}</pre>
        <h3 style="margin:18px 0 8px;font-size:13px;color:var(--text-2)">世界书</h3>
        <pre class="code-preview" id="char-world-preview">${esc(state.preview?.worldBlock || '当前角色未绑定世界书，或没有条目被激活。')}</pre>
        <h3 style="margin:18px 0 8px;font-size:13px;color:var(--text-2)">映射到 storyDefaults</h3>
        <pre class="code-preview" id="char-defaults">${esc(JSON.stringify(state.preview?.storyDefaults || {}, null, 2))}</pre>`;
    }

    const active = state.data.stats.activeCharacterId === card.id;
    view.innerHTML = `
      <div class="page-head">
        <div>
          <div class="row" style="margin-bottom:6px">
            <button class="btn btn-ghost btn-sm" data-action="goto-characters">← 返回</button>
            ${isNew ? '<span class="badge amber">未保存</span>' : `<span class="badge">${esc((card.source && card.source.type) || 'manual')}</span>`}
            ${active ? '<span class="badge success">当前角色</span>' : ''}
          </div>
          <h1>${esc(card.name || '新建角色')}</h1>
          <p>${esc(card.description ? card.description.slice(0, 120) : '填写角色身份，或从 Type0 / 酒馆角色卡导入。')}</p>
        </div>
        <div class="page-actions">
          ${isNew || active ? '' : `<button class="btn" data-action="activate-character" data-id="${esc(card.id)}">设为当前</button>`}
          <button class="btn" data-action="export-character" data-id="${esc(card.id)}" ${isNew ? 'disabled' : ''}>导出 JSON</button>
          <button class="btn" data-action="export-character-png" data-id="${esc(card.id)}" ${isNew ? 'disabled' : ''}>导出 PNG</button>
          <button class="btn btn-primary" data-action="save-character" ${state.saving ? 'disabled' : ''}>${state.saving ? '保存中…' : '保存'}</button>
        </div>
      </div>

      <div class="editor-layout">
        <div class="card editor-main">
          <div class="tabs">
            ${tabs.map(([key, label]) => `<button class="tab ${state.charTab === key ? 'active' : ''}" data-action="char-tab" data-tab="${key}">${label}</button>`).join('')}
          </div>
          ${body}
        </div>
        <aside class="editor-side">
          <div class="card side-card">
            <h3>角色概述</h3>
            <div class="kv"><span>来源</span><span>${esc(card.source?.title || (card.source?.type === 'type0' ? 'Type0' : '手动填写'))}</span></div>
            <div class="kv"><span>分享码</span><span>${esc(card.source?.code || '—')}</span></div>
            <div class="kv"><span>revision</span><span>${esc(card.source?.revision ?? '—')}</span></div>
            <div class="kv"><span>更新时间</span><span>${esc(fmtDate(card.updatedAt))}</span></div>
            <div class="kv"><span>世界设定</span><span>${hds.world ? '已填写' : '未填写'}</span></div>
            <div class="kv"><span>世界书</span><span>${(state.data.stats.activeLorebookIds || []).length} 本启用</span></div>
          </div>
          <div class="card side-card">
            <h3>会被注入什么</h3>
            <p class="muted" style="margin:0;font-size:12px;line-height:1.6">
              角色名 / 描述 / 视角 / 世界会进入故事设定；角色系统提示、历史后置指令、对话示例与激活的世界书条目会直接进入系统提示词。
            </p>
          </div>
          <div class="card side-card">
            <h3>危险区</h3>
            <button class="btn btn-danger btn-sm" data-action="delete-character" data-id="${esc(card.id)}" ${isNew ? 'disabled' : ''}>删除这个角色</button>
          </div>
        </aside>
      </div>
    `;
    bindCharacterForm();
    if (state.charTab === 'preview') refreshPreview();
  });
}

function bindCharacterForm() {
  $$('[data-scope]').forEach((input) => {
    input.addEventListener('input', () => {
      const scope = input.dataset.scope;
      const key = input.dataset.field;
      const target = scope === 'hds' ? state.character.hds : state.character;
      if (!target) return;
      const type = input.dataset.fieldType;
      let value = input.value;
      if (key === 'tags' || key === 'alternate_greetings' || key === 'perspectives') {
        value = key === 'tags' ? parseTags(value) : parseLines(value);
      }
      target[key] = value;
      markCharacterDirty();
      if (state.charTab === 'preview') schedulePreview();
    });
  });
}

function markCharacterDirty() {
  state.characterDirty = true;
  setSaveState('dirty', '未保存');
}

function schedulePreview() {
  clearTimeout(schedulePreview.timer);
  schedulePreview.timer = setTimeout(refreshPreview, 600);
}

async function refreshPreview() {
  if (!state.character) return;
  try {
    const result = await api('preview.prompt', {
      character: state.character,
      lorebookIds: state.data.stats.activeLorebookIds || [],
      query: state.previewQuery || '',
    });
    state.preview = result;
    const characterNode = $('#char-preview');
    const worldNode = $('#char-world-preview');
    const defaultsNode = $('#char-defaults');
    if (characterNode) characterNode.textContent = result.characterBlock || '（该角色没有额外注入的角色级提示词。）';
    if (worldNode) worldNode.textContent = result.worldBlock || '（没有激活的世界书条目。）';
    if (defaultsNode) defaultsNode.textContent = JSON.stringify(result.storyDefaults || {}, null, 2);
  } catch (error) {
    toast(error.message || String(error), 'error');
  }
}

async function saveCharacter({ silent = false } = {}) {
  if (!state.character || state.saving) return;
  if (!String(state.character.name || '').trim()) {
    toast('请先填写角色名', 'error');
    state.charTab = 'identity';
    renderCharacterEditor(state.character.id || '');
    return;
  }
  state.saving = true;
  setSaveState('saving', '保存中…');
  try {
    const result = await api('character.save', { character: state.character });
    state.character = result.character;
    state.characterDirty = false;
    state.data = result.state;
    setSaveState('idle', '已同步');
    if (!silent) toast('角色已保存');
    updateChrome();
    if (!state.route.id) navigate('characters', result.character.id);
    else renderCharacterEditor(result.character.id);
  } catch (error) {
    setSaveState('error', '保存失败');
    toast(error.message || String(error), 'error');
  } finally {
    state.saving = false;
  }
}

/* ------------------------------------------------------------------ */
/* 世界书                                                              */
/* ------------------------------------------------------------------ */

function renderLorebooks() {
  const books = state.data.lorebooks.filter((book) => {
    const query = state.search.trim().toLowerCase();
    if (!query) return true;
    return `${book.name} ${book.description}`.toLowerCase().includes(query);
  });

  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1>世界书</h1>
        <p>兼容酒馆 World Info：关键词 / 常驻条目、次要关键词逻辑、插入顺序与字符预算。</p>
      </div>
      <div class="page-actions">
        <button class="btn" data-action="goto-import">导入</button>
        <button class="btn btn-primary" data-action="new-lorebook">＋ 新建世界书</button>
      </div>
    </div>

    ${books.length ? `<div class="card list">
      ${books.map((book) => `<button class="list-row" data-action="open-lorebook" data-id="${esc(book.id)}">
        <div class="avatar" style="width:38px;height:38px;border-radius:11px;font-size:13px">▤</div>
        <div class="list-main">
          <div class="list-title">${esc(book.name)} ${book.constantCount ? `<span class="badge amber">${book.constantCount} 常驻</span>` : ''}</div>
          <div class="list-sub">${esc(book.description || '未填写说明')}</div>
        </div>
        <div class="list-meta">
          <span class="muted" style="font-size:12px">${book.entryCount} 条 · 预算 ${book.budget}</span>
          <span class="badge">${esc((book.source && book.source.type) === 'type0' ? 'Type0' : '手动')}</span>
        </div>
      </button>`).join('')}
    </div>` : `<div class="empty"><div class="empty-icon">▤</div><h3>还没有世界书</h3><p>把地点、组织、人物关系写成条目，让叙事在需要时自动引用。</p><button class="btn btn-primary" data-action="new-lorebook">新建世界书</button></div>`}
  `;
}

function emptyLorebook() {
  return {
    id: '', name: '', description: '', scan_depth: 4, token_budget: 6000,
    recursive_scanning: false, entries: [], extensions: {},
    source: { type: 'manual' },
  };
}

function renderLorebookEditor(id) {
  const load = async () => {
    if (id === 'new') {
      if (!state.book) state.book = emptyLorebook();
      return;
    }
    if (state.book && state.book.id === id) return;
    if (!id) { state.book = emptyLorebook(); return; }
    const result = await api('lorebook.get', { id });
    state.book = result.lorebook;
    state.bookDirty = false;
  };
  return load().then(() => {
    const book = state.book;
    if (!book) {
      view.innerHTML = `<div class="empty"><h3>世界书不存在</h3><button class="btn" data-action="goto-lorebooks">返回列表</button></div>`;
      return;
    }
    const query = state.entryQuery.trim().toLowerCase();
    const entries = (book.entries || []).filter((entry) => {
      if (!query) return true;
      return `${entry.comment} ${(entry.keys || []).join(' ')} ${entry.content}`.toLowerCase().includes(query);
    });
    const isNew = !book.id;

    view.innerHTML = `
      <div class="page-head">
        <div>
          <div class="row" style="margin-bottom:6px">
            <button class="btn btn-ghost btn-sm" data-action="goto-lorebooks">← 返回</button>
            ${isNew ? '<span class="badge amber">未保存</span>' : ''}
          </div>
          <h1>${esc(book.name || '新建世界书')}</h1>
          <p>${book.entries.length} 个条目 · 预算 ${book.token_budget} 字符 · 扫描深度 ${book.scan_depth}</p>
        </div>
        <div class="page-actions">
          <button class="btn" data-action="export-lorebook" data-id="${esc(book.id)}" ${isNew ? 'disabled' : ''}>导出 JSON</button>
          <button class="btn" data-action="duplicate-lorebook" data-id="${esc(book.id)}" ${isNew ? 'disabled' : ''}>复制</button>
          <button class="btn btn-primary" data-action="save-lorebook" ${state.saving ? 'disabled' : ''}>${state.saving ? '保存中…' : '保存'}</button>
        </div>
      </div>

      <div class="editor-layout">
        <div class="card editor-main" style="padding:0;overflow:hidden">
          <div class="entry-toolbar">
            <div class="field" style="flex:1;min-width:180px">
              <input class="input" id="entry-search" placeholder="搜索条目、关键词或内容…" value="${esc(state.entryQuery)}" />
            </div>
            <button class="btn btn-sm" data-action="add-entry">＋ 新增条目</button>
          </div>
          ${entries.length ? `<table class="entry-table">
            <thead><tr><th style="width:38%">条目</th><th>内容</th><th style="width:90px">顺序</th><th style="width:90px">概率</th><th style="width:110px">状态</th><th style="width:90px"></th></tr></thead>
            <tbody>
              ${entries.map((entry) => `<tr class="${entry.enabled ? '' : 'is-disabled'}">
                <td>
                  <div class="entry-name">${esc(entry.comment || '未命名条目')} ${entry.constant ? '<span class="badge amber">常驻</span>' : ''}</div>
                  <div style="margin-top:4px">${(entry.keys || []).slice(0, 4).map((key) => `<span class="keyword">${esc(key)}</span>`).join(' ')}</div>
                </td>
                <td><div class="entry-content">${esc(entry.content)}</div></td>
                <td class="mono">${esc(entry.insertion_order)}</td>
                <td class="mono">${esc(entry.probability)}%</td>
                <td>
                  <label class="switch">
                    <input type="checkbox" data-action="toggle-entry" data-id="${esc(entry.id)}" ${entry.enabled ? 'checked' : ''} />
                    <span class="track"></span>
                  </label>
                </td>
                <td class="entry-actions">
                  <button class="icon-btn" title="编辑" data-action="edit-entry" data-id="${esc(entry.id)}">✎</button>
                  <button class="icon-btn" title="删除" data-action="delete-entry" data-id="${esc(entry.id)}">✕</button>
                </td>
              </tr>`).join('')}
            </tbody>
          </table>` : `<div class="empty" style="border:0;border-radius:0"><div class="empty-icon">▤</div><h3>${query ? '没有匹配条目' : '世界书还是空的'}</h3><p>${query ? '换个关键词试试。' : '新增条目，填写关键词与内容；常驻条目会每回合注入。'}</p><button class="btn" data-action="add-entry">新增条目</button></div>`}
        </div>

        <aside class="editor-side">
          <div class="card side-card">
            <h3>世界书设置</h3>
            <div class="field" style="margin-bottom:10px">
              <label>名称</label>
              <input class="input" data-scope="book" data-field="name" value="${esc(book.name)}" placeholder="例如：港城设定集" />
            </div>
            <div class="field" style="margin-bottom:10px">
              <label>说明</label>
              <textarea class="textarea" rows="2" data-scope="book" data-field="description">${esc(book.description)}</textarea>
            </div>
            <div class="field" style="margin-bottom:10px">
              <label>扫描深度</label>
              <input class="input" type="number" data-scope="book" data-field="scan_depth" value="${esc(book.scan_depth)}" />
            </div>
            <div class="field">
              <label>注入字符预算</label>
              <input class="input" type="number" data-scope="book" data-field="token_budget" value="${esc(book.token_budget)}" />
            </div>
          </div>
          <div class="card side-card">
            <h3>激活规则</h3>
            <p class="muted" style="margin:0;font-size:12px;line-height:1.6">
              常驻条目始终注入；其余条目按关键词命中。次要关键词配合 AND ANY / NOT ALL / NOT ANY / AND ALL 逻辑；按插入顺序排序后受字符预算裁剪。
            </p>
          </div>
        </aside>
      </div>
    `;
    bindBookForm();
    const search = $('#entry-search');
    if (search) {
      search.addEventListener('input', () => {
        state.entryQuery = search.value;
        renderLorebookEditor(state.book.id || '');
        const node = $('#entry-search');
        if (node) { node.focus(); node.setSelectionRange(node.value.length, node.value.length); }
      });
    }
  });
}

function bindBookForm() {
  $$('[data-scope="book"]').forEach((input) => {
    input.addEventListener('input', () => {
      const key = input.dataset.field;
      const numeric = ['scan_depth', 'token_budget'].includes(key);
      state.book[key] = numeric ? Number(input.value || 0) : input.value;
      state.bookDirty = true;
      setSaveState('dirty', '未保存');
    });
  });
}

async function saveLorebook({ silent = false } = {}) {
  if (!state.book || state.saving) return;
  if (!String(state.book.name || '').trim()) {
    toast('请先填写世界书名称', 'error');
    return;
  }
  state.saving = true;
  setSaveState('saving', '保存中…');
  try {
    const result = await api('lorebook.save', { lorebook: state.book });
    state.book = result.lorebook;
    state.bookDirty = false;
    state.data = result.state;
    setSaveState('idle', '已同步');
    if (!silent) toast('世界书已保存');
    updateChrome();
    if (!state.route.id || state.route.id === 'new') navigate('lorebooks', result.lorebook.id);
    else renderLorebookEditor(result.lorebook.id);
  } catch (error) {
    setSaveState('error', '保存失败');
    toast(error.message || String(error), 'error');
  } finally {
    state.saving = false;
  }
}

function openEntryDrawer(entryId = '') {
  const book = state.book;
  if (!book) return;
  const existing = entryId ? (book.entries || []).find((item) => item.id === entryId) : null;
  const entry = existing || {
    id: '', comment: '', keys: [], secondary_keys: [], content: '',
    enabled: true, constant: false, selective: true, selectiveLogic: 0,
    insertion_order: (book.entries || []).length * 10 + 100, position: 'before_char',
    probability: 100, depth: 4, role: 0, group: '',
    case_sensitive: false, match_whole_words: false, use_regex: false, extensions: {},
  };
  drawerRoot.hidden = false;
  drawerRoot.innerHTML = `
    <div class="drawer-backdrop" data-close="drawer"></div>
    <aside class="drawer" role="dialog" aria-modal="true" aria-label="编辑世界书条目">
      <div class="drawer-head">
        <h2>${existing ? '编辑条目' : '新增条目'}</h2>
        <button class="icon-btn" data-close="drawer" aria-label="关闭">✕</button>
      </div>
      <div class="drawer-body">
        <div class="form-grid">
          <div class="field span-2"><label>条目名</label><input class="input" id="entry-comment" value="${esc(entry.comment)}" placeholder="例如：旧港区" /></div>
          <div class="field span-2"><label>关键词</label><input class="input" id="entry-keys" value="${esc((entry.keys || []).join(', '))}" placeholder="逗号分隔；常驻条目可留空" /></div>
          <div class="field span-2"><label>次要关键词</label><input class="input" id="entry-secondary" value="${esc((entry.secondary_keys || []).join(', '))}" placeholder="可选" /></div>
          <div class="field span-2"><label>内容 <span class="req">*</span></label><textarea class="textarea" id="entry-content" rows="8">${esc(entry.content)}</textarea></div>
          <div class="field"><label>插入顺序</label><input class="input" type="number" id="entry-order" value="${esc(entry.insertion_order)}" /></div>
          <div class="field"><label>触发概率 %</label><input class="input" type="number" id="entry-probability" min="0" max="100" value="${esc(entry.probability)}" /></div>
          <div class="field"><label>注入位置</label>
            <select class="select" id="entry-position">
              <option value="before_char" ${entry.position === 'before_char' ? 'selected' : ''}>角色设定之前</option>
              <option value="after_char" ${entry.position === 'after_char' ? 'selected' : ''}>角色设定之后</option>
            </select>
          </div>
          <div class="field"><label>次要关键词逻辑</label>
            <select class="select" id="entry-logic">
              <option value="0" ${Number(entry.selectiveLogic) === 0 ? 'selected' : ''}>任一命中 (AND ANY)</option>
              <option value="1" ${Number(entry.selectiveLogic) === 1 ? 'selected' : ''}>并非全部命中 (NOT ALL)</option>
              <option value="2" ${Number(entry.selectiveLogic) === 2 ? 'selected' : ''}>全不命中 (NOT ANY)</option>
              <option value="3" ${Number(entry.selectiveLogic) === 3 ? 'selected' : ''}>全部命中 (AND ALL)</option>
            </select>
          </div>
          <div class="field"><label>深度</label><input class="input" type="number" id="entry-depth" value="${esc(entry.depth)}" /></div>
          <div class="field"><label>分组</label><input class="input" id="entry-group" value="${esc(entry.group)}" /></div>
          <div class="field span-2 row wrap" style="gap:18px">
            <label class="switch"><input type="checkbox" id="entry-enabled" ${entry.enabled ? 'checked' : ''}/><span class="track"></span><span>启用</span></label>
            <label class="switch"><input type="checkbox" id="entry-constant" ${entry.constant ? 'checked' : ''}/><span class="track"></span><span>常驻</span></label>
            <label class="switch"><input type="checkbox" id="entry-selective" ${entry.selective ? 'checked' : ''}/><span class="track"></span><span>启用次要关键词</span></label>
            <label class="switch"><input type="checkbox" id="entry-case" ${entry.case_sensitive ? 'checked' : ''}/><span class="track"></span><span>区分大小写</span></label>
            <label class="switch"><input type="checkbox" id="entry-whole" ${entry.match_whole_words ? 'checked' : ''}/><span class="track"></span><span>全词匹配</span></label>
            <label class="switch"><input type="checkbox" id="entry-regex" ${entry.use_regex ? 'checked' : ''}/><span class="track"></span><span>正则</span></label>
          </div>
        </div>
      </div>
      <div class="drawer-foot">
        ${existing ? '<button class="btn btn-danger" id="entry-delete">删除</button>' : ''}
        <div class="spacer"></div>
        <button class="btn" data-close="drawer">取消</button>
        <button class="btn btn-primary" id="entry-save">${existing ? '保存条目' : '添加条目'}</button>
      </div>
    </aside>
  `;
  bindModalClosers();

  $('#entry-save').addEventListener('click', () => {
    const content = $('#entry-content').value.trim();
    if (!content) { toast('条目内容不能为空', 'error'); return; }
    const data = {
      id: entry.id || `entry_${Math.random().toString(16).slice(2, 10)}`,
      comment: $('#entry-comment').value.trim(),
      keys: parseTags($('#entry-keys').value),
      secondary_keys: parseTags($('#entry-secondary').value),
      content,
      enabled: $('#entry-enabled').checked,
      constant: $('#entry-constant').checked,
      selective: $('#entry-selective').checked,
      selectiveLogic: Number($('#entry-logic').value),
      insertion_order: Number($('#entry-order').value || 100),
      position: $('#entry-position').value,
      probability: Math.max(0, Math.min(100, Number($('#entry-probability').value || 100))),
      depth: Number($('#entry-depth').value || 4),
      role: entry.role || 0,
      group: $('#entry-group').value.trim(),
      case_sensitive: $('#entry-case').checked,
      match_whole_words: $('#entry-whole').checked,
      use_regex: $('#entry-regex').checked,
      extensions: entry.extensions || {},
    };
    if (existing) {
      const index = state.book.entries.findIndex((item) => item.id === existing.id);
      state.book.entries[index] = data;
    } else {
      state.book.entries.push(data);
    }
    state.bookDirty = true;
    setSaveState('dirty', '未保存');
    closeDrawer();
    renderLorebookEditor(state.book.id || '');
    toast(existing ? '条目已更新（记得保存世界书）' : '条目已添加（记得保存世界书）', 'info');
  });

  const deleteButton = $('#entry-delete');
  if (deleteButton) {
    deleteButton.addEventListener('click', async () => {
      const ok = await confirmDialog('删除条目', `确定删除「${entry.comment || '未命名条目'}」？保存世界书后生效。`, '删除');
      if (!ok) return;
      state.book.entries = state.book.entries.filter((item) => item.id !== entry.id);
      state.bookDirty = true;
      setSaveState('dirty', '未保存');
      closeDrawer();
      renderLorebookEditor(state.book.id || '');
    });
  }
}

/* ------------------------------------------------------------------ */
/* 提示词                                                              */
/* ------------------------------------------------------------------ */

function renderPrompts() {
  const draft = state.promptsDraft || {};
  const groups = [
    ['narrative', '叙事核心', '这些字段直接进入主叙事系统提示词；留空表示回退插件配置或内置默认。'],
    ['advanced', '高级 / 后台任务', '压缩、情绪侧端与顶层覆盖；除非明确知道影响，否则保持留空。'],
  ];
  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1>叙事提示词</h1>
        <p>统一管理全局文风、主叙事方向与后台任务提示词；配置页中的同名字段已隐藏并作为回退值。</p>
      </div>
      <div class="page-actions">
        <button class="btn" data-action="reset-prompts">恢复配置默认</button>
        <button class="btn btn-primary" data-action="save-prompts">保存</button>
      </div>
    </div>
    ${groups.map(([group, title, hint]) => `
      <div class="section-title"><h2>${title}</h2><span>${hint}</span></div>
      <div class="card" style="padding:20px">
        <div class="form-grid">
          ${(state.data.catalog.prompts || []).filter((item) => item.group === group).map((item) => `
            <div class="field span-2">
              <label>${esc(item.label)}</label>
              <textarea class="textarea" rows="${item.rows || 4}" data-scope="prompt" data-field="${esc(item.key)}" placeholder="${esc(item.hint || '')}">${esc(draft[item.key] || '')}</textarea>
              ${item.hint ? `<span class="hint">${esc(item.hint)}</span>` : ''}
            </div>`).join('')}
        </div>
      </div>
    `).join('')}
  `;
  $$('[data-scope="prompt"]').forEach((input) => {
    input.addEventListener('input', () => {
      state.promptsDraft[input.dataset.field] = input.value;
      state.promptsDirty = true;
      setSaveState('dirty', '未保存');
    });
  });
}

async function savePrompts({ silent = false } = {}) {
  try {
    const result = await api('prompts.save', { prompts: state.promptsDraft });
    state.promptsDraft = { ...result.prompts };
    state.promptsDirty = false;
    state.data = result.state;
    setSaveState('idle', '已同步');
    if (!silent) toast('提示词已保存');
  } catch (error) {
    setSaveState('error', '保存失败');
    toast(error.message || String(error), 'error');
  }
}

/* ------------------------------------------------------------------ */
/* 绑定                                                                */
/* ------------------------------------------------------------------ */

function renderBinding() {
  const data = state.data;
  const active = data.active || { characterId: '', lorebookIds: [] };
  const stories = data.stories || [];
  const characterOptions = data.characters.map((card) => `<option value="${esc(card.id)}" ${active.characterId === card.id ? 'selected' : ''}>${esc(card.name)}</option>`).join('');

  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1>故事绑定</h1>
        <p>默认绑定决定新故事与未单独配置的故事使用哪个角色；单个故事可覆盖。</p>
      </div>
      <div class="page-actions">
        <button class="btn btn-primary" data-action="save-active">保存默认绑定</button>
      </div>
    </div>

    <div class="grid grid-2">
      <div class="card" style="padding:20px">
        <h2 style="font-size:15px;margin:0 0 14px">默认绑定</h2>
        <div class="field" style="margin-bottom:14px">
          <label>默认角色</label>
          <select class="select" id="active-character">
            <option value="">（不绑定，使用配置回退）</option>
            ${characterOptions}
          </select>
        </div>
        <div class="field">
          <label>默认世界书</label>
          <div id="active-books" class="row wrap" style="gap:8px">
            ${data.lorebooks.length ? data.lorebooks.map((book) => `
              <label class="switch" style="border:1px solid var(--border);border-radius:9px;padding:7px 10px">
                <input type="checkbox" value="${esc(book.id)}" ${active.lorebookIds.includes(book.id) ? 'checked' : ''} />
                <span class="track"></span><span>${esc(book.name)}</span>
              </label>`).join('') : '<span class="muted" style="font-size:12px">还没有世界书。</span>'}
          </div>
        </div>
      </div>

      <div class="card" style="padding:20px">
        <h2 style="font-size:15px;margin:0 0 14px">生效方式</h2>
        <div class="kv"><span>角色卡未填写字段</span><span>回退插件配置 storyDefaults</span></div>
        <div class="kv"><span>世界书</span><span>仅注入激活条目，受字符预算限制</span></div>
        <div class="kv"><span>已运行故事</span><span>角色卡实时生效；故事设定需点击「应用到故事」</span></div>
        <div class="notice info" style="margin-top:14px">
          角色卡中的 name / description / world 等会实时进入提示词；若要让故事档案本身也改写，使用下方「应用到故事」。
        </div>
      </div>
    </div>

    <div class="section-title"><h2>故事列表</h2><span>${stories.length} 个</span></div>
    ${stories.length ? `<div class="card list">
      ${stories.map((story) => {
        const binding = data.bindings[story.id] || {};
        return `<div class="list-row">
          <div class="list-main">
            <div class="list-title">${esc(story.label)} ${story.status === 'active' ? '<span class="badge success">运行中</span>' : ''}</div>
            <div class="list-sub">${esc(story.platform)} · ${esc(story.id)} · 更新 ${fmtDate(story.updatedAt)}</div>
          </div>
          <div class="list-meta">
            <select class="select" style="width:180px;height:34px" data-story-character="${esc(story.id)}">
              <option value="">跟随默认</option>
              ${data.characters.map((card) => `<option value="${esc(card.id)}" ${binding.characterId === card.id ? 'selected' : ''}>${esc(card.name)}</option>`).join('')}
            </select>
            <button class="btn btn-sm" data-action="bind-story" data-id="${esc(story.id)}">绑定</button>
            <button class="btn btn-sm" data-action="apply-story" data-id="${esc(story.id)}">应用到故事</button>
          </div>
        </div>`;
      }).join('')}
    </div>` : `<div class="empty"><div class="empty-icon">⇄</div><h3>还没有运行中的故事</h3><p>先在插件配置页完成模型与白名单，然后从聊天里说第一句话，或执行 interlude.story.start。</p></div>`}
  `;
}

/* ------------------------------------------------------------------ */
/* 导入中心                                                            */
/* ------------------------------------------------------------------ */

function renderImport() {
  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1>导入中心</h1>
        <p>Type0 是首个内置来源：选择「Type0 导入」后会自动拉取并映射角色身份；也可以手动填写或导入酒馆文件。</p>
      </div>
      <div class="page-actions">
        <button class="btn btn-primary" data-action="new-character">打开导入向导</button>
      </div>
    </div>

    <div class="import-grid">
      <div class="card" style="padding:20px">
        <span class="badge accent">来源 A</span>
        <h2 style="font-size:16px;margin:10px 0 6px">Type0 分享码</h2>
        <p class="muted" style="margin:0 0 14px;font-size:12.5px">直连分享站拉取预设，把 storyDefaults 自动映射为角色卡与世界书。</p>
        <div class="field" style="margin-bottom:10px">
          <label>分享站地址</label>
          <input class="input" id="import-base" value="${esc(state.data.meta.defaultShareBase || 'https://typ0.space')}" />
        </div>
        <div class="field" style="margin-bottom:12px">
          <label>分享码 / 链接</label>
          <input class="input" id="import-code" placeholder="例如：ABCD-1234 或 https://typ0.space/p/ABCD-1234" />
        </div>
        <button class="btn btn-primary" data-action="type0-preview">连接并预览</button>
        <div id="type0-result" style="margin-top:14px"></div>
      </div>

      <div class="card" style="padding:20px">
        <span class="badge teal">来源 B</span>
        <h2 style="font-size:16px;margin:10px 0 6px">手动填写</h2>
        <p class="muted" style="margin:0 0 14px;font-size:12.5px">从空白角色卡开始，逐项填写身份、世界与提示词；适合原创角色。</p>
        <button class="btn" data-action="new-character" style="width:100%;height:44px">打开角色编辑向导</button>
        <div class="notice info" style="margin-top:14px">
          Type0 只提供配置分区（storyDefaults 等），不包含酒馆 PNG 头像；导入后可在编辑器中补充头像与对话示例。
        </div>
      </div>
    </div>

    <div class="section-title"><h2>Type0 自动同步</h2><span>WebUI 内配置，插件加载时自动检查</span></div>
    <div class="card" style="padding:20px">
      <div class="grid grid-2">
        <div class="field"><label class="switch"><input type="checkbox" id="preset-enabled" /><span class="track"></span><span>启用自动同步</span></label></div>
        <div class="field"><label class="switch"><input type="checkbox" id="preset-auto" /><span class="track"></span><span>插件加载时检查更新</span></label></div>
        <div class="field span-2"><label>分享站地址</label><input class="input" id="preset-base" placeholder="https://typ0.space" /></div>
        <div class="field span-2"><label>分享码 / 完整链接</label><input class="input" id="preset-code" placeholder="ABCD-1234 或 https://typ0.space/p/ABCD-1234" /></div>
        <div class="field span-2"><label>同步分区（逗号分隔，留空 = 全部）</label><input class="input" id="preset-sections" placeholder="runtime, urge, memory" /></div>
        <div class="field"><label class="switch"><input type="checkbox" id="preset-apply" /><span class="track"></span><span>应用到当前故事</span></label></div>
      </div>
      <div style="display:flex;gap:10px;margin-top:16px">
        <button class="btn btn-primary" id="preset-save">保存自动同步设置</button>
        <button class="btn" id="preset-run">立即同步</button>
      </div>
      <div class="notice info" style="margin-top:14px">自动同步只写配置分区与当前故事设定；角色卡、世界书与提示词仍由本页导入与编辑器管理。</div>
    </div>

    <div class="section-title"><h2>文件导入</h2><span>酒馆 V2 JSON / PNG · World Info JSON</span></div>
    <div class="import-grid">
      <label class="dropzone" id="drop-character">
        <input type="file" accept=".json,.png,application/json,image/png" hidden />
        <div class="dz-icon">◐</div>
        <h3>角色卡</h3>
        <p>拖入或点击选择 .json / .png；PNG 会读取 chara 数据块。</p>
      </label>
      <label class="dropzone" id="drop-lorebook">
        <input type="file" accept=".json,application/json" hidden />
        <div class="dz-icon">▤</div>
        <h3>世界书</h3>
        <p>拖入或点击选择 World Info JSON（entries 支持对象或数组）。</p>
      </label>
    </div>

    <div class="section-title"><h2>格式说明</h2><span>导入后统一转为内部结构</span></div>
    <div class="card" style="padding:20px">
      <div class="kv"><span>角色卡</span><span>V2 data / V1 平铺 / HDS 扩展 hds</span></div>
      <div class="kv"><span>世界书</span><span>entries 对象或数组；key/keysecondary/order/disable 自动归一</span></div>
      <div class="kv"><span>导出</span><span>角色卡 V2 JSON（可导出 PNG，需头像）、World Info JSON</span></div>
    </div>
  `;
  bindImportPage();
}

function bindImportPage() {
  bindDropzone($('#drop-character'), 'character');
  bindDropzone($('#drop-lorebook'), 'lorebook');
  bindPresetSync();
}

function bindPresetSync() {
  const preset = state.data.presetSync || {};
  const enabled = $('#preset-enabled');
  if (!enabled) return;
  enabled.checked = !!preset.enabled;
  $('#preset-auto').checked = !!preset.autoUpdate;
  $('#preset-base').value = preset.baseUrl || (state.data.meta.defaultShareBase || 'https://typ0.space');
  $('#preset-code').value = preset.shareUrl || preset.shareCode || '';
  $('#preset-sections').value = Array.isArray(preset.sections) ? preset.sections.join(', ') : '';
  $('#preset-apply').checked = !!preset.applyToCurrentStory;

  $('#preset-save').addEventListener('click', async () => {
    const raw = $('#preset-code').value.trim();
    const isUrl = /^https?:\/\//i.test(raw);
    const sections = $('#preset-sections').value
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean);
    try {
      setSaveState('saving', '保存中…');
      const result = await api('preset.sync.save', {
        presetSync: {
          enabled: $('#preset-enabled').checked,
          baseUrl: $('#preset-base').value.trim(),
          shareUrl: isUrl ? raw : '',
          shareCode: isUrl ? '' : raw,
          autoUpdate: $('#preset-auto').checked,
          sections,
          applyToCurrentStory: $('#preset-apply').checked,
        },
      });
      state.data = result.state;
      setSaveState('idle', '已保存');
      toast('自动同步设置已保存');
    } catch (error) {
      setSaveState('error', '保存失败');
      toast(error.message || String(error), 'error');
    }
  });

  $('#preset-run').addEventListener('click', async () => {
    try {
      setSaveState('saving', '同步中…');
      const result = await api('preset.sync.run', { force: true });
      state.data = result.state;
      setSaveState('idle', '已同步');
      toast(result.message || (result.applied ? '已同步' : '没有新版本'));
    } catch (error) {
      setSaveState('error', '同步失败');
      toast(error.message || String(error), 'error');
    }
  });
}

async function previewType0FromImportPage() {
  const baseUrl = $('#import-base').value.trim();
  const code = $('#import-code').value.trim();
  const resultNode = $('#type0-result');
  if (!code) { toast('请填写分享码或链接', 'error'); return; }
  resultNode.innerHTML = '<div class="notice info">正在连接分享站…</div>';
  try {
    const preview = await api('type0.preview', { baseUrl, code });
    resultNode.innerHTML = renderType0Preview(preview, { compact: true });
    bindType0Import(resultNode, preview, { baseUrl, code });
  } catch (error) {
    resultNode.innerHTML = `<div class="notice danger">${esc(error.message || error)}</div>`;
  }
}

function bindDropzone(node, kind) {
  if (!node) return;
  const input = $('input[type="file"]', node);
  const open = () => input.click();
  node.addEventListener('click', (event) => {
    event.preventDefault();
    if (event.target !== input) open();
  });
  node.addEventListener('dragover', (event) => { event.preventDefault(); node.classList.add('is-over'); });
  node.addEventListener('dragleave', () => node.classList.remove('is-over'));
  node.addEventListener('drop', async (event) => {
    event.preventDefault();
    node.classList.remove('is-over');
    const file = event.dataTransfer.files?.[0];
    if (file) await importFile(file, kind);
  });
  input.addEventListener('change', async () => {
    const file = input.files?.[0];
    if (file) await importFile(file, kind);
    input.value = '';
  });
}

async function importFile(file, kind) {
  if (file.size > 8 * 1024 * 1024) { toast('文件超过 8MB 上限', 'error'); return; }
  const contentBase64 = await fileToBase64(file);
  try {
    setSaveState('saving', '导入中…');
    const action = kind === 'character' ? 'import.character' : 'import.lorebook';
    const result = await api(action, { filename: file.name, contentBase64 });
    state.data = result.state;
    state.promptsDraft = { ...(result.state.prompts || {}) };
    setSaveState('idle', '已同步');
    updateChrome();
    toast(`${file.name} 导入成功`);
    if (kind === 'character') {
      state.character = null;
      navigate('characters', result.character.id);
    } else {
      state.book = null;
      navigate('lorebooks', result.lorebook.id);
    }
  } catch (error) {
    setSaveState('error', '导入失败');
    toast(error.message || String(error), 'error');
  }
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || '');
      resolve(result.includes(',') ? result.split(',', 2)[1] : result);
    };
    reader.onerror = () => reject(new Error('读取文件失败'));
    reader.readAsDataURL(file);
  });
}

/* ------------------------------------------------------------------ */
/* 说明                                                                */
/* ------------------------------------------------------------------ */

function renderAbout() {
  view.innerHTML = `
    <div class="page-head">
      <div><h1>说明</h1><p>内容层与连接层分离后的边界、格式与映射规则。</p></div>
    </div>
    <div class="grid grid-2">
      <div class="card" style="padding:20px">
        <h2 style="font-size:15px;margin:0 0 10px">配置页保留什么</h2>
        <div class="kv"><span>模型连接 / API Key</span><span>插件配置 → 模型中心</span></div>
        <div class="kv"><span>QQ / OneBot 账号</span><span>插件配置 → QQ 接入</span></div>
        <div class="kv"><span>运行节奏 / 记忆 / 浏览器</span><span>插件配置</span></div>
        <div class="kv"><span>Type0 自动同步</span><span>WebUI → 导入中心</span></div>
      </div>
      <div class="card" style="padding:20px">
        <h2 style="font-size:15px;margin:0 0 10px">WebUI 管理什么</h2>
        <div class="kv"><span>角色身份</span><span>角色卡 / 故事设定映射</span></div>
        <div class="kv"><span>世界书</span><span>条目、关键词、预算</span></div>
        <div class="kv"><span>叙事提示词</span><span>文风 / 主叙事 / 压缩 / Alter</span></div>
        <div class="kv"><span>故事绑定</span><span>默认与按故事覆盖</span></div>
      </div>
    </div>

    <div class="section-title"><h2>数据与兼容</h2><span>本地 JSON，原子写入</span></div>
    <div class="card" style="padding:20px">
      <div class="kv"><span>存储位置</span><span class="mono">data/plugin_data/astrbot_plugin_hds_interlude/content.json</span></div>
      <div class="kv"><span>角色卡格式</span><span>SillyTavern V2 JSON / V1 / PNG chara；导出 V2</span></div>
      <div class="kv"><span>世界书格式</span><span>World Info JSON；entries 对象或数组</span></div>
      <div class="kv"><span>Type0</span><span>/api/preset/&lt;code&gt;（含自动回退路径）</span></div>
      <div class="notice info" style="margin-top:14px">
        修改内容后无需重载插件：服务层每次读取配置时都会叠加 WebUI 内容。删除 content.json 即清空 WebUI 内容（角色/世界书/提示词/自动同步），叙事回退内置默认。
      </div>
    </div>
  `;
}

/* ------------------------------------------------------------------ */
/* 新建角色向导（手动 / Type0）                                        */
/* ------------------------------------------------------------------ */

function openNewCharacterModal() {
  modalRoot.hidden = false;
  modalRoot.innerHTML = `
    <div class="modal-backdrop" data-close="modal"></div>
    <div class="modal wide" role="dialog" aria-modal="true" aria-label="新建角色">
      <div class="modal-head">
        <h2>新建角色身份</h2>
        <button class="icon-btn" data-close="modal" aria-label="关闭">✕</button>
      </div>
      <div class="modal-body">
        <div class="field" style="max-width:320px;margin-bottom:18px">
          <label for="new-source">来源</label>
          <select class="select" id="new-source">
            <option value="manual">手动填写</option>
            <option value="type0">Type0 导入</option>
          </select>
          <span class="hint">Type0 会直连分享站获取预设并自动填充；手动填写从空白开始。</span>
        </div>

        <div id="source-manual">
          <div class="form-grid">
            <div class="field"><label>角色名 <span class="req">*</span></label><input class="input" id="manual-name" placeholder="例如：林晚" /></div>
            <div class="field"><label>标签</label><input class="input" id="manual-tags" placeholder="用逗号分隔" /></div>
            <div class="field span-2"><label>角色描述</label><textarea class="textarea" id="manual-description" rows="6" placeholder="身份、经历、作息、习惯与行为边界。"></textarea></div>
            <div class="field"><label>性格</label><textarea class="textarea" id="manual-personality" rows="3"></textarea></div>
            <div class="field"><label>场景 / 世界</label><textarea class="textarea" id="manual-scenario" rows="3"></textarea></div>
          </div>
        </div>

        <div id="source-type0" hidden>
          <div class="form-grid">
            <div class="field span-2"><label>分享站地址</label><input class="input" id="type0-base" value="${esc(state.data.meta.defaultShareBase || 'https://typ0.space')}" /></div>
            <div class="field span-2"><label>分享码 / 完整链接 <span class="req">*</span></label><input class="input" id="type0-code" placeholder="ABCD-1234 或 https://typ0.space/p/ABCD-1234" /></div>
          </div>
          <div class="row" style="margin:14px 0">
            <button class="btn" id="type0-connect">连接并预览</button>
            <span class="muted" style="font-size:12px">只读取公开预设；不会上传本地数据。</span>
          </div>
          <div id="type0-preview-area"></div>
        </div>
      </div>
      <div class="modal-foot">
        <button class="btn" data-close="modal">取消</button>
        <button class="btn btn-primary" id="new-submit">创建并编辑</button>
      </div>
    </div>
  `;
  bindModalClosers();

  const source = $('#new-source');
  const manual = $('#source-manual');
  const type0 = $('#source-type0');
  const submit = $('#new-submit');
  let type0Preview = null;

  source.addEventListener('change', () => {
    const isType0 = source.value === 'type0';
    manual.hidden = isType0;
    type0.hidden = !isType0;
    type0Preview = null;
    submit.textContent = isType0 ? '导入并设为当前' : '创建并编辑';
    submit.disabled = false;
  });

  $('#type0-connect').addEventListener('click', async () => {
    const baseUrl = $('#type0-base').value.trim();
    const code = $('#type0-code').value.trim();
    const area = $('#type0-preview-area');
    if (!code) { toast('请填写分享码或链接', 'error'); return; }
    area.innerHTML = '<div class="notice info">正在连接分享站并映射字段…</div>';
    try {
      type0Preview = await api('type0.preview', { baseUrl, code });
      area.innerHTML = renderType0Preview(type0Preview);
      submit.disabled = false;
    } catch (error) {
      type0Preview = null;
      area.innerHTML = `<div class="notice danger">${esc(error.message || error)}</div>`;
      submit.disabled = true;
    }
  });

  submit.addEventListener('click', async () => {
    if (source.value === 'manual') {
      const name = $('#manual-name').value.trim();
      if (!name) { toast('请填写角色名', 'error'); return; }
      const card = emptyCharacter();
      card.name = name;
      card.tags = parseTags($('#manual-tags').value);
      card.description = $('#manual-description').value.trim();
      card.personality = $('#manual-personality').value.trim();
      card.scenario = $('#manual-scenario').value.trim();
      card.hds.world = card.scenario;
      await createCharacter(card);
      return;
    }
    if (!type0Preview) { toast('请先连接并预览 Type0 预设', 'error'); return; }
    try {
      submit.disabled = true;
      submit.textContent = '导入中…';
      const result = await api('type0.import', {
        baseUrl: $('#type0-base').value.trim(),
        code: $('#type0-code').value.trim(),
        activate: true,
      });
      state.data = result.state;
      state.promptsDraft = { ...(result.state.prompts || {}) };
      state.character = result.character;
      closeModal();
      updateChrome();
      toast(`已导入「${result.character.name}」并设为当前角色`);
      navigate('characters', result.character.id);
    } catch (error) {
      toast(error.message || String(error), 'error');
      submit.disabled = false;
      submit.textContent = '导入并设为当前';
    }
  });
}

function renderType0Preview(preview, { compact = false } = {}) {
  const warnings = (preview.warnings || []).length
    ? `<div class="notice" style="margin:12px 0"><div><strong>需要注意</strong><ul>${preview.warnings.map((item) => `<li>${esc(item)}</li>`).join('')}</ul></div></div>`
    : '';
  const mapping = (preview.mapping || []).length
    ? `<table class="mapping-table"><thead><tr><th>Type0 字段</th><th>角色卡字段</th><th>值</th></tr></thead><tbody>
        ${preview.mapping.map((row) => `<tr><td>${esc(row.source)}</td><td>${esc(row.target)}</td><td>${esc(row.value || '（空）')}</td></tr>`).join('')}
      </tbody></table>`
    : '';
  return `
    <div class="card" style="padding:16px;box-shadow:none">
      <div class="row wrap" style="margin-bottom:10px">
        <span class="badge accent">Type0</span>
        <strong>${esc(preview.title)}</strong>
        <span class="muted" style="font-size:12px">${esc(preview.code)} · revision ${esc(preview.revision)}</span>
      </div>
      <div class="row wrap" style="gap:8px;margin-bottom:12px">
        <span class="badge">${esc(preview.summary.characterName || '未命名')}</span>
        <span class="badge">描述 ${esc(preview.summary.descriptionLength)} 字</span>
        <span class="badge">视角 ${esc(preview.summary.perspectives)}</span>
        <span class="badge">补充事实 ${esc(preview.summary.supplementaryFacts)}</span>
        <span class="badge">世界书 ${esc(preview.summary.lorebookEntries)} 条</span>
      </div>
      ${warnings}
      ${compact ? '' : mapping}
      ${compact ? `<button class="btn btn-primary" id="type0-import-inline">导入并设为当前</button>` : ''}
    </div>
  `;
}

function bindType0Import(root, preview, { baseUrl, code }) {
  const button = $('#type0-import-inline', root);
  if (!button) return;
  button.addEventListener('click', async () => {
    try {
      button.disabled = true;
      const result = await api('type0.import', { baseUrl, code, activate: true });
      state.data = result.state;
      state.character = result.character;
      updateChrome();
      toast(`已导入「${result.character.name}」`);
      navigate('characters', result.character.id);
    } catch (error) {
      button.disabled = false;
      toast(error.message || String(error), 'error');
    }
  });
}

async function createCharacter(card) {
  try {
    const result = await api('character.save', { character: card });
    state.data = result.state;
    state.character = result.character;
    closeModal();
    updateChrome();
    toast('角色已创建');
    navigate('characters', result.character.id);
  } catch (error) {
    toast(error.message || String(error), 'error');
  }
}

/* ------------------------------------------------------------------ */
/* 通用弹窗 / 抽屉                                                     */
/* ------------------------------------------------------------------ */

function bindModalClosers() {
  $$('[data-close="modal"]', modalRoot).forEach((node) => node.addEventListener('click', closeModal));
  $$('[data-close="drawer"]', drawerRoot).forEach((node) => node.addEventListener('click', closeDrawer));
}

function closeModal() {
  modalRoot.hidden = true;
  modalRoot.innerHTML = '';
}

function closeDrawer() {
  drawerRoot.hidden = true;
  drawerRoot.innerHTML = '';
}

function confirmDialog(title, message, confirmLabel = '确认') {
  return new Promise((resolve) => {
    modalRoot.hidden = false;
    modalRoot.innerHTML = `
      <div class="modal-backdrop" data-close="modal"></div>
      <div class="modal" style="width:min(460px,calc(100vw - 32px))" role="alertdialog" aria-modal="true">
        <div class="modal-head"><h2>${esc(title)}</h2><button class="icon-btn" data-close="modal">✕</button></div>
        <div class="modal-body"><p style="margin:0;color:var(--text-2)">${esc(message)}</p></div>
        <div class="modal-foot">
          <button class="btn" data-close="modal">取消</button>
          <button class="btn btn-danger" id="confirm-yes">${esc(confirmLabel)}</button>
        </div>
      </div>
    `;
    const finish = (value) => { closeModal(); resolve(value); };
    $$('[data-close="modal"]', modalRoot).forEach((node) => node.addEventListener('click', () => finish(false)));
    $('#confirm-yes').addEventListener('click', () => finish(true));
  });
}

/* ------------------------------------------------------------------ */
/* 下载                                                                */
/* ------------------------------------------------------------------ */

function download(filename, content, mime = 'application/json') {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename || 'download.json';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function downloadBase64(filename, base64, mime) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  const blob = new Blob([bytes], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename || 'download';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ------------------------------------------------------------------ */
/* 事件委托                                                            */
/* ------------------------------------------------------------------ */

view.addEventListener('click', async (event) => {
  const target = event.target.closest('[data-action]');
  if (!target) return;
  const action = target.dataset.action;
  const id = target.dataset.id || '';

  if (action === 'open-character') { state.character = null; navigate('characters', id); return; }
  if (action === 'open-lorebook') { state.book = null; navigate('lorebooks', id); return; }
  if (action === 'goto-characters') { navigate('characters'); return; }
  if (action === 'goto-lorebooks') { navigate('lorebooks'); return; }
  if (action === 'goto-import') { navigate('import'); return; }
  if (action === 'goto-binding') { navigate('binding'); return; }
  if (action === 'new-character') { openNewCharacterModal(); return; }
  if (action === 'new-lorebook') { state.book = emptyLorebook(); navigate('lorebooks', 'new'); return; }
  if (action === 'filter-characters') { state.charFilter = target.dataset.filter; renderCharacters(); return; }
  if (action === 'char-tab') { state.charTab = target.dataset.tab; renderCharacterEditor(state.character?.id || ''); return; }

  if (action === 'save-character') { await saveCharacter(); return; }
  if (action === 'save-lorebook') { await saveLorebook(); return; }
  if (action === 'save-prompts') { await savePrompts(); return; }

  if (action === 'activate-character') {
    try {
      const result = await api('character.activate', { characterId: id, lorebookIds: state.data.stats.activeLorebookIds || [] });
      state.data = result.state;
      updateChrome();
      toast('已设为当前角色');
      render();
    } catch (error) { toast(error.message || String(error), 'error'); }
    return;
  }

  if (action === 'delete-character') {
    const card = state.data.characters.find((item) => item.id === id);
    const ok = await confirmDialog('删除角色', `确定删除「${card?.name || '该角色'}」？此操作不可撤销。`, '删除');
    if (!ok) return;
    try {
      const result = await api('character.delete', { id });
      state.data = result.state;
      if (state.character?.id === id) state.character = null;
      updateChrome();
      toast('角色已删除');
      navigate('characters');
    } catch (error) { toast(error.message || String(error), 'error'); }
    return;
  }

  if (action === 'duplicate-character') {
    try {
      const result = await api('character.duplicate', { id });
      state.data = result.state;
      updateChrome();
      toast('已复制角色');
      navigate('characters', result.character.id);
    } catch (error) { toast(error.message || String(error), 'error'); }
    return;
  }

  if (action === 'export-character') {
    try {
      const result = await api('export.character', { id });
      download(result.filename, result.content, 'application/json');
    } catch (error) { toast(error.message || String(error), 'error'); }
    return;
  }

  if (action === 'export-character-png') {
    try {
      const result = await api('export.character.png', { id });
      downloadBase64(result.filename, result.contentBase64, 'image/png');
    } catch (error) { toast(error.message || String(error), 'error'); }
    return;
  }

  if (action === 'delete-lorebook') {
    const book = state.data.lorebooks.find((item) => item.id === id);
    const ok = await confirmDialog('删除世界书', `确定删除「${book?.name || '该世界书'}」？绑定关系会一并解除。`, '删除');
    if (!ok) return;
    try {
      const result = await api('lorebook.delete', { id });
      state.data = result.state;
      updateChrome();
      toast('世界书已删除');
      navigate('lorebooks');
    } catch (error) { toast(error.message || String(error), 'error'); }
    return;
  }

  if (action === 'duplicate-lorebook') {
    try {
      const result = await api('lorebook.duplicate', { id });
      state.data = result.state;
      updateChrome();
      toast('已复制世界书');
      navigate('lorebooks', result.lorebook.id);
    } catch (error) { toast(error.message || String(error), 'error'); }
    return;
  }

  if (action === 'export-lorebook') {
    try {
      const result = await api('export.lorebook', { id });
      download(result.filename, result.content, 'application/json');
    } catch (error) { toast(error.message || String(error), 'error'); }
    return;
  }

  if (action === 'add-entry') { openEntryDrawer(); return; }
  if (action === 'edit-entry') { openEntryDrawer(id); return; }
  if (action === 'delete-entry') {
    const entry = state.book?.entries?.find((item) => item.id === id);
    const ok = await confirmDialog('删除条目', `确定删除「${entry?.comment || '该条目'}」？保存世界书后生效。`, '删除');
    if (!ok) return;
    state.book.entries = state.book.entries.filter((item) => item.id !== id);
    state.bookDirty = true;
    setSaveState('dirty', '未保存');
    renderLorebookEditor(state.book.id || '');
    return;
  }
  if (action === 'toggle-entry') {
    const entry = state.book?.entries?.find((item) => item.id === id);
    if (!entry) return;
    entry.enabled = target.checked;
    state.bookDirty = true;
    setSaveState('dirty', '未保存');
    return;
  }

  if (action === 'save-active') {
    const characterId = $('#active-character').value;
    const lorebookIds = $$('#active-books input:checked').map((node) => node.value);
    try {
      const result = await api('character.activate', { characterId, lorebookIds });
      state.data = result.state;
      updateChrome();
      toast('默认绑定已保存');
      renderBinding();
    } catch (error) { toast(error.message || String(error), 'error'); }
    return;
  }

  if (action === 'bind-story') {
    const select = $(`[data-story-character="${id}"]`);
    const characterId = select ? select.value : '';
    try {
      const result = await api('binding.save', { storyId: id, characterId, lorebookIds: state.data.stats.activeLorebookIds || [] });
      state.data = result.state;
      updateChrome();
      toast('已绑定');
    } catch (error) { toast(error.message || String(error), 'error'); }
    return;
  }

  if (action === 'apply-story') {
    const select = $(`[data-story-character="${id}"]`);
    const characterId = select ? select.value : '';
    const ok = await confirmDialog('应用到故事', '会把角色卡中的角色名、描述、世界、视角等写入该故事设定（覆盖同名项）。', '应用');
    if (!ok) return;
    try {
      const result = await api('story.apply', {
        storyId: id,
        characterId: characterId || state.data.stats.activeCharacterId,
        lorebookIds: state.data.stats.activeLorebookIds || [],
      });
      state.data = result.state;
      updateChrome();
      toast('已写入故事设定');
    } catch (error) { toast(error.message || String(error), 'error'); }
    return;
  }

  if (action === 'reset-prompts') {
    const ok = await confirmDialog('恢复默认', '清空 WebUI 中保存的全部提示词覆盖，回退到插件配置或内置默认。', '清空');
    if (!ok) return;
    state.promptsDraft = {};
    try {
      const result = await api('prompts.save', { prompts: {} });
      state.data = result.state;
      state.promptsDirty = false;
      toast('已恢复默认');
      renderPrompts();
    } catch (error) { toast(error.message || String(error), 'error'); }
    return;
  }

  if (action === 'type0-preview') { await previewType0FromImportPage(); return; }
});

view.addEventListener('change', (event) => {
  const target = event.target.closest('[data-action="toggle-entry"]');
  if (!target || !state.book) return;
  const entry = state.book.entries.find((item) => item.id === target.dataset.id);
  if (!entry) return;
  entry.enabled = target.checked;
  state.bookDirty = true;
  setSaveState('dirty', '未保存');
});

view.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && event.target.matches('.char-card')) {
    event.target.click();
  }
});

/* ------------------------------------------------------------------ */
/* 导航 / 快捷键 / 启动                                                */
/* ------------------------------------------------------------------ */

$$('.nav-item').forEach((item) => {
  item.addEventListener('click', () => {
    $('#sidebar').classList.remove('open');
    navigate(item.dataset.nav);
  });
});

$('#btn-menu')?.addEventListener('click', () => $('#sidebar').classList.toggle('open'));
$('#btn-new-character')?.addEventListener('click', openNewCharacterModal);

$('#global-search')?.addEventListener('input', (event) => {
  state.search = event.target.value;
  if (!['characters', 'lorebooks'].includes(state.route.name)) {
    navigate('characters');
  } else {
    render();
  }
});

document.addEventListener('keydown', async (event) => {
  const mod = event.metaKey || event.ctrlKey;
  if (mod && event.key.toLowerCase() === 's') {
    event.preventDefault();
    if (state.route.name === 'characters' && state.character) await saveCharacter();
    else if (state.route.name === 'lorebooks' && state.book) await saveLorebook();
    else if (state.route.name === 'prompts') await savePrompts();
  }
  if (mod && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    $('#global-search')?.focus();
  }
  if (event.key === 'Escape') { closeModal(); closeDrawer(); }
});

window.addEventListener('hashchange', async () => {
  await autoSave();
  state.character = null;
  state.book = null;
  render();
});

async function autoSave() {
  if (state.characterDirty && state.character?.id) await saveCharacter({ silent: true });
  if (state.bookDirty && state.book?.id) await saveLorebook({ silent: true });
  if (state.promptsDirty) await savePrompts({ silent: true });
}

async function boot() {
  if (!bridge) {
    view.innerHTML = `<div class="empty"><h3>请从 AstrBot WebUI 打开</h3><p>本页面依赖 AstrBot 插件页面桥接，直接打开 HTML 文件无法读取数据。</p></div>`;
    return;
  }
  try {
    const context = await bridge.ready();
    applyTheme(Boolean(context?.isDark));
    if (typeof bridge.onContext === 'function') {
      bridge.onContext((next) => applyTheme(Boolean(next?.isDark)));
    }
  } catch (error) {
    console.warn('bridge ready failed', error);
  }
  try {
    await loadState();
    await render();
  } catch (error) {
    view.innerHTML = `<div class="empty"><div class="empty-icon">!</div><h3>无法加载内容库</h3><p>${esc(error.message || error)}</p><button class="btn" onclick="location.reload()">重试</button></div>`;
  }
}

function applyTheme(isDark) {
  document.documentElement.dataset.theme = isDark ? 'dark' : 'light';
  document.documentElement.style.colorScheme = isDark ? 'dark' : 'light';
}

window.addEventListener('beforeunload', (event) => {
  if (state.characterDirty || state.bookDirty || state.promptsDirty) {
    event.preventDefault();
    event.returnValue = '';
  }
});

boot();
