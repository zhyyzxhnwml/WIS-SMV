import { get, put } from './request'

// 违规类型三级目录（类目 → 类型 → 细类，含每项违规船舶数量）
export function fetchViolationCatalog() {
  return get('/violations/catalog')
}

/**
 * 违规列表（分页 + 类目/类型/细类/关键词筛选）。
 *
 * 四个筛选条件都由后端下推到 SQL，且类目与类型是**求交**关系，
 * 所以「类目=AIS设备类 + 类型=进入禁航区」会正确地返回 0 条。
 * 前端做三级联动，正常情况下不会发出矛盾组合。
 *
 * 注：subtype 形如 'ais_gap#suspected_off'，URLSearchParams 会把 '#' 编成 %23，
 * 否则会被浏览器当锚点截断。
 */
export function fetchViolations({
  type = '', category = '', subtype = '', search = '', page = 1, pageSize = 50,
} = {}) {
  const params = new URLSearchParams()
  if (category) params.set('category', category)
  if (type) params.set('type', type)
  if (subtype) params.set('subtype', subtype)
  if (search) params.set('search', search)
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
