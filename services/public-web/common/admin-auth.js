// 管理页共用的鉴权与请求封装。
// 抽取前 /exports/ 与 /tplus-sync/ 各自内联了逐字相同的一份；新增 /sync/ 时会变成第三份，故收敛到此。
// applyGate 是两页唯一有差异的函数（管理员分支加载的东西不同），用 onAdmin 回调收敛。
(function (global) {
  'use strict';

  const API_BASE = location.port === '8080' ? 'http://localhost:8000' : '/api';
  const AUTH_KEYS = ['aliecs_auth_token', 'portal_token', 'admin_token'];

  const token = () => AUTH_KEYS.map((key) => localStorage.getItem(key) || '').find(Boolean) || '';
  const clearAuthToken = () => AUTH_KEYS.forEach((key) => localStorage.removeItem(key));
  const fmtTime = (value) => (value ? new Date(value).toLocaleString() : '-');
  const chip = (status) => `<span class="chip ${status || 'degraded'}">${status || 'unknown'}</span>`;
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[char]));

  function authHeaders() {
    const headers = { 'Content-Type': 'application/json' };
    const value = token();
    if (value) headers.Authorization = `Bearer ${value}`;
    return headers;
  }

  async function api(path, opt = {}) {
    const response = await fetch(`${API_BASE}${path}`, {
      ...opt,
      headers: Object.assign(authHeaders(), opt.headers || {}),
    });
    const text = await response.text();
    let data = {};
    if (text) { try { data = JSON.parse(text); } catch { data = { raw: text }; } }
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    return data;
  }

  async function fetchMe() {
    if (!token()) return null;
    try { return await api('/v1/auth/me'); } catch { return null; }
  }

  const isAdminUser = (me) => !!me
    && (((me.roles || []).includes('admin')) || ((me.permissions || []).includes('admin.access')));

  const ssoLogin = () => {
    location.href = `${API_BASE}/v1/auth/oidc/login?rd=${encodeURIComponent(location.pathname + location.search)}`;
  };

  // 管理员闸门。DOM id 契约：loginBtn / logoutBtn / adminContent / gateHint / refreshBtn(可选)。
  function applyGate(me, onAdmin) {
    const $ = (id) => document.getElementById(id);
    const admin = isAdminUser(me);
    $('loginBtn').classList.toggle('hidden', admin);
    $('logoutBtn').classList.toggle('hidden', !token());
    $('adminContent').classList.toggle('hidden', !admin);
    $('gateHint').classList.toggle('hidden', admin);
    const refresh = $('refreshBtn');
    if (refresh) refresh.classList.toggle('hidden', !admin);
    if (admin && typeof onAdmin === 'function') onAdmin();
  }

  async function downloadExport(url, name) {
    const response = await fetch(`${API_BASE}${url}`, { headers: authHeaders() });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try { detail = (await response.json()).detail || detail; } catch { /* 保持默认 detail */ }
      throw new Error(detail);
    }
    const blob = await response.blob();
    const disposition = response.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename\*=UTF-8''([^;]+)/);
    const fileName = match
      ? decodeURIComponent(match[1])
      : (name && name.endsWith('.xlsx') ? name : `${name || 'export'}.xlsx`);
    const anchor = document.createElement('a');
    anchor.href = URL.createObjectURL(blob);
    anchor.download = fileName;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(anchor.href);
  }

  global.AliECSAdmin = {
    API_BASE, token, clearAuthToken, authHeaders, api, fetchMe,
    isAdminUser, applyGate, downloadExport, ssoLogin, esc, fmtTime, chip,
  };
})(window);
