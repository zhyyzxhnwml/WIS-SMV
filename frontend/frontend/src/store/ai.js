import { reactive } from 'vue'
import { fetchProfile, updateProfile, updateColors, generateLogo as apiGenerateLogo } from '../api/ai.js'

export const DEFAULT_COLORS = {
  normal: '#0f9d58',
  anchored: '#f59e0b',
  berthed: '#4285f4',
  violating: '#ea4335',
  track: '#1a73e8'
}

export const aiConfig = reactive({
  name: '级联监管者',
  avatar: '/vessel-violation-agent-avatar.jpg',
  logoUrl: '/port_vessel_violation_logo.jpg',
  colors: { ...DEFAULT_COLORS }
})

// 判断值是否为图片地址（路径 / 绝对地址 / data URI），用于头像/图标的 emoji ↔ 图片 双形态渲染
export function isImageUrl(v) {
  return typeof v === 'string' && /^(data:|https?:\/\/|\/)/.test(v)
}

export async function loadAiProfile() {
  try {
    const p = await fetchProfile()
    applyProfile(p)
  } catch (e) {
    console.error('AI 配置加载失败:', e)
  }
}

export async function updateAiProfile(data) {
  const p = await updateProfile(data)
  applyProfile(p)
  return p
}

export async function saveColors(colors) {
  const c = await updateColors({ colors })
  aiConfig.colors = { ...DEFAULT_COLORS, ...(c || {}) }
  return c
}

export async function generateLogo() {
  const p = await apiGenerateLogo()
  if (p && p.logoUrl) aiConfig.logoUrl = p.logoUrl
  return p
}

function applyProfile(p) {
  if (!p) return
  if (p.name) aiConfig.name = p.name
  if (p.avatar) aiConfig.avatar = p.avatar
  if (p.logoUrl) aiConfig.logoUrl = p.logoUrl
  if (p.colors) aiConfig.colors = { ...DEFAULT_COLORS, ...p.colors }
}
