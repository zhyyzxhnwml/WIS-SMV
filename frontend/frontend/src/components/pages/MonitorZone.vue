<template>
  <div class="monitor-page">
    <PageHeader icon="edit" title="区域分析" subtitle="框选区域多边形 · 全部统计以船舶为单位（不是轨迹点）">
      <button class="btn btn-secondary" @click="toggleDrawMode">
        <AppIcon :name="drawMode ? 'hand' : 'edit'" inline /> {{ drawMode ? '退出绘制' : '开始框选' }}
      </button>
      <button class="btn btn-secondary" @click="undoPoint" v-if="drawPoints.length > 0"><AppIcon name="refresh-cw" inline /> 撤销</button>
      <button class="btn btn-secondary" @click="clearDraw"><AppIcon name="trash" inline /> 清除</button>
      <button class="btn btn-primary" @click="reload"><AppIcon name="refresh-cw" inline /> 刷新</button>
    </PageHeader>

    <div class="map-container" ref="mapContainer" :class="{ 'draw-mode': drawMode }">
      <div v-if="drawMode" class="draw-hint">
        <AppIcon name="edit" inline /> 点击地图画点，至少3个点构成区域（当前{{ drawPoints.length }}点）· 拖拽橙色圆点可调整位置
      </div>
      <div class="map-overlay-top-left">
        <div class="map-legend">
          <h4>图例</h4>
          <div class="legend-item"><span class="dot" style="background:var(--normal-color,#0f9d58);"></span> 正常航行</div>
          <div class="legend-item"><span class="dot" style="background:var(--violating-color,#ea4335);"></span> 违规船舶</div>
          <div class="legend-item"><span class="dot" style="background:var(--anchored-color,#f59e0b);"></span> 锚泊</div>
          <div class="legend-item" style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(255,255,255,0.15);">
            <span class="dot" style="background:#ff6b00;"></span> 分析区域
          </div>
        </div>
      </div>
      <div v-if="hoverShip" class="map-hover-info" :style="hoverStyle">
        <strong>{{ hoverShip.mmsi }}</strong>
        <span>速度 {{ hoverShip.speed }} kn</span>
        <span v-if="hoverShip.violated" style="color:#ff6b6b;">{{ hoverShip.violationType }}</span>
      </div>
      <div class="map-loading" v-if="loading"><AppIcon name="hourglass" inline /> 加载数据中...</div>
      <div class="map-loaded" v-else><AppIcon name="check-circle" inline /> {{ trackCount }} 条轨迹</div>
    </div>

    <!-- 区域分析浮动窗口 -->
    <Transition name="slide-up">
      <div v-if="showAnalysis" class="zone-window" :class="{ 'is-mobile': isMobile }" :style="zoneWindowStyle" @pointerdown="startDragWindow">
        <div class="zw-header">
          <span class="zw-title"><AppIcon name="bar-chart" inline /> 区域分析</span>
          <span class="zw-stats">{{ result.shipCount || 0 }} 艘 · {{ result.areaKm2 || 0 }} km²</span>
          <button class="zw-close" @click="showAnalysis = false">&times;</button>
        </div>
        <div v-if="analysisLoading" class="zw-loading"><AppIcon name="hourglass" inline /> 分析中...</div>
        <div v-else class="zw-body">
          <!-- 方向分布（船舶单位：按主航向） -->
          <div class="zw-chart-section">
            <div class="zw-chart-label">方向分布 <span class="zw-axis-hint">（角度 → 船舶数）</span></div>
            <div class="zw-chart-wrap">
              <svg class="zw-polar" viewBox="0 0 240 240" xmlns="http://www.w3.org/2000/svg">
                <circle cx="120" cy="120" r="90" fill="none" stroke="rgba(255,255,255,0.1)" stroke-width="0.5"/>
                <circle cx="120" cy="120" r="60" fill="none" stroke="rgba(255,255,255,0.08)" stroke-width="0.5"/>
                <circle cx="120" cy="120" r="30" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="0.5"/>
                <text x="120" y="28" text-anchor="middle" fill="rgba(255,255,255,0.3)" font-size="7">{{ maxDirCount }}</text>
                <text x="120" y="58" text-anchor="middle" fill="rgba(255,255,255,0.25)" font-size="6">{{ Math.round(maxDirCount * 0.66) }}</text>
                <text x="120" y="88" text-anchor="middle" fill="rgba(255,255,255,0.2)" font-size="6">{{ Math.round(maxDirCount * 0.33) }}</text>
                <g v-for="(bin, i) in (result.directionDistribution || [])" :key="'d'+i">
                  <path :d="polarSectorPath(bin.angle, 30, bin.count, maxDirCount)" :fill="dirColor(i)" fill-opacity="0.7" :stroke="dirColor(i)" stroke-width="0.5" stroke-opacity="0.9"/>
                </g>
                <text x="120" y="18" text-anchor="middle" fill="rgba(255,255,255,0.6)" font-size="9" font-weight="600">N</text>
                <text x="222" y="123" text-anchor="start" fill="rgba(255,255,255,0.6)" font-size="9" font-weight="600">E</text>
                <text x="120" y="232" text-anchor="middle" fill="rgba(255,255,255,0.6)" font-size="9" font-weight="600">S</text>
                <text x="18" y="123" text-anchor="end" fill="rgba(255,255,255,0.6)" font-size="9" font-weight="600">W</text>
                <circle cx="120" cy="120" r="3" fill="rgba(255,255,255,0.3)"/>
                <text x="6" y="124" fill="rgba(255,255,255,0.25)" font-size="6" transform="rotate(-90,6,124)">船舶数</text>
              </svg>
            </div>
          </div>
          <!-- 速度分布（船舶单位：按区域内平均速度） -->
          <div class="zw-chart-section">
            <div class="zw-chart-label">速度分布 <span class="zw-axis-hint">（速度区间 → 船舶数）</span></div>
            <div class="zw-chart-wrap">
              <svg class="zw-bar" viewBox="0 0 260 100" xmlns="http://www.w3.org/2000/svg">
                <line x1="30" y1="10" x2="30" y2="80" stroke="rgba(255,255,255,0.15)" stroke-width="0.5"/>
                <line x1="30" y1="80" x2="255" y2="80" stroke="rgba(255,255,255,0.15)" stroke-width="0.5"/>
                <text x="26" y="14" text-anchor="end" fill="rgba(255,255,255,0.3)" font-size="6">{{ maxSpeedCount }}</text>
                <text x="26" y="47" text-anchor="end" fill="rgba(255,255,255,0.25)" font-size="6">{{ Math.round(maxSpeedCount / 2) }}</text>
                <text x="26" y="82" text-anchor="end" fill="rgba(255,255,255,0.2)" font-size="6">0</text>
                <text x="4" y="50" fill="rgba(255,255,255,0.3)" font-size="6" transform="rotate(-90,4,50)">船舶数</text>
                <rect v-for="(bin, i) in (result.speedDistribution || [])" :key="'s'+i" :x="35 + i * 37" :y="80 - barHeight(bin.count, maxSpeedCount, 65)" :width="30" :height="barHeight(bin.count, maxSpeedCount, 65)" :fill="speedColor(i)" fill-opacity="0.8" rx="2"/>
                <text v-for="(bin, i) in (result.speedDistribution || [])" :key="'st'+i" :x="50 + i * 37" y="93" text-anchor="middle" fill="rgba(255,255,255,0.5)" font-size="7">{{ bin.range }}</text>
                <text x="142" y="99" text-anchor="middle" fill="rgba(255,255,255,0.3)" font-size="6">速度 (kn)</text>
              </svg>
            </div>
          </div>
          <!-- 时间分布（船舶单位：按首次进入时刻） -->
          <div class="zw-chart-section">
            <div class="zw-chart-label">进入时刻 <span class="zw-axis-hint">（时刻 → 船舶数）</span></div>
            <div class="zw-chart-wrap">
              <svg class="zw-bar" viewBox="0 0 260 100" xmlns="http://www.w3.org/2000/svg">
                <line x1="30" y1="10" x2="30" y2="80" stroke="rgba(255,255,255,0.15)" stroke-width="0.5"/>
                <line x1="30" y1="80" x2="255" y2="80" stroke="rgba(255,255,255,0.15)" stroke-width="0.5"/>
                <text x="26" y="14" text-anchor="end" fill="rgba(255,255,255,0.3)" font-size="6">{{ maxTimeCount }}</text>
                <text x="26" y="47" text-anchor="end" fill="rgba(255,255,255,0.25)" font-size="6">{{ Math.round(maxTimeCount / 2) }}</text>
                <text x="26" y="82" text-anchor="end" fill="rgba(255,255,255,0.2)" font-size="6">0</text>
                <text x="4" y="50" fill="rgba(255,255,255,0.3)" font-size="6" transform="rotate(-90,4,50)">船舶数</text>
                <rect v-for="(bin, i) in (result.timeDistribution || [])" :key="'t'+i" :x="33 + i * 9.2" :y="80 - barHeight(bin.count, maxTimeCount, 65)" :width="7" :height="barHeight(bin.count, maxTimeCount, 65)" fill="#4fc3f7" fill-opacity="0.7" rx="1"/>
                <text v-for="h in [0,3,6,9,12,15,18,21]" :key="'tl'+h" :x="36 + h * 9.2" y="93" text-anchor="middle" fill="rgba(255,255,255,0.4)" font-size="6">{{ String(h).padStart(2,'0') }}</text>
                <text x="142" y="99" text-anchor="middle" fill="rgba(255,255,255,0.3)" font-size="6">时刻 (h)</text>
              </svg>
            </div>
          </div>
          <!-- 船舶列表（按区域内点数降序） -->
          <div class="zw-chart-section">
            <div class="zw-chart-label">船舶列表 <span class="zw-axis-hint">（共 {{ (result.ships||[]).length }} 艘）</span></div>
            <table class="zw-ship-table">
              <thead><tr><th>MMSI</th><th>均速</th><th>主航向</th><th>驻留</th><th>状态</th></tr></thead>
              <tbody>
                <tr v-for="s in (result.ships || []).slice(0, 12)" :key="s.mmsi">
                  <td>{{ s.mmsi }}</td>
                  <td>{{ s.zoneAvgSpeed }}kn</td>
                  <td>{{ s.mainCourse != null ? s.mainCourse + '°' : '-' }}</td>
                  <td>{{ s.dwellMinutes ? s.dwellMinutes + 'min' : '-' }}</td>
                  <td>
                    <span v-if="s.violationDetected" class="risk-tag risk-high">违规</span>
                    <span v-else-if="s.anchored" class="risk-tag risk-low">锚泊</span>
                    <span v-else class="risk-tag" style="background:#0f9d58;">航行</span>
                  </td>
                </tr>
                <tr v-if="(result.ships||[]).length === 0"><td colspan="5" style="text-align:center;color:rgba(255,255,255,0.4);">无</td></tr>
              </tbody>
            </table>
          </div>
          <!-- 底部统计摘要 -->
          <div class="zw-summary">
            <span><AppIcon name="ship" inline /> {{ result.shipCount || 0 }}艘</span>
            <span style="color:#0f9d58;"><AppIcon name="compass" inline /> {{ result.sailingCount || 0 }}航行</span>
            <span style="color:#f59e0b;"><AppIcon name="anchor" inline /> {{ result.anchoredCount || 0 }}锚泊</span>
            <span v-if="(result.violatedShips||[]).length" style="color:#ff6b6b;"><AppIcon name="alert-octagon" inline /> {{ result.violatedShips.length }}违规</span>
            <span><AppIcon name="clock" inline /> 均速 {{ result.avgSpeed || 0 }}kn</span>
            <span><AppIcon name="clock" inline /> 均驻留 {{ result.avgDwellMinutes || 0 }}min</span>
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, nextTick, computed } from 'vue'
import PageHeader from '../../components/PageHeader.vue'
import AppIcon from '../../components/AppIcon.vue'
import { useTrackMap } from '../../composables/useTrackMap.js'
import { fetchShips } from '../../api/ships.js'

