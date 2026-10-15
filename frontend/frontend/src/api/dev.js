import { get, put, post, del } from './request'

// ── 开发者模式：全局 LLM 模型与参数配置 ──

export function fetchLlmConfig() {
  return get('/dev/llm-config')
}

export function saveLlmConfig(cfg) {
  return put('/dev/llm-config', cfg)
}

export function testLlmConfig() {
  return post('/dev/llm-config/test')
}

export function fetchModels() {
  return get('/dev/models')
}

export function resetLlmConfig() {
  return post('/dev/llm-config/reset')
}

// ── 开发者模式：模型提供商（多提供商 + 用途绑定） ──
//
// 写操作全部要 model.config 权限点（后端 @perm_required），读操作登录即可。
// 密钥的三种语义（后端 llm_providers.upsert_provider 定义，前端必须照办）：
//   api_key 字段缺席 → 保持原密钥    '' → 清除    非空 → 覆盖
// 所以「不改密钥就保存」时不要把 api_key 塞进 payload（用 buildProviderPayload）。

export function fetchProviders() {
  return get('/dev/providers')
}

// 新增提供商（需超管）。厂商预设已在启动时全部补进档案，页面上就地填密钥即可；
// 这里只用于「自定义 / 其他」这类目录里没有的地址。
export function createProvider(data) {
  return post('/dev/providers', data)
}

export function updateProvider(pid, data) {
  return put(`/dev/providers/${pid}`, data)
}

export function deleteProvider(pid) {
  return del(`/dev/providers/${pid}`)
}

export function fetchProviderModels(pid) {
  return get(`/dev/providers/${pid}/models`)
}

export function testProvider(pid, model = '') {
  return post(`/dev/providers/${pid}/test`, { model })
}

export function fetchBindings() {
  return get('/dev/bindings')
}

export function saveBindings(bindings) {
  // 直接发绑定表本身（后端就是这么读的）：{chat|judge|draft: {provider_id, model} | null}
  // 传 null = 取消显式绑定，回到「按能力自动回落」。
  return put('/dev/bindings', bindings)
}

// ── 可执行插件（阶段 2：dsh 风格，manifest + Python 判定代码） ──
//
// 权限分两层（后端 @perm_required）：
//   plugin.draft   —— 所有角色：新建、编辑、校验、试跑、让 AI 起草
//   plugin.publish —— 仅超级管理员：启用、停用、回滚、删除
// 草稿不参与线上判定；**启用后跑的是真代码且全局生效**，所以那条线收紧到超管。
//
// 传参说明：编辑是「manifest 一块 + 源码一块」两个输入框，所以这里传
// { manifest_yaml, source_py }；后端会用 manifest.compose_plugin_file 拼回全文。
// 手写字符串拼 `---` 差一个就会变成「缺少 manifest 段」，那种报错看不出是拼接问题。

export function fetchPlugins() {
  return get('/dev/plugins')
}

export function fetchPluginTemplate() {
  return get('/dev/plugins/template')
}

export function fetchPlugin(pid) {
  return get(`/dev/plugins/${pid}`)
}

export function fetchPluginVersions(pid) {
  return get(`/dev/plugins/${pid}/versions`)
}

export function fetchPluginVersion(pid, version) {
  return get(`/dev/plugins/${pid}/versions/${version}`)
}

export function fetchPluginFindings(pid, limit = 50) {
  return get(`/dev/plugins/${pid}/findings?limit=${limit}`)
}

export function createPlugin(payload) {
  return post('/dev/plugins', payload)
}

export function updatePlugin(pid, payload) {
  return put(`/dev/plugins/${pid}`, payload)
}

export function validatePlugin(pid, payload = {}) {
  return post(`/dev/plugins/${pid}/validate`, payload)
}

export function trialPlugin(pid, payload = {}) {
  return post(`/dev/plugins/${pid}/trial`, payload)
}

export function enablePlugin(pid, canRaiseRisk = false) {
  return post(`/dev/plugins/${pid}/enable`, { can_raise_risk: canRaiseRisk })
}

export function disablePlugin(pid) {
  return post(`/dev/plugins/${pid}/disable`)
}

export function rollbackPlugin(pid, version) {
  return post(`/dev/plugins/${pid}/rollback`, { version })
}

export function deletePlugin(pid) {
  return del(`/dev/plugins/${pid}`)
}

// 让 AI 造一个插件草稿。同步接口，可能跑几十秒（生成 → 校验 → 试跑 → 重试）。
export function forgePlugin(message) {
  return post('/ai/plugins/forge', { message })
}
