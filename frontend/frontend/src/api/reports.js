import { get } from './request'

export function fetchViolationReports(params = {}) {
  const query = new URLSearchParams()
  if (params.search) query.set('search', params.search)
  if (params.type) query.set('type', params.type)
  if (params.page) query.set('page', params.page)
  if (params.pageSize) query.set('pageSize', params.pageSize)
  const qs = query.toString()
  return get(`/reports${qs ? '?' + qs : ''}`)
}
