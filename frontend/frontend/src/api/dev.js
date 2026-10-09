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

// 预设提供商目录（DeepSeek / 智谱 / 通义 …）：登录即可读，里面只有公开的地址与模型名。
export function fetchProviderPresets() {
  return get('/dev/provider-presets')
}

// 载荷里带 preset 时，缺的字段由预设补齐（只补不覆盖），所以
// {preset: 'deepseek', api_key: 'sk-…'} 就够建一条完整档案。
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

// ── 开发者模式：自定义技能（skill.md） ──

export function fetchSkillTemplate() {
  return get('/dev/skills/template')
}

export function validateSkill(skillMd) {
  return post('/dev/skills/validate', { skill_md: skillMd })
}

export function fetchSkills() {
  return get('/dev/skills')
}

export function fetchSkill(sid) {
  return get(`/dev/skills/${sid}`)
}

export function createSkill(skillMd) {
  return post('/dev/skills', { skill_md: skillMd })
}

export function updateSkill(sid, skillMd) {
  return put(`/dev/skills/${sid}`, { skill_md: skillMd })
}

export function setSkillStatus(sid, status) {
  return put(`/dev/skills/${sid}`, { status })
}

export function deleteSkill(sid) {
  return del(`/dev/skills/${sid}`)
}
