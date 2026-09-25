import { get, put, post } from './request'

export function fetchUserProfile() {
  return get('/user/profile')
}

// 当前用户的数据统计（按用户隔离）
export function fetchUserStats() {
  return get('/data/user-stats')
}

// 仪表盘统计（超级管理员/管理员/用户 三级隔离）
export function fetchDashboardStats() {
  return get('/dashboard/stats')
}

// ── 账户自助 ──

// 修改显示昵称
export function updateUserProfile(name) {
  return put('/user/profile', { name })
}

// 修改密码（需旧密码）
export function changePassword(oldPassword, newPassword) {
  return post('/user/password', { old_password: oldPassword, new_password: newPassword })
}

// 上传头像（multipart，不设置 Content-Type，让浏览器生成 boundary）
export async function uploadAvatar(file) {
  const form = new FormData()
  form.append('file', file)
  const resp = await fetch('/api/v1/user/avatar', { method: 'POST', body: form })
  const result = await resp.json()
  if (result.code !== 200) {
    throw new Error(result.message || '上传失败')
  }
  return result.data
}

// ── 超级管理员：用户管理 ──

export function fetchAdminUsers() {
  return get('/admin/users')
}

export function setUserRole(uid, role) {
  return put(`/admin/users/${uid}/role`, { role })
}

// ── 组织管理（管理员 / 超级管理员）──

export function fetchOrgs() {
  return get('/orgs')
}

export function createOrg(name) {
  return post('/orgs', { name })
}

export function fetchOrgDetail(orgId) {
  return get(`/orgs/${orgId}`)
}

export function addOrgMember(orgId, username) {
  return post(`/orgs/${orgId}/members`, { username })
}

export function removeOrgMember(orgId, userId) {
  return fetch(`/api/v1/orgs/${orgId}/members/${userId}`, { method: 'DELETE' })
    .then(r => r.json())
    .then(result => {
      if (result.code !== 200) throw new Error(result.message || '移除失败')
      return result.data
    })
}

export function deleteOrg(orgId) {
  return fetch(`/api/v1/orgs/${orgId}`, { method: 'DELETE' })
    .then(r => r.json())
    .then(result => {
      if (result.code !== 200) throw new Error(result.message || '删除失败')
      return result.data
    })
}
