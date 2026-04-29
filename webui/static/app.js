const DEFAULT_FORM = {
  name: 'sub_bot',
  enabled: false,
  server_url: '',
  username: '',
  password: '',
  e2ee_password: '',
  onebot_ws_url: 'ws://127.0.0.1:6200/ws/',
  onebot_access_token: '',
  onebot_self_id: 910001,
  reconnect_delay: 5.0,
  max_reconnect_attempts: 10,
  enable_subchannel_session_isolation: true,
  remote_media_max_size: 20971520,
  skip_own_messages: true,
  debug: false,
};

const state = {
  editingId: null,
  bots: [],
  status: null,
  currentPage: 'network',
  basicInfo: {
    items: [],
    summary: {
      enabled_count: 0,
      online_count: 0,
    },
    loaded: false,
  },
  logs: {
    items: [],
    lastId: 0,
    maxEntries: 5000,
    pollTimer: null,
    generation: 0,
    autoScroll: true,
    activeLevels: new Set(['DEBUG', 'INFO', 'WARN', 'ERROR']),
  },
};

function getSuggestedOnebotSelfId() {
  const suggested = Number(state.status?.suggested_onebot_self_id);
  return Number.isFinite(suggested) && suggested > 0
    ? suggested
    : DEFAULT_FORM.onebot_self_id;
}

function buildCreateDefaults() {
  return {
    ...DEFAULT_FORM,
    onebot_self_id: getSuggestedOnebotSelfId(),
  };
}

const elements = {
  navButtons: Array.from(document.querySelectorAll('[data-page]')),
  networkPage: document.getElementById('networkPage'),
  basicPage: document.getElementById('basicPage'),
  logsPage: document.getElementById('logsPage'),
  bridgeStatus: document.getElementById('bridgeStatus'),
  mainBotStatus: document.getElementById('mainBotStatus'),
  webuiStatus: document.getElementById('webuiStatus'),
  webuiUrl: document.getElementById('webuiUrl'),
  basicInfoGrid: document.getElementById('basicInfoGrid'),
  basicEmptyState: document.getElementById('basicEmptyState'),
  basicEnabledCount: document.getElementById('basicEnabledCount'),
  basicOnlineCount: document.getElementById('basicOnlineCount'),
  banner: document.getElementById('statusBanner'),
  botGrid: document.getElementById('botGrid'),
  emptyState: document.getElementById('emptyState'),
  createButton: document.getElementById('createButton'),
  refreshButton: document.getElementById('refreshButton'),
  basicRefreshButton: document.getElementById('basicRefreshButton'),
  modal: document.getElementById('botModal'),
  modalTitle: document.getElementById('modalTitle'),
  form: document.getElementById('botForm'),
  closeModalButton: document.getElementById('closeModalButton'),
  cancelButton: document.getElementById('cancelButton'),
  submitButton: document.getElementById('submitButton'),
  toast: document.getElementById('toast'),
  logConsole: document.getElementById('logConsole'),
  logAutoScrollToggle: document.getElementById('logAutoScrollToggle'),
  logAutoScrollLabel: document.getElementById('logAutoScrollLabel'),
  logMeta: document.getElementById('logMeta'),
  clearLogsButton: document.getElementById('clearLogsButton'),
  logFilterButtons: Array.from(document.querySelectorAll('[data-log-level]')),
};

function showToast(message, kind = 'default') {
  elements.toast.textContent = message;
  elements.toast.className = `toast ${kind}`;
  window.clearTimeout(showToast._timer);
  showToast._timer = window.setTimeout(() => {
    elements.toast.className = 'toast hidden';
  }, 2600);
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401 && !options.skipAuthRedirect) {
    window.location.replace('/');
    throw new Error(payload.error || payload.detail || '登录已失效，请重新登录');
  }
  if (!response.ok) {
    throw new Error(payload.error || payload.detail || '请求失败');
  }
  return payload;
}

