import { get, put } from './request'

// 违规类型二级目录（含每类型的违规船舶数量）
export function fetchViolationCatalog() {
  return get('/violations/catalog')
}

// 违规列表（分页 + 类型筛选）
export function fetchViolations({ type = '', page = 1, pageSize = 50 } = {}) {
  const params = new URLSearchParams()
  if (type) params.set('type', type)
  params.set('page', page)
  params.set('pageSize', pageSize)
  return get(`/violations?${params.toString()}`)
}

// 违规详情
export function fetchViolationDetail(id) {
  return get(`/violations/${id}`)
}

// 违规证据数据
export function fetchViolationEvidence(id) {
  return get(`/violations/${id}/evidence`)
}

// 违规证据统计
export function fetchViolationEvidenceStats() {
  return get('/violations/evidence-stats')
}

// 更新违规状态
export function updateViolationStatus(id, status) {
  return put(`/violations/${id}/status`, { status })
}
