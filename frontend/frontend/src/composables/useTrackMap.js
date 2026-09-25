/**
 * composables/useTrackMap.js — 船舶监控子页共享的地图逻辑
 *
 * 四个子页（原始数据/清洗后/级联判断/区域分析）共用：
 *   - 高德地图初始化/销毁
 *   - 轨迹绘制（断段：AIS中断/位置跳变/陆地/大间距）
 *   - hover 信息卡绑定
 * 各页只提供自己的"船舶着色函数"与图例。
 */
import { ref, computed } from 'vue'
import { loadAMap } from '../utils/amap.js'
import { aiConfig } from '../store/ai.js'

// 相邻点间距超过该值(km) → 视为数据间隔（几何兜底断段，避免强行连线）
const GAP_KM = 2.0

export function useTrackMap() {
  const mapContainer = ref(null)
  const loading = ref(false)
  const hoverShip = ref(null)
  const hoverStyle = ref({})
  const trackCount = ref(0)

  let map = null
  let AMapLib = null
  let polylines = []

  const colors = computed(() => aiConfig.colors)

  /** 初始化地图（容器 ref 就绪后调用）。返回地图实例。 */
  async function initMap(onClick) {
    if (!mapContainer.value) return null
    try {
      AMapLib = await loadAMap()
      map = new AMapLib.Map(mapContainer.value, {
        center: [106.663, 29.582],
        zoom: 15,
        mapStyle: 'amap://styles/light',
        resizeEnable: true,
        showIndoorMap: false,
        layers: [new AMapLib.TileLayer.Satellite()],
      })
      if (onClick) map.on('click', onClick)
      return map
    } catch (e) {
      console.error('地图加载失败:', e)
      return null
    }
  }

  function destroyMap() {
    clearTracks()
    if (map) { map.destroy(); map = null }
  }

  function fitView() {
    if (map) setTimeout(() => map.setFitView(), 300)
  }

  // 两点球面距离（km）
  function haversineKm(a, b) {
    const R = 6371
    const toRad = d => d * Math.PI / 180
    const p1 = toRad(a[1]), p2 = toRad(b[1])
    const dp = toRad(b[1] - a[1]), dl = toRad(b[0] - a[0])
    const h = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2
    return 2 * R * Math.asin(Math.sqrt(h))
  }

  /**
   * 按质量标记断段：AIS中断/位置跳变处断开；跳变段画橙色虚线；
   * 大间距画灰色虚线；陆地点孤立标记。
   * track 点格式：{ lat, lng, speed, tags?: [], onLand?: bool, crossesLand?: bool }
   */
  function splitTrack(track) {
    const segments = []   // 正常分段
    const jumpLegs = []   // 跳变 leg（橙色虚线）
    const gapLegs = []    // 数据间隔 leg（灰色虚线）
    const landPoints = [] // 陆地孤立点
    let cur = []
    for (let i = 0; i < track.length; i++) {
      const p = track[i]
      const lnglat = [p.lng, p.lat]
      const tags = p.tags || []
      const hasJump = tags.some(t => String(t).includes('位置跳变'))
      const hasGap = tags.some(t => String(t).includes('AIS中断'))

      if (p.onLand) {
        if (cur.length) segments.push(cur)
        landPoints.push(lnglat)
        cur = []
        continue
      }
      if (p.crossesLand && cur.length) {
        segments.push(cur)
        cur = []
      } else if ((hasJump || hasGap) && cur.length) {
        segments.push(cur)
        if (hasJump) jumpLegs.push([cur[cur.length - 1], lnglat])
        cur = []
      } else if (cur.length && haversineKm(cur[cur.length - 1], lnglat) > GAP_KM) {
        segments.push(cur)
        gapLegs.push([cur[cur.length - 1], lnglat])
        cur = []
      }
      cur.push(lnglat)
    }
    if (cur.length) segments.push(cur)
    return { segments, jumpLegs, gapLegs, landPoints }
  }

  /**
   * 绘制一批船舶轨迹。
   * @param {Array} ships 每艘 { mmsi, track:[{lat,lng,speed,tags,onLand,crossesLand}], ... }
   *   track 也可叫 rawTrack（/ships/raw-tracks 原始数据接口用后者，见下方取值）
   * @param {Object} opts
   *   - colorOf(ship) → { color, weight, opacity }（各页自定义着色）
   *   - infoOf(ship) → hover 卡片信息对象
   *   - filterText   → MMSI 搜索高亮
   */
  function drawShips(ships, opts = {}) {
    clearTracks()
    if (!map || !AMapLib || !ships?.length) return
    const AMap = AMapLib
    const filter = (opts.filterText || '').toLowerCase().trim()
    const allPaths = []
    const c = colors.value

    ships.forEach(ship => {
      // 两种字段名都要认：/ships/raw-tracks 返回 rawTrack（原始抽样点），
      // /ships 与 /ships/cascade-tracks 返回 track（清洗后轨迹）。
      // 只认 track 会让原始数据页 529 艘船全部在此处 return，地图上一条轨迹都没有。
      const track = ship.track || ship.rawTrack
      if (!track || track.length < 2) return
      const matched = filter && String(ship.mmsi).toLowerCase().includes(filter)

      const style = (opts.colorOf ? opts.colorOf(ship, c) : null) || {
        color: c.normal || '#0f9d58', weight: 2, opacity: 0.5,
      }
      const strokeColor = matched ? '#ff6b00' : style.color
      const weight = matched ? 5 : style.weight
      const opacity = matched ? 1.0 : style.opacity

      const qinfo = opts.infoOf ? opts.infoOf(ship) : { mmsi: ship.mmsi, name: ship.mmsi }

      const { segments, jumpLegs, gapLegs, landPoints } = splitTrack(track)
      segments.forEach(seg => {
        if (seg.length < 2) return
        const polyline = new AMap.Polyline({
          path: seg, strokeColor, strokeWeight: weight, strokeOpacity: opacity,
          lineJoin: 'round', lineCap: 'round',
        })
        bindHover(polyline, seg, qinfo, weight, opacity)
        polyline.setMap(map)
        polylines.push(polyline)
        allPaths.push(...seg)
      })
      jumpLegs.forEach(leg => {
        const line = new AMap.Polyline({
          path: leg, strokeColor: '#ffb74d', strokeWeight: weight + 1, strokeOpacity: 0.95,
          strokeStyle: 'dashed', lineJoin: 'round', lineCap: 'round',
        })
        line.setMap(map)
        polylines.push(line)
      })
      gapLegs.forEach(leg => {
        const line = new AMap.Polyline({
          path: leg, strokeColor: 'rgba(176,190,197,0.7)', strokeWeight: weight, strokeOpacity: 0.55,
          strokeStyle: 'dashed', lineJoin: 'round', lineCap: 'round',
        })
        line.setMap(map)
        polylines.push(line)
      })
      landPoints.forEach(pt => {
        const marker = new AMap.CircleMarker({
          center: pt, radius: 4, strokeColor: '#a1887f', strokeOpacity: 0.9,
          fillColor: '#ff5722', fillOpacity: 0.9, zIndex: 200,
        })
        marker.setMap(map)
        polylines.push(marker)
      })
    })

    trackCount.value = ships.length
    if (allPaths.length > 0) map.setFitView(null, false, [50, 50, 50, 50])
  }

  /** 绘制额外的散点图层（如原始数据的噪声点高亮）。 */
  function drawPoints(points, style) {
    if (!map || !AMapLib || !points?.length) return
    points.forEach(pt => {
      const marker = new AMapLib.CircleMarker({
        center: [pt.lng, pt.lat],
        radius: style?.radius || 5,
        fillColor: pt.color || style?.color || '#ff5722',
        fillOpacity: style?.opacity ?? 0.9,
        strokeColor: '#ffffff', strokeWeight: 1, strokeOpacity: 0.8,
        zIndex: 300,
      })
      marker.setMap(map)
      polylines.push(marker)
    })
  }

  function bindHover(polyline, seg, qinfo, weight, opacity) {
    const center = seg[Math.floor(seg.length / 2)]
    polyline.on('mouseover', (e) => {
      const pos = e.lnglat || center
      hoverShip.value = qinfo
      hoverStyle.value = {
        left: (map.lngLatToContainer(pos)?.x || 400) + 'px',
        top: (map.lngLatToContainer(pos)?.y || 200) + 'px',
      }
      polyline.setOptions({ strokeWeight: 7, strokeOpacity: 1.0, zIndex: 100 })
    })
    polyline.on('mouseout', () => {
      hoverShip.value = null
      polyline.setOptions({ strokeWeight: weight, strokeOpacity: opacity })
    })
    polyline.on('click', () => { map.setCenter(center); map.setZoom(16) })
  }

  function clearTracks() {
    polylines.forEach(p => map?.remove(p))
    polylines = []
    trackCount.value = 0
  }

  return {
    mapContainer, loading, hoverShip, hoverStyle, trackCount,
    initMap, destroyMap, fitView, drawShips, drawPoints, clearTracks, splitTrack,
    getMap: () => map, getAMap: () => AMapLib,
  }
}