function setActivePage(page) {
  state.currentPage = page;
  elements.networkPage.classList.toggle('hidden', page !== 'network');
  elements.basicPage.classList.toggle('hidden', page !== 'basic');
  elements.logsPage.classList.toggle('hidden', page !== 'logs');

  for (const button of elements.navButtons) {
    const isActive = button.dataset.page === page;
    button.classList.toggle('active', isActive);
    button.classList.toggle('ghost', !isActive);
  }

  if (page === 'logs') {
    renderLogs();
  }
}

function buildBasicInfoFallback() {
  const items = [];
  const bridgeEnabled = Boolean(state.status?.bridge_enabled);
  const webuiEnabled = Boolean(state.status?.independent_webui_enabled);
  const blocked = !bridgeEnabled || !webuiEnabled;
  const blockedLabel = !bridgeEnabled ? '受总开关禁用' : '等待基础信息接口';

  if (state.status?.main_bot_enabled) {
    items.push({
      bot_id: 'main',
      client_name: '主bot',
      login_username: '-',
      nickname: '-',
      avatar_url: '',
      status_code: blocked ? 'blocked' : 'pending',
      status_label: blocked ? blockedLabel : '等待基础信息接口',
      server_url: '-',
      onebot_self_id: state.status?.main_bot_onebot_self_id || '-',
      server_display_name: '',
      server_avatar_url: '',
      is_main_bot: true,
      user_id: '',
    });
  }

  for (const bot of state.bots.filter((item) => item.enabled)) {
    items.push({
      bot_id: bot.id,
      client_name: bot.name || '未命名副bot',
      login_username: bot.username || '-',
      nickname: bot.username || '-',
      avatar_url: '',
      status_code: blocked ? 'blocked' : 'pending',
      status_label: blocked ? blockedLabel : '等待基础信息接口',
      server_url: bot.server_url || '-',
      onebot_self_id: bot.onebot_self_id || '-',
      server_display_name: '',
      server_avatar_url: '',
      is_main_bot: false,
      user_id: '',
    });
  }

  const onlineCount = items.filter((item) => item.status_code === 'online').length;
  return {
    items,
    summary: {
      enabled_count: items.length,
      online_count: onlineCount,
    },
  };
}

async function activatePage(page, { forceReload = false } = {}) {
  setActivePage(page);
  if (page === 'basic') {
    await loadBasicInfo({ forceReload, silent: false });
  }
}

function getBasicStatusTone(statusCode) {
  if (statusCode === 'online') {
    return 'online';
  }
  if (statusCode === 'blocked') {
    return 'blocked';
  }
  return 'pending';
}

function getAvatarInitial(item) {
  const source = String(item.nickname || item.login_username || item.client_name || '?').trim();
  return escapeHtml(source.charAt(0) || '?');
}

function setBanner(message = '', tone = 'warning') {
  if (!message) {
    elements.banner.className = 'banner hidden';
    elements.banner.textContent = '';
    return;
  }
  elements.banner.className = `banner ${tone}`;
  elements.banner.textContent = message;
}

function renderStatus(status) {
  state.status = status;
  elements.bridgeStatus.textContent = status.bridge_enabled ? '已开启' : '已关闭';
  elements.mainBotStatus.textContent = status.main_bot_enabled ? '主bot可用' : '主bot停用';
  elements.webuiStatus.textContent = status.independent_webui_enabled ? '独立 WebUI 已启用' : '独立 WebUI 未启用';
  elements.webuiUrl.textContent = status.access_url || '-';

  if (!status.bridge_enabled) {
    setBanner('当前“启用桥接总开关”已关闭，主bot与所有副bot都不会建立连接。');
    return;
  }
  if (!status.independent_webui_enabled) {
    setBanner('独立 WebUI 当前未启用。只有开启独立 WebUI，副bot 才会真正接入运行。');
    return;
  }
  setBanner('');
}

