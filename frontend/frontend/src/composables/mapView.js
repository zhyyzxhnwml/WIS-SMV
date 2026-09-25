/**
 * composables/mapView.js — 四个监控阶段页共用同一份地图视野
 *
 * 为什么需要它：四个阶段（原始 → 清洗 → 级联 → 最终）要能"只做加法"地看，
 * 前提是**视野不能变**。原先每页 initMap 后各自 setFitView，而各页画的船集不同
 * （原始是 rawTrack 全集、清洗后只有保留点、级联/最终又不一样），
 * 于是点上一步/下一步地图会跳一下，根本看不出这一步加了什么。
 *
 * 做法：第一步把视野定下来（fitView 一次）并记住，之后每一步都恢复到它；
 * 用户自己拖动/缩放过的视野也会被记住并沿用。
 *
 * 模块级变量：在 SPA 里跨页面共享，刷新后重置。
 */

// 没有共享视野时的初始视野（与各页地图初始化保持一致）
export const DEFAULT_VIEW = { center: [106.663, 29.582], zoom: 15 }

let view = null

/** 是否已经定过视野（定过就不再重新 fitView）。 */
export function hasView() {
  return view !== null
}

/** 记住当前地图视野。 */
export function rememberView(map) {
  if (!map) return
  try {
    const c = map.getCenter()
    view = { center: [c.lng, c.lat], zoom: map.getZoom() }
  } catch (e) {
    // 地图已销毁时取中心会抛，忽略即可
  }
}

/** 把地图恢复到共享视野。返回是否恢复成功（false = 还没定过视野）。 */
export function applyView(map) {
  if (!map || !view) return false
  map.setZoomAndCenter(view.zoom, view.center)
  return true
}

/** 清掉共享视野（需要重新适配时用）。 */
export function resetView() {
  view = null
}
