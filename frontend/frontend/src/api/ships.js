import { get } from './request'

export function fetchShips(params = {}) {
  const query = new URLSearchParams()
  if (params.search) query.set('search', params.search)
  if (params.type) query.set('type', params.type)
  if (params.status) query.set('status', params.status)
  if (params.page) query.set('page', params.page)
  if (params.pageSize) query.set('pageSize', params.pageSize)
  const qs = query.toString()
  return get(`/ships${qs ? '?' + qs : ''}`)
}

/**
 * 原始数据可视化：清洗前轨迹（raw_track）+ 船舶单位清洗统计。
 * 返回 { ships: [...], summary: {...} }。
 */
export function fetchRawTracks() {
  return get('/ships/raw-tracks')
}

/**
 * 级联判断后可视化：每艘船的 L1 事件 + L2 路由 + L3 判定 + 清洗后轨迹。
 * 返回 { ships: [...], summary: {...} }（全部船舶单位）。
 */
export function fetchCascadeTracks() {
  return get('/ships/cascade-tracks')
}