function renderBasicInfo(payload) {
  const summary = payload?.summary || {};
  const items = Array.isArray(payload?.items) ? payload.items : [];
  state.basicInfo = {
    items,
    summary,
    loaded: state.basicInfo.loaded,
  };

  elements.basicEnabledCount.textContent = String(summary.enabled_count || 0);
  elements.basicOnlineCount.textContent = String(summary.online_count || 0);
  elements.basicEmptyState.classList.toggle('hidden', items.length > 0);
  elements.basicInfoGrid.innerHTML = '';

  for (const item of items) {
    const card = document.createElement('article');
    const statusTone = getBasicStatusTone(item.status_code);
    const serverDisplayName = item.server_display_name || '';
    const serverAvatarUrl = item.server_avatar_url || '';
    card.className = 'basic-info-card';
    card.innerHTML = `
      <div class="basic-info-card-header">
        <div class="basic-avatar-shell">
          <span class="basic-avatar-fallback">${getAvatarInitial(item)}</span>
          ${item.avatar_url ? `<img class="basic-avatar-image" src="${escapeHtml(item.avatar_url)}" alt="${escapeHtml(item.nickname || item.client_name || 'avatar')}" onerror="this.remove()" />` : ''}
        </div>
        <div class="basic-identity-block">
          <div class="basic-identity-top">
            <div>
              <h3>${escapeHtml(item.client_name || '未命名客户端')}</h3>
              <p class="basic-login-name">@${escapeHtml(item.login_username || '-')}</p>
            </div>
            <span class="basic-status-pill ${statusTone}">${escapeHtml(item.status_label || '未接入')}</span>
          </div>
          <p class="basic-display-name">${escapeHtml(item.nickname || item.login_username || '-')}</p>
        </div>
      </div>

      <div class="basic-meta-list">
        <div class="basic-meta-row">
          <span>聊天显示昵称</span>
          <strong>${escapeHtml(item.nickname || '-')}</strong>
        </div>
        <div class="basic-meta-row">
          <span>Rocket.Chat 用户名</span>
          <strong>${escapeHtml(item.login_username || '-')}</strong>
        </div>
        <div class="basic-meta-row">
          <span>OneBot self_id</span>
          <strong>${escapeHtml(String(item.onebot_self_id || '-'))}</strong>
        </div>
        <div class="basic-meta-row wide">
          <span>Rocket.Chat 服务器</span>
          <div class="basic-server-value">
            <code>${escapeHtml(item.server_url || '-')}</code>
          </div>
        </div>
        <div class="basic-meta-row wide basic-target-row">
          <div class="basic-target-summary">
            <div class="basic-room-avatar-shell" title="${escapeHtml(serverDisplayName || '未获取到服务器昵称')}">
              ${serverAvatarUrl ? `<img class="basic-room-avatar-image" src="${escapeHtml(serverAvatarUrl)}" alt="${escapeHtml(serverDisplayName || '服务器标识')}" onerror="this.closest('.basic-target-summary').dataset.avatarMissing = 'true'; this.parentElement.classList.add('is-missing'); this.remove();" />` : ''}
              <span class="basic-room-avatar-fallback ${serverAvatarUrl ? 'hidden' : ''}">${escapeHtml((serverDisplayName || '?').trim().charAt(0) || '?')}</span>
            </div>
            <div class="basic-target-texts">
              <strong class="basic-target-name">${escapeHtml(serverDisplayName || '未获取到服务器昵称')}</strong>
            </div>
          </div>
        </div>
      </div>
    `;
    elements.basicInfoGrid.appendChild(card);
  }
}

function isLogConsoleNearBottom() {
  if (!elements.logConsole) {
    return true;
  }
  const distance = elements.logConsole.scrollHeight - elements.logConsole.scrollTop - elements.logConsole.clientHeight;
  return distance < 48;
}