const { mapContainer, initMap, destroyMap, drawShips, hoverShip, hoverStyle, trackCount } = useTrackMap()

const ships = ref([])
const loading = ref(true)
const searchText = ref('')

// 绘制状态
const drawMode = ref(false)
const drawPoints = ref([])
let drawOverlay = null
let drawMarkers = []

// 分析弹窗
const showAnalysis = ref(false)
const analysisLoading = ref(false)
const result = ref({})

// 移动端
const mqMobile = window.matchMedia('(max-width: 768px)')
const isMobile = ref(mqMobile.matches)
if (typeof mqMobile.addEventListener === 'function') mqMobile.addEventListener('change', e => { isMobile.value = e.matches })

const zoneWindowStyle = ref({ right: '20px', top: '80px' })
let dragging = false, dragOffset = { x: 0, y: 0 }

function startDragWindow(e) {
  if (e.target.closest('.zw-close')) return
  if (isMobile.value) return
  e.preventDefault()
  dragging = true
  const rect = e.currentTarget.getBoundingClientRect()
  dragOffset.x = e.clientX - rect.left
  dragOffset.y = e.clientY - rect.top
  const onMove = (ev) => {
    if (!dragging) return
    zoneWindowStyle.value = { left: (ev.clientX - dragOffset.x) + 'px', top: (ev.clientY - dragOffset.y) + 'px', right: 'auto' }
  }
  const onUp = () => {
    dragging = false
    document.removeEventListener('pointermove', onMove)
    document.removeEventListener('pointerup', onUp)
    document.removeEventListener('pointercancel', onUp)
  }
  document.addEventListener('pointermove', onMove)
  document.addEventListener('pointerup', onUp)
  document.addEventListener('pointercancel', onUp)
}

const colors = computed(() => ({}))
function colorOf(ship, c) {
  if (ship.violated) return { color: c.violating || '#ea4335', weight: 3, opacity: 0.9 }
  const avg = ship.avgSpeed || 0
  if (avg > 15) return { color: '#ea4335', weight: 2, opacity: 0.8 }
  if (avg < 0.5) return { color: c.anchored || '#f59e0b', weight: 2, opacity: 0.8 }
  return { color: c.normal || '#0f9d58', weight: 2, opacity: 0.4 }
}
function infoOf(ship) {
  return { mmsi: ship.mmsi, name: ship.name, speed: ship.track?.[0]?.speed || '?', violated: ship.violated, violationType: ship.violationType || '违规船舶' }
}

async function reload() {
  loading.value = true
  try {
    const data = await fetchShips()
    ships.value = data.list || []
    await nextTick()
    if (!map) { await initMap(onMapClick) }
    drawShips(ships.value, { colorOf, infoOf, filterText: searchText.value })
  } catch (e) { console.error('zone ships load failed', e) }
  finally { loading.value = false }
}
function doSearch() { drawShips(ships.value, { colorOf, infoOf, filterText: searchText.value }) }

let map = null, AMapLib = null
async function onMapClick(e) {
  if (!drawMode.value || !map) return
  const lat = e.lnglat.getLat(), lng = e.lnglat.getLng()
  drawPoints.value.push({ lat, lng })
  addDrawMarker(drawPoints.value.length - 1)
  updateDrawPolygon()
  if (drawPoints.value.length >= 3) await triggerAnalysis()
}

function toggleDrawMode() {
  drawMode.value = !drawMode.value
  if (!drawMode.value) clearDraw()
  document.body.style.cursor = drawMode.value ? 'crosshair' : ''
}
function addDrawMarker(idx) {
  if (!map || !AMapLib) return
  const p = drawPoints.value[idx]
  const marker = new AMapLib.CircleMarker({ center: [p.lng, p.lat], radius: 8, fillColor: '#ff6b00', fillOpacity: 1, strokeColor: '#ffffff', strokeWeight: 3, zIndex: 120, draggable: true, cursor: 'grab', bubble: true })
  marker.setMap(map)
  const label = new AMapLib.Marker({ position: [p.lng, p.lat], content: `<div style="color:#fff;font-size:11px;font-weight:700;width:16px;height:16px;display:flex;align-items:center;justify-content:center;pointer-events:none;user-select:none;text-shadow:0 1px 2px rgba(0,0,0,0.5);">${idx + 1}</div>`, offset: new AMapLib.Pixel(-8, -8), zIndex: 121 })
  label.setMap(map)
  marker.on('dragging', () => {
    const c = marker.getCenter()
    drawPoints.value[idx] = { lat: c.getLat(), lng: c.getLng() }
    label.setPosition([c.getLng(), c.getLat()])
    updateDrawPolygon()
  })
  marker.on('dragend', () => { if (drawPoints.value.length >= 3) triggerAnalysis() })
  drawMarkers.push({ circle: marker, label })
}
function updateDrawPolygon() {
  if (drawOverlay) { map?.remove(drawOverlay); drawOverlay = null }
  if (!map || !AMapLib || drawPoints.value.length < 2) return
  const path = drawPoints.value.map(p => [p.lng, p.lat])
  drawOverlay = drawPoints.value.length === 2
    ? new AMapLib.Polyline({ path, strokeColor: '#ff6b00', strokeWeight: 3, strokeOpacity: 0.8, lineJoin: 'round' })
    : new AMapLib.Polygon({ path, fillColor: '#ff6b00', fillOpacity: 0.15, strokeColor: '#ff6b00', strokeWeight: 3, strokeOpacity: 0.8 })
  drawOverlay.setMap(map)
}
function clearDrawOverlay() {
  if (drawOverlay) { map?.remove(drawOverlay); drawOverlay = null }
  drawMarkers.forEach(m => { map?.remove(m.circle); map?.remove(m.label) })
  drawMarkers = []
}
function undoPoint() {
  if (!drawPoints.value.length) return
  drawPoints.value.pop()
  clearDrawOverlay()
  drawPoints.value.forEach((_, i) => addDrawMarker(i))
  updateDrawPolygon()
  showAnalysis.value = false
}
function clearDraw() { drawMode.value = false; drawPoints.value = []; clearDrawOverlay(); showAnalysis.value = false; document.body.style.cursor = '' }

async function triggerAnalysis() {
  if (drawPoints.value.length < 3) return
  analysisLoading.value = true
  showAnalysis.value = true
  try {
    const resp = await fetch('/api/v1/zone/analyze', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ points: drawPoints.value.map(p => ({ lat: p.lat, lng: p.lng })) }),
    })
    const json = await resp.json()
    if (json.code === 200) result.value = json.data
  } catch (e) { result.value = {} }
  finally { analysisLoading.value = false }
}

// ── 图表计算属性 ──
const maxDirCount = computed(() => Math.max(1, ...(result.value.directionDistribution || []).map(d => d.count)))
const maxSpeedCount = computed(() => Math.max(1, ...(result.value.speedDistribution || []).map(d => d.count)))
const maxTimeCount = computed(() => Math.max(1, ...(result.value.timeDistribution || []).map(d => d.count)))

function polarSectorPath(startAngle, spanAngle, count, maxCount) {
  const cx = 120, cy = 120, maxR = 85
  const r = Math.max(5, (count / maxCount) * maxR)
  const a1 = (startAngle - 90) * Math.PI / 180, a2 = (startAngle + spanAngle - 90) * Math.PI / 180
  const x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1)
  const x2 = cx + r * Math.cos(a2), y2 = cy + r * Math.sin(a2)
  const largeArc = spanAngle > 180 ? 1 : 0
  return `M${cx},${cy} L${x1},${y1} A${r},${r} 0 ${largeArc} 1 ${x2},${y2} Z`
}
function barHeight(count, maxCount, maxPx = 65) { return Math.max(1, (count / maxCount) * maxPx) }
function dirColor(i) { return ['#ff6b00','#ff8f00','#ffb300','#ffd600','#ffea00','#c6ff00','#76ff03','#00e676','#00bfa5','#00b0ff','#2979ff','#651fff'][i % 12] }
function speedColor(i) { return ['#0f9d58','#4caf50','#8bc34a','#ffc107','#ff9800','#f44336'][i % 6] }

onMounted(async () => {
  map = await initMap(onMapClick)
  AMapLib = window.AMap
  await reload()
})
onUnmounted(() => { destroyMap(); map = null; if (typeof mqMobile.removeEventListener === 'function') mqMobile.removeEventListener('change', e => { isMobile.value = e.matches }) })
</script>

<style scoped>
.monitor-page { display: flex; flex-direction: column; gap: 16px; }
.search-input { padding: 6px 12px; border: 1px solid rgba(169, 205, 226, 0.45); border-radius: 8px; font-size: 13px; width: 240px; background: #fff; }
.map-container { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); height: 420px; overflow: hidden; position: relative; z-index: 0; border: 1px solid rgba(169, 205, 226, 0.45); }
.map-container.draw-mode { outline: 3px solid #ff8f5c; outline-offset: -3px; }
.draw-hint { position: absolute; top: 16px; left: 50%; transform: translateX(-50%); z-index: 2000; background: linear-gradient(135deg, #0a3d62, #08718f); color: #fff; padding: 8px 20px; border-radius: 20px; font-size: 13px; white-space: nowrap; box-shadow: 0 6px 20px rgba(8, 113, 143, 0.4); }
.map-overlay-top-left { position: absolute; top: 60px; left: 16px; z-index: 1000; }
.map-legend { background: rgba(6, 31, 56, 0.9); padding: 14px 16px; border-radius: var(--radius-sm); min-width: 150px; color: #fff; backdrop-filter: blur(8px); border: 1px solid rgba(118, 200, 255, 0.22); }
.map-legend h4 { font-size: 13px; margin-bottom: 8px; padding-left: 10px; position: relative; }
.map-legend h4::before { content: ''; position: absolute; left: 0; top: 50%; transform: translateY(-50%); width: 3px; height: 13px; border-radius: 2px; background: var(--gradient-teal); }
.legend-item { display: flex; align-items: center; gap: 8px; font-size: 12px; margin-bottom: 5px; color: rgba(255,255,255,0.75); }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.map-hover-info { position: absolute; z-index: 2000; background: rgba(6, 31, 56, 0.94); color: #fff; padding: 8px 14px; border-radius: 8px; font-size: 12px; line-height: 1.6; pointer-events: none; transform: translate(-50%, -100%); margin-top: -12px; white-space: nowrap; border: 1px solid rgba(118, 200, 255, 0.25); }
.map-hover-info strong { display: block; font-size: 13px; }
.map-loading, .map-loaded { position: absolute; bottom: 16px; left: 50%; transform: translateX(-50%); z-index: 1000; background: rgba(6, 31, 56, 0.85); color: #fff; padding: 6px 20px; border-radius: 20px; font-size: 12px; border: 1px solid rgba(118, 200, 255, 0.2); }
.zone-window { position: fixed; z-index: 99999; width: 320px; max-height: 90vh; background: linear-gradient(170deg, rgba(8, 36, 61, 0.96), rgba(6, 31, 56, 0.94)); backdrop-filter: blur(16px) saturate(1.4); border-radius: 14px; border: 1px solid rgba(118, 200, 255, 0.25); box-shadow: 0 8px 32px rgba(2, 15, 34, 0.55); color: #fff; overflow: hidden; user-select: none; }
.zw-header { display: flex; align-items: center; gap: 6px; padding: 10px 12px 8px; border-bottom: 1px solid rgba(118, 200, 255, 0.14); cursor: move; touch-action: none; background: linear-gradient(90deg, rgba(12,140,233,0.14), rgba(24,184,166,0.06) 60%, transparent); }
.zw-title { font-size: 13px; font-weight: 700; flex-shrink: 0; }
.zw-stats { font-size: 11px; color: rgba(150, 210, 245, 0.75); flex: 1; text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.zw-close { background: none; border: none; font-size: 18px; color: rgba(180, 220, 250, 0.5); cursor: pointer; line-height: 1; padding: 0 2px; }
.zw-close:hover { color: #fff; }
.zw-loading { text-align: center; padding: 30px 0; color: rgba(180, 220, 250, 0.6); font-size: 12px; }
.zw-body { padding: 8px 12px 12px; overflow: auto; max-height: calc(90vh - 50px); }
.zw-chart-section { margin-bottom: 10px; }
.zw-chart-label { font-size: 11px; font-weight: 600; color: rgba(150, 210, 245, 0.75); text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 4px; }
.zw-axis-hint { font-size: 9px; font-weight: 400; color: rgba(150, 210, 245, 0.45); text-transform: none; letter-spacing: 0; }
.zw-chart-wrap { background: rgba(12, 140, 233, 0.07); border: 1px solid rgba(118, 200, 255, 0.1); border-radius: 8px; padding: 6px; }
.zw-polar, .zw-bar { width: 100%; height: auto; display: block; }
.zw-ship-table { width: 100%; border-collapse: collapse; font-size: 10px; color: rgba(255,255,255,0.8); }
.zw-ship-table th, .zw-ship-table td { text-align: left; padding: 3px 5px; border-bottom: 1px solid rgba(118, 200, 255, 0.1); }
.zw-ship-table th { color: rgba(150, 210, 245, 0.6); font-weight: 600; }
.risk-tag { display: inline-block; padding: 1px 5px; border-radius: 4px; color: #fff; font-weight: 600; font-size: 9px; }
.risk-tag.risk-high { background: #ea4335; }
.risk-tag.risk-low { background: #f59e0b; }
.zw-summary { display: flex; gap: 10px; flex-wrap: wrap; justify-content: center; padding: 6px 0 2px; font-size: 11px; color: rgba(150, 210, 245, 0.75); border-top: 1px solid rgba(118, 200, 255, 0.12); margin-top: 4px; }
.slide-up-enter-active, .slide-up-leave-active { transition: all 0.3s ease; }
.slide-up-enter-from, .slide-up-leave-to { transform: translateY(20px); opacity: 0; }
@media (max-width: 768px) {
  .map-container { height: 320px; }
  .zone-window.is-mobile { width: calc(100vw - 24px) !important; max-height: 65vh !important; left: 12px !important; top: 120px !important; right: auto !important; }
}
</style>
