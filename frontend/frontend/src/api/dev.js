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