function renderLogAutoScrollState() {
  if (elements.logAutoScrollToggle) {
    elements.logAutoScrollToggle.checked = state.logs.autoScroll;
  }
  if (elements.logAutoScrollLabel) {
    elements.logAutoScrollLabel.textContent = state.logs.autoScroll ? '自动滚动已开启' : '自动滚动已关闭';
  }
}

function renderLogs({ scrollToBottom = false } = {}) {
  const activeLevels = state.logs.activeLevels;
  const visibleItems = state.logs.items.filter((item) => activeLevels.has(item.level));

  if (!visibleItems.length) {
    elements.logConsole.innerHTML = '<div class="log-empty">暂时还没有桥接器实时日志。</div>';
  } else {
    elements.logConsole.innerHTML = visibleItems
      .map((item) => `
        <div class="log-entry log-${item.level.toLowerCase()}">
          <span class="log-entry-level">${escapeHtml(item.level)}</span>
          <span class="log-entry-line">${escapeHtml(item.line)}</span>
        </div>
      `)
      .join('');
  }

  elements.logMeta.textContent = `实时日志 · 缓存 ${state.logs.items.length}/${state.logs.maxEntries} 条`;

  for (const button of elements.logFilterButtons) {
    const level = button.dataset.logLevel;
    button.classList.toggle('active', state.logs.activeLevels.has(level));
  }

  renderLogAutoScrollState();

  if (scrollToBottom && state.logs.autoScroll) {
    elements.logConsole.scrollTop = elements.logConsole.scrollHeight;
  }
}

async function loadLogs({ reset = false } = {}) {
  const afterId = reset ? 0 : state.logs.lastId;
  const requestGeneration = state.logs.generation;
  const payload = await requestJson(`/api/logs?after_id=${afterId}`);

  if (requestGeneration !== state.logs.generation) {
    return;
  }

  if (reset) {
    state.logs.items = [];
    state.logs.lastId = 0;
  }

  const incoming = Array.isArray(payload.items) ? payload.items : [];
  if (incoming.length) {
    state.logs.items.push(...incoming);
    const maxEntries = Number(payload.max_entries) || state.logs.maxEntries;
    state.logs.maxEntries = maxEntries;
    if (state.logs.items.length > maxEntries) {
      state.logs.items = state.logs.items.slice(-maxEntries);
    }
    state.logs.lastId = Number(incoming[incoming.length - 1].id) || state.logs.lastId;
  }

  renderLogs({ scrollToBottom: incoming.length > 0 });
}

function setLogAutoScroll(enabled) {
  state.logs.autoScroll = Boolean(enabled);
  renderLogAutoScrollState();
  if (state.logs.autoScroll && elements.logConsole) {
    elements.logConsole.scrollTop = elements.logConsole.scrollHeight;
  }
}

async function clearLogs() {
  const confirmed = window.confirm('确认清空当前实时日志吗？这会同时重置服务端缓存和当前页面日志视图。');
  if (!confirmed) {
    return;
  }

  const payload = await requestJson('/api/logs/clear', {
    method: 'POST',
  });

  state.logs.generation += 1;
  state.logs.items = [];
  state.logs.lastId = 0;
  state.logs.maxEntries = Number(payload.max_entries) || state.logs.maxEntries;
  renderLogs();
  showToast(`已清空 ${Number(payload.cleared) || 0} 条日志`, 'success');
}

function startLogPolling() {
  if (state.logs.pollTimer) {
    return;
  }

  const poll = async () => {
    try {
      await loadLogs();
    } catch (error) {
      console.error('log polling failed', error);
    } finally {
      state.logs.pollTimer = window.setTimeout(poll, 1000);
    }
  };

  state.logs.pollTimer = window.setTimeout(poll, 1000);
}

function effectiveStatusLabel(bot) {
  if (!state.status?.bridge_enabled) {
    return '受总开关禁用';
  }
  if (!state.status?.independent_webui_enabled) {
    return '受独立WebUI开关禁用';
  }
  return bot.enabled ? '运行中' : '已停用';
}

