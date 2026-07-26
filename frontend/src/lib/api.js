import axios from 'axios';

export const AUTH_ERROR_EVENT = 'nocturne:auth-error';

export const api = axios.create({
  baseURL: '/api'
});

// 请求拦截器：自动附加 Bearer Token 和 X-Namespace
api.interceptors.request.use((config) => {
  config.headers = config.headers ?? {};
  const token = localStorage.getItem('api_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  if (!('X-Namespace' in config.headers)) {
    const ns = localStorage.getItem('selected_namespace');
    if (ns && !config.url.startsWith('/review')) {
      config.headers['X-Namespace'] = ns;
    }
  }
  return config;
});

// 响应拦截器：401 时清除 token 并触发重新认证
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response && error.response.status === 401) {
      localStorage.removeItem('api_token');
      window.dispatchEvent(new CustomEvent(AUTH_ERROR_EVENT));
    }
    return Promise.reject(error);
  }
);

const encodeId = (id) => encodeURIComponent(id);

// ============ Review API ============

export const getGroups = () =>
  api.get('/review/groups').then(res => res.data);

export const getGroupDiff = (nodeUuid) =>
  api.get(`/review/groups/${encodeId(nodeUuid)}/diff`).then(res => res.data);

export const rollbackGroup = (nodeUuid) =>
  api.post(`/review/groups/${encodeId(nodeUuid)}/rollback`, {}).then(res => res.data);

export const approveGroup = (nodeUuid) =>
  api.delete(`/review/groups/${encodeId(nodeUuid)}`).then(res => res.data);

export const clearAll = () =>
  api.delete('/review').then(res => res.data);

// ============ Browse API ============

export const getDomains = () =>
  api.get('/browse/domains').then(res => res.data);

export const addDomain = (domain) =>
  api.post('/browse/domains', { domain }).then(res => res.data);

export const removeDomain = (domain) =>
  api.delete(`/browse/domains/${encodeURIComponent(domain)}`).then(res => res.data);

export const getNamespaces = () =>
  api.get('/browse/namespaces').then(res => res.data);

export const deleteNode = (domain, path) =>
  api.delete('/browse/node', { params: { domain, path } }).then(res => res.data);

export const searchMemories = (q, { domain, limit } = {}) =>
  api.get('/browse/search', { params: { q, domain, limit } }).then(res => res.data);

export const createMemory = (data) =>
  api.post('/browse/node', data).then(res => res.data);

export const addAlias = (data) =>
  api.post('/browse/node/alias', data).then(res => res.data);

export const renameNode = (data) =>
  api.post('/browse/node/rename', data).then(res => res.data);

// ============ Settings API ============

export const getSettings = () =>
  api.get('/settings').then(res => res.data);

export const updateSettings = (data) =>
  api.put('/settings', data).then(res => res.data);

export const getSettingsBootUris = () =>
  api.get('/settings/boot-uris').then(res => res.data);

export const setSettingsBootUris = (uris) =>
  api.put('/settings/boot-uris', { uris }).then(res => res.data);

export const toggleSettingsBootUri = (uri, enabled) =>
  api.patch('/settings/boot-uris', { uri, enabled }).then(res => res.data);

export const getAllBootUris = () =>
  api.get('/settings/boot-uris/all').then(res => res.data.boot_uris);

const _nsSlug = (ns) => encodeURIComponent(ns || '_ns_default_0x7f3a9e');

export const setBootUrisForNs = (namespace, uris) =>
  api.put(`/settings/boot-uris/ns/${_nsSlug(namespace)}`, { uris }).then(res => res.data);

export const deleteBootUrisForNs = (namespace) =>
  api.delete(`/settings/boot-uris/ns/${_nsSlug(namespace)}`).then(res => res.data);

// --- Presets ---

export const listPresets = () =>
  api.get('/presets').then(res => res.data.presets);

export const createPreset = (data) =>
  api.post('/presets', data).then(res => res.data);

export const updatePreset = (id, data) =>
  api.put(`/presets/${id}`, data).then(res => res.data);

export const deletePreset = (id) =>
  api.delete(`/presets/${id}`).then(res => res.data);

export const activatePreset = (id) =>
  api.post(`/presets/${id}/activate`).then(res => res.data);

export const duplicatePreset = (id, newName) =>
  api.post(`/presets/${id}/duplicate`, { new_name: newName }).then(res => res.data);

// --- Database ---

export const getDatabaseStatus = () =>
  api.get('/settings/database/status').then(res => res.data);

export const testDatabase = (database_url) =>
  api.post('/settings/database/test', { database_url }).then(res => res.data);

export const createDatabase = (path) =>
  api.post('/settings/database/create', { path }).then(res => res.data);

export const openDbFolder = () =>
  api.post('/settings/database/open-folder').then(res => res.data);

export default api;