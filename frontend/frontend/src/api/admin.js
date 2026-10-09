import { get } from './request'

// 注：曾有一组「系统参数」接口（fetchSettings/saveSettings/resetSetting），
// 供在线调整级联判定阈值。已按需求移除 —— 判定阈值是流水线内部调定的量，
// 不作为运行时旋钮暴露。前端页面与路由、后端接口与权限点一并删干净。

// ── 审计日志（@perm_required("audit.view")，管理员及以上）──

function qs(params) {
  const q = new URLSearchParams()
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== '' && v !== null && v !== undefined) q.set(k, v)
  })
  const s = q.toString()
  return s ? `?${s}` : ''
}

export function fetchAuditLogs(params) {
  return get(`/admin/audit-logs${qs(params)}`)
}

export function fetchAuditActions() {
  return get('/admin/audit-logs/actions')
}

// CSV 不走 request.js —— 那个只认 JSON。这里给浏览器一个直链自己下载（同源带 cookie）。
// 注意后端把导出上限硬编码为 200 条（见 app.py 的 admin_audit_logs），UI 上要如实说明。
export function auditCsvUrl(params) {
  return `/api/v1/admin/audit-logs${qs({ ...params, format: 'csv' })}`
}

// ── 角色说明（@perm_required("user.manage")，仅超级管理员）──

export function fetchRoles() {
  return get('/admin/roles')
}