function renderBots(items) {
  state.bots = items;
  elements.botGrid.innerHTML = '';
  elements.emptyState.classList.toggle('hidden', items.length > 0);

  for (const bot of items) {
    const card = document.createElement('article');
    card.className = 'bot-card';
    card.innerHTML = `
      <div class="bot-card-header">
        <div>
          <span class="card-chip">${escapeHtml(bot.name || '未命名副bot')}</span>
          <p class="card-type">Websocket客户端</p>
        </div>
        <label class="field-switch compact-switch">
          <input type="checkbox" ${bot.enabled ? 'checked' : ''} data-role="toggle" data-id="${bot.id}" />
          <i></i>
        </label>
      </div>

      <div class="card-body">
        <div class="card-line">
          <span>状态</span>
          <strong>${escapeHtml(effectiveStatusLabel(bot))}</strong>
        </div>
        <div class="card-line">
          <span>Rocket.Chat</span>
          <code>${escapeHtml(bot.server_url || '-')}</code>
        </div>
        <div class="card-line">
          <span>WS URL</span>
          <code>${escapeHtml(bot.onebot_ws_url || '-')}</code>
        </div>
        <div class="card-line">
          <span>用户名</span>
          <strong>${escapeHtml(bot.username || '-')}</strong>
        </div>
        <div class="card-line">
          <span>self_id</span>
          <strong>${escapeHtml(String(bot.onebot_self_id || '-'))}</strong>
        </div>
      </div>

      <div class="card-actions">
        <button class="action-chip" type="button" data-role="edit" data-id="${bot.id}">编辑</button>
        <button class="action-chip danger" type="button" data-role="delete" data-id="${bot.id}">删除</button>
      </div>
    `;
    elements.botGrid.appendChild(card);
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function setFormData(data) {
  const merged = { ...DEFAULT_FORM, ...data };
  for (const [key, value] of Object.entries(merged)) {
    const field = elements.form.elements.namedItem(key);
    if (!field) {
      continue;
    }
    if (field.type === 'checkbox') {
      field.checked = Boolean(value);
    } else {
      field.value = value ?? '';
    }
  }
}

function collectFormData() {
  const payload = {};
  for (const [key, defaultValue] of Object.entries(DEFAULT_FORM)) {
    const field = elements.form.elements.namedItem(key);
    if (!field) {
      continue;
    }

    if (field.type === 'checkbox') {
      payload[key] = field.checked;
      continue;
    }

    const rawValue = field.value;
    if (typeof defaultValue === 'number') {
      payload[key] = rawValue === '' ? defaultValue : Number(rawValue);
      continue;
    }
    payload[key] = rawValue;
  }
  return payload;
}

function openModal(bot = null) {
  state.editingId = bot?.id || null;
  elements.modalTitle.textContent = bot ? `编辑副bot：${bot.name}` : '新建副bot';
  setFormData(bot || buildCreateDefaults());
  elements.modal.classList.remove('hidden');
}

function closeModal() {
  state.editingId = null;
  elements.modal.classList.add('hidden');
}

async function loadData() {
  const [status, bots] = await Promise.all([
    requestJson('/api/status'),
    requestJson('/api/bots'),
  ]);
  renderStatus(status);
  renderBots(bots.items || []);

  if (state.currentPage === 'basic') {
    await loadBasicInfo({ forceReload: true, silent: true });
  }
}

async function loadBasicInfo({ forceReload = false, silent = false } = {}) {
  if (!forceReload && state.basicInfo.loaded) {
    return;
  }

  try {
    const basicInfo = await requestJson('/api/basic-info');
    state.basicInfo.loaded = true;
    renderBasicInfo(basicInfo);
  } catch (error) {
    state.basicInfo.loaded = false;
    renderBasicInfo(buildBasicInfoFallback());
    if (!silent) {
      showToast('基础信息接口暂不可用，已显示回退信息；如刚更新插件，请重启 RocketCat Shell。', 'error');
    }
  }
}

async function saveBot() {
  const payload = collectFormData();
  const isEditing = Boolean(state.editingId);
  const endpoint = state.editingId ? `/api/bots/${state.editingId}` : '/api/bots';
  const method = state.editingId ? 'PUT' : 'POST';

  await requestJson(endpoint, {
    method,
    body: JSON.stringify(payload),
  });
  closeModal();
  showToast(isEditing ? '副bot 已更新' : '副bot 已创建', 'success');
  await loadData();
}

async function toggleBot(botId, enabled) {
  const target = state.bots.find((bot) => bot.id === botId);
  if (!target) {
    return;
  }
  await requestJson(`/api/bots/${botId}`, {
    method: 'PUT',
    body: JSON.stringify({ ...target, enabled }),
  });
  showToast(enabled ? '副bot 已启用' : '副bot 已停用', 'success');
  await loadData();
}

async function deleteBot(botId) {
  const target = state.bots.find((bot) => bot.id === botId);
  if (!target) {
    return;
  }
  const confirmed = window.confirm(`确认删除副bot「${target.name}」吗？`);
  if (!confirmed) {
    return;
  }
  await requestJson(`/api/bots/${botId}`, { method: 'DELETE' });
  showToast('副bot 已删除', 'success');
  await loadData();
}

elements.createButton?.addEventListener('click', () => openModal());
elements.refreshButton?.addEventListener('click', async () => {
  await loadData();
  showToast('列表已刷新');
});
elements.clearLogsButton?.addEventListener('click', async () => {
  try {
    await clearLogs();
  } catch (error) {
    showToast(error.message || '清空日志失败', 'error');
  }
});
elements.logAutoScrollToggle?.addEventListener('change', (event) => {
  setLogAutoScroll(event.target.checked);
});
elements.basicRefreshButton?.addEventListener('click', async () => {
  await activatePage('basic', { forceReload: true });
  showToast('基础信息已刷新');
});
for (const button of elements.navButtons) {
  button.addEventListener('click', async () => {
    try {
      await activatePage(button.dataset.page);
    } catch (error) {
      showToast(error.message || '页面切换失败', 'error');
    }
  });
}
elements.closeModalButton?.addEventListener('click', closeModal);
elements.cancelButton?.addEventListener('click', closeModal);
elements.submitButton?.addEventListener('click', async () => {
  try {
    await saveBot();
  } catch (error) {
    showToast(error.message || '保存失败', 'error');
  }
});

elements.modal?.addEventListener('click', (event) => {
  if (event.target === elements.modal) {
    closeModal();
  }
});

elements.botGrid?.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-role]');
  if (!button) {
    return;
  }
  const { id, role } = button.dataset;
  if (role === 'edit') {
    const target = state.bots.find((bot) => bot.id === id);
    if (target) {
      openModal(target);
    }
    return;
  }
  if (role === 'delete') {
    try {
      await deleteBot(id);
    } catch (error) {
      showToast(error.message || '删除失败', 'error');
    }
  }
});

elements.botGrid?.addEventListener('change', async (event) => {
  const input = event.target.closest('[data-role="toggle"]');
  if (!input) {
    return;
  }
  try {
    await toggleBot(input.dataset.id, input.checked);
  } catch (error) {
    input.checked = !input.checked;
    showToast(error.message || '切换失败', 'error');
  }
});

for (const button of elements.logFilterButtons) {
  button.addEventListener('click', () => {
    const level = button.dataset.logLevel;
    if (state.logs.activeLevels.has(level)) {
      state.logs.activeLevels.delete(level);
    } else {
      state.logs.activeLevels.add(level);
    }
    renderLogs();
  });
}

window.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    closeModal();
  }
});

Promise.all([
  loadData(),
  loadLogs({ reset: true }),
])
  .then(() => {
    setActivePage('network');
    startLogPolling();
  })
  .catch((error) => {
    showToast(error.message || '加载失败', 'error');
  });