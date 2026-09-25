<template>
  <div class="page">
    <PageHeader icon="radar" title="船舶监控" subtitle="辖区船舶实时轨迹与航行动态">
      <button class="btn btn-secondary" @click="toggleDrawMode">
        <AppIcon :name="drawMode ? 'hand' : 'edit'" inline /> {{ drawMode ? '退出绘制' : '区域分析' }}
      </button>
      <button class="btn btn-secondary" @click="undoPoint" v-if="drawPoints.length > 0"><AppIcon name="refresh-cw" inline /> 撤销</button>
      <button class="btn btn-secondary" @click="clearDraw"><AppIcon name="trash" inline /> 清除</button>
      <button class="btn btn-primary" @click="loadAllTracks"><AppIcon name="refresh-cw" inline /> 刷新</button>
    </PageHeader>

    <div class="toolbar">
      <div class="toolbar-left">
        <input type="text" class="search-input" placeholder="搜索MMSI高亮显示..." v-model="searchText" @input="doSearch">
        <span class="toolbar-hint">共 <strong>{{ shipTotal }}</strong> 艘</span>
      </div>
    </div>

    <div class="map-container" ref="mapContainer" :class="{ 'draw-mode': drawMode }">
      <div v-if="drawMode" class="draw-hint">
        <span><AppIcon name="edit" inline /> 点击地图画点，至少3个点构成区域（当前{{ drawPoints.length }}点）· 拖拽橙色圆点可调整位置</span>
      </div>
      <div class="map-overlay-top-left">
        <div class="map-legend">
          <h4>图例</h4>
          <div class="legend-item"><span class="dot" style="background:var(--violating-color, #ea4335);"></span> 违规船舶</div>
          <div class="legend-item"><span class="dot" style="background:var(--normal-color, #0f9d58);"></span> 正常航行</div>
          <div class="legend-item"><span class="dot" style="background:#ea4335;"></span> 高速(&gt;15kn)</div>
          <div class="legend-item"><span class="dot" style="background:#ffb74d;border-radius:0;width:14px;"></span> 位置跳变段（虚线）</div>
          <div class="legend-item"><span class="dot" style="background:#4fc3f7;border-radius:0;width:14px;"></span> AIS中断断点</div>
          <div class="legend-item"><span class="dot" style="background:rgba(176,190,197,0.7);border-radius:0;width:14px;"></span> 数据间隔（灰虚线）</div>
          <div class="legend-item"><span class="dot" style="background:rgba(141,110,99,0.8);border-radius:0;width:14px;"></span> 横穿陆地段（灰虚线）</div>
          <div class="legend-item"><span class="dot" style="background:#ff5722;"></span> 陆地点（异常）</div>
          <div class="legend-item" style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(255,255,255,0.15);">
            <span class="dot" style="background:#ff6b00;"></span> 分析区域
          </div>
        </div>
      </div>
      <div v-if="hoverShip" class="map-hover-info" :style="hoverStyle">
        <strong>{{ hoverShip.name }}</strong>
        <span>MMSI: {{ hoverShip.mmsi }}</span>
        <span>速度: {{ hoverShip.speed }} kn</span>
        <span v-if="hoverShip.violated" style="color:#ff6b6b;"><AppIcon name="alert-octagon" inline /> {{ hoverShip.violationType }}</span>
        <span v-if="hoverShip.qualityLevel" :style="hoverShip.dataQualitySuspect ? 'color:#ffb74d;' : 'color:#7ff0e2;'">
          数据质量: {{ qlLabel(hoverShip.qualityLevel) }}<template v-if="hoverShip.cleanedPoints">&nbsp;· 剔除 {{ hoverShip.cleanedPoints }} 异常点</template>
        </span>
        <span v-if="hoverShip.dataQualitySuspect" style="color:#ff6b6b;"><AppIcon name="alert-triangle" inline /> 数据存疑</span>
        <span v-if="hoverShip.onLandPoints || hoverShip.landCrossings" style="color:#ffb74d;">
          <AppIcon name="radar" inline /> 陆地点 {{ hoverShip.onLandPoints }} · 横穿陆地 {{ hoverShip.landCrossings }}
        </span>
      </div>
      <div class="map-loading" v-if="loading"><span><AppIcon name="hourglass" inline /> 加载数据中...</span></div>
      <div class="map-loaded" v-else><span><AppIcon name="check-circle" inline /> {{ allTracks.length }} 条轨迹</span></div>
    </div>

    <!-- ========= 区域分析浮动窗口 ========= -->
    <Transition name="slide-up">
      <div v-if="showAnalysis" class="zone-window" :class="{ 'is-mobile': isMobile }" :style="zoneWindowStyle"
           @pointerdown="startDragWindow">
        <!-- 标题栏 -->
        <div class="zw-header">
          <span class="zw-title"><AppIcon name="bar-chart" inline /> 区域分析</span>
          <span class="zw-stats">{{ result.shipCount || 0 }} 艘 · {{ result.areaKm2 || 0 }} km²</span>
          <div class="zw-zoom-btns">
            <button class="zw-zbtn" @click="zoomIn" title="放大">＋</button>
            <button class="zw-zbtn" @click="zoomOut" title="缩小">－</button>
            <button class="zw-zbtn" @click="zoomReset" title="重置"><AppIcon name="refresh-cw" :size="13" /></button>
          </div>
          <button class="zw-close" @click="showAnalysis = false">&times;</button>
        </div>

        <!-- 加载中 -->
        <div v-if="analysisLoading" class="zw-loading"><AppIcon name="hourglass" inline /> 分析中...</div>

        <!-- 图表区域 -->
        <div v-else class="zw-body" :style="{ transform: `scale(${zoomLevel})`, transformOrigin: 'top left' }">
          <!-- 方向分布（极区图 / 风玫瑰图） -->
          <div class="zw-chart-section">
            <div class="zw-chart-label">方向分布 <span class="zw-axis-hint">（角度 → 轨迹点数）</span></div>
            <div class="zw-chart-wrap">
              <svg class="zw-polar" viewBox="0 0 240 240" xmlns="http://www.w3.org/2000/svg">
                <!-- 同心圆参考线 + 刻度 -->
                <circle cx="120" cy="120" r="90" fill="none" stroke="rgba(255,255,255,0.1)" stroke-width="0.5"/>
                <circle cx="120" cy="120" r="60" fill="none" stroke="rgba(255,255,255,0.08)" stroke-width="0.5"/>
                <circle cx="120" cy="120" r="30" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="0.5"/>
                <!-- 刻度值 -->
                <text x="120" y="28" text-anchor="middle" fill="rgba(255,255,255,0.3)" font-size="7">{{ maxDirCount }}</text>
                <text x="120" y="58" text-anchor="middle" fill="rgba(255,255,255,0.25)" font-size="6">{{ Math.round(maxDirCount * 0.66) }}</text>
                <text x="120" y="88" text-anchor="middle" fill="rgba(255,255,255,0.2)" font-size="6">{{ Math.round(maxDirCount * 0.33) }}</text>
                <!-- 方向扇形 -->
                <g v-for="(bin, i) in (result.directionDistribution || [])" :key="'d'+i">
                  <path :d="polarSectorPath(bin.angle, 30, bin.count, maxDirCount)"
                        :fill="dirColor(i)" fill-opacity="0.7"
                        :stroke="dirColor(i)" stroke-width="0.5" stroke-opacity="0.9"/>
                </g>
                <!-- 方向标签 N/E/S/W -->
                <text x="120" y="18" text-anchor="middle" fill="rgba(255,255,255,0.6)" font-size="9" font-weight="600">N</text>
                <text x="222" y="123" text-anchor="start" fill="rgba(255,255,255,0.6)" font-size="9" font-weight="600">E</text>
                <text x="120" y="232" text-anchor="middle" fill="rgba(255,255,255,0.6)" font-size="9" font-weight="600">S</text>
                <text x="18" y="123" text-anchor="end" fill="rgba(255,255,255,0.6)" font-size="9" font-weight="600">W</text>
                <!-- 中心圆 -->
                <circle cx="120" cy="120" r="3" fill="rgba(255,255,255,0.3)"/>
                <!-- Y轴标注 -->
                <text x="6" y="124" fill="rgba(255,255,255,0.25)" font-size="6" transform="rotate(-90,6,124)">轨迹点数</text>
              </svg>
            </div>
          </div>

          <!-- 速度分布（直方图） -->
          <div class="zw-chart-section">
            <div class="zw-chart-label">速度分布 <span class="zw-axis-hint">（速度区间 → 轨迹点数）</span></div>
            <div class="zw-chart-wrap">
              <svg class="zw-bar" viewBox="0 0 260 100" xmlns="http://www.w3.org/2000/svg">
                <!-- Y轴参考线 -->
                <line x1="30" y1="10" x2="30" y2="80" stroke="rgba(255,255,255,0.15)" stroke-width="0.5"/>
                <line x1="30" y1="80" x2="255" y2="80" stroke="rgba(255,255,255,0.15)" stroke-width="0.5"/>
                <!-- Y轴刻度 -->
                <text x="26" y="14" text-anchor="end" fill="rgba(255,255,255,0.3)" font-size="6">{{ maxSpeedCount }}</text>
                <text x="26" y="47" text-anchor="end" fill="rgba(255,255,255,0.25)" font-size="6">{{ Math.round(maxSpeedCount / 2) }}</text>
                <text x="26" y="82" text-anchor="end" fill="rgba(255,255,255,0.2)" font-size="6">0</text>
                <!-- Y轴标注 -->
                <text x="4" y="50" fill="rgba(255,255,255,0.3)" font-size="6" transform="rotate(-90,4,50)">轨迹点数</text>
                <!-- 柱子 -->
                <rect v-for="(bin, i) in (result.speedDistribution || [])" :key="'s'+i"
                      :x="35 + i * 37" :y="80 - barHeight(bin.count, maxSpeedCount, 65)"
                      :width="30" :height="barHeight(bin.count, maxSpeedCount, 65)"
                      :fill="speedColor(i)" fill-opacity="0.8" rx="2"/>
                <!-- X轴标签 -->
                <text v-for="(bin, i) in (result.speedDistribution || [])" :key="'st'+i"
                      :x="50 + i * 37" y="93"
                      text-anchor="middle" fill="rgba(255,255,255,0.5)" font-size="7">{{ bin.range }}</text>
                <!-- X轴标注 -->
                <text x="142" y="99" text-anchor="middle" fill="rgba(255,255,255,0.3)" font-size="6">速度 (kn)</text>
              </svg>
            </div>
          </div>

          <!-- 时间分布（直方图） -->
          <div class="zw-chart-section">
            <div class="zw-chart-label">时间分布 <span class="zw-axis-hint">（时刻 → 轨迹点数）</span></div>
            <div class="zw-chart-wrap">
              <svg class="zw-bar" viewBox="0 0 260 100" xmlns="http://www.w3.org/2000/svg">
                <!-- Y轴参考线 -->
                <line x1="30" y1="10" x2="30" y2="80" stroke="rgba(255,255,255,0.15)" stroke-width="0.5"/>
                <line x1="30" y1="80" x2="255" y2="80" stroke="rgba(255,255,255,0.15)" stroke-width="0.5"/>
                <!-- Y轴刻度 -->
                <text x="26" y="14" text-anchor="end" fill="rgba(255,255,255,0.3)" font-size="6">{{ maxTimeCount }}</text>
                <text x="26" y="47" text-anchor="end" fill="rgba(255,255,255,0.25)" font-size="6">{{ Math.round(maxTimeCount / 2) }}</text>
                <text x="26" y="82" text-anchor="end" fill="rgba(255,255,255,0.2)" font-size="6">0</text>
                <!-- Y轴标注 -->
                <text x="4" y="50" fill="rgba(255,255,255,0.3)" font-size="6" transform="rotate(-90,4,50)">轨迹点数</text>
                <!-- 柱子 -->
                <rect v-for="(bin, i) in (result.timeDistribution || [])" :key="'t'+i"
                      :x="33 + i * 9.2" :y="80 - barHeight(bin.count, maxTimeCount, 65)"
                      :width="7" :height="barHeight(bin.count, maxTimeCount, 65)"
                      fill="#4fc3f7" fill-opacity="0.7" rx="1"/>
                <!-- X轴标签（每3小时） -->
                <text v-for="h in [0,3,6,9,12,15,18,21]" :key="'tl'+h"
                      :x="36 + h * 9.2" y="93"
                      text-anchor="middle" fill="rgba(255,255,255,0.4)" font-size="6">{{ String(h).padStart(2,'0') }}</text>
                <!-- X轴标注 -->
                <text x="142" y="99" text-anchor="middle" fill="rgba(255,255,255,0.3)" font-size="6">时刻 (h)</text>
              </svg>
            </div>
          </div>

          <!-- 底部统计摘要 -->
          <div class="zw-summary">
            <span><AppIcon name="clock" inline /> {{ result.avgSpeed || 0 }}kn</span>
            <span><AppIcon name="compass" inline /> {{ result.avgCourse || 0 }}°</span>
            <span v-if="result.violatedShips && result.violatedShips.length > 0" style="color:#ff6b6b;">
              <AppIcon name="alert-octagon" inline /> {{ result.violatedShips.length }}违规
            </span>
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
import { aiConfig } from '../../store/ai.js'
import { loadAMap } from '../../utils/amap.js'

const mapContainer = ref(null)
const searchText = ref('')
const allTracks = ref([])
const shipTotal = ref(0)
const loading = ref(true)
const hoverShip = ref(null)
const hoverStyle = ref({})

// 绘制状态
const drawMode = ref(false)
const drawPoints = ref([])
let drawOverlay = null
let drawMarkers = []

// 分析弹窗
const showAnalysis = ref(false)
const analysisLoading = ref(false)
const result = ref({})

let map = null
let polylines = []
let highlightTimer = null
let AMapLib = null

// 移动端检测（≤768px 区域窗口固定定位、不可拖）
const mqMobile = window.matchMedia('(max-width: 768px)')
const isMobile = ref(mqMobile.matches)
function onMqChange(e) { isMobile.value = e.matches }
if (typeof mqMobile.addEventListener === 'function') {
  mqMobile.addEventListener('change', onMqChange)
}

// 窗口拖拽 + 缩放
const zoneWindowStyle = ref({ right: '20px', top: '80px' })
let dragging = false
let dragOffset = { x: 0, y: 0 }
const zoomLevel = ref(1)

function zoomIn() { zoomLevel.value = Math.min(zoomLevel.value + 0.25, 2.5) }
function zoomOut() { zoomLevel.value = Math.max(zoomLevel.value - 0.25, 0.5) }
function zoomReset() { zoomLevel.value = 1 }

const colors = computed(() => aiConfig.colors)

// ── 图表计算属性 ──
const maxDirCount = computed(() => {
  const dist = result.value.directionDistribution || []
  return Math.max(1, ...dist.map(d => d.count))
})
const maxSpeedCount = computed(() => {
  const dist = result.value.speedDistribution || []
  return Math.max(1, ...dist.map(d => d.count))
})
const maxTimeCount = computed(() => {
  const dist = result.value.timeDistribution || []
  return Math.max(1, ...dist.map(d => d.count))
})

// ── 极区图扇形路径 ──
function polarSectorPath(startAngle, spanAngle, count, maxCount) {
  const cx = 120, cy = 120, maxR = 85
  const r = Math.max(5, (count / maxCount) * maxR)
  const a1 = (startAngle - 90) * Math.PI / 180
  const a2 = (startAngle + spanAngle - 90) * Math.PI / 180
  const x1 = cx + r * Math.cos(a1)
  const y1 = cy + r * Math.sin(a1)
  const x2 = cx + r * Math.cos(a2)
  const y2 = cy + r * Math.sin(a2)
  const largeArc = spanAngle > 180 ? 1 : 0
  return `M${cx},${cy} L${x1},${y1} A${r},${r} 0 ${largeArc} 1 ${x2},${y2} Z`
}

function barHeight(count, maxCount, maxPx = 65) {
  return Math.max(1, (count / maxCount) * maxPx)
}

function dirColor(i) {
  const palette = ['#ff6b00','#ff8f00','#ffb300','#ffd600','#ffea00','#c6ff00',
                   '#76ff03','#00e676','#00bfa5','#00b0ff','#2979ff','#651fff']
  return palette[i % palette.length]
}

function speedColor(i) {
  const palette = ['#0f9d58','#4caf50','#8bc34a','#ffc107','#ff9800','#f44336']
  return palette[i % palette.length]
}

function qlLabel(l) {
  return l === 'good' ? '良好' : l === 'fair' ? '一般' : '较差'
}

// ── 窗口拖拽（Pointer Events，兼容鼠标 + 触摸） ──
function startDragWindow(e) {
  if (e.target.closest('.zw-close') || e.target.closest('.zw-zbtn')) return
  if (isMobile.value) return // 移动端固定定位，不可拖
  e.preventDefault()
  dragging = true
  const rect = e.currentTarget.getBoundingClientRect()
  dragOffset.x = e.clientX - rect.left
  dragOffset.y = e.clientY - rect.top
  const onMove = (ev) => {
    if (!dragging) return
    const x = ev.clientX - dragOffset.x
    const y = ev.clientY - dragOffset.y
    zoneWindowStyle.value = { left: x + 'px', top: y + 'px', right: 'auto' }
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

// ── 高德地图初始化 ──
async function initMap() {
  if (!mapContainer.value) return
  try {
    var AMap = await loadAMap()
    AMapLib = AMap
    map = new AMap.Map(mapContainer.value, {
      center: [106.663, 29.582],
      zoom: 15,
      mapStyle: 'amap://styles/light',
      resizeEnable: true,
      showIndoorMap: false,
      layers: [new AMap.TileLayer.Satellite()],
    })
    setTimeout(() => map.setFitView(), 500)

    map.on('click', function(e) {
      if (!drawMode.value || !map) return
      var lat = e.lnglat.getLat()
      var lng = e.lnglat.getLng()
      drawPoints.value.push({ lat, lng })
      addDrawMarker(drawPoints.value.length - 1)
      updateDrawPolygon()
      if (drawPoints.value.length >= 3) {
        triggerAnalysis()
      }
    })
  } catch (e) {
    console.error('地图加载失败:', e)
  }
}

// ── 绘制相关 ──
function toggleDrawMode() {
  drawMode.value = !drawMode.value
  if (!drawMode.value) clearDraw()
  document.body.style.cursor = drawMode.value ? 'crosshair' : ''
}

function addDrawMarker(pointIndex) {
  if (!map || !AMapLib) return
  const p = drawPoints.value[pointIndex]
  if (!p) return

  const marker = new AMapLib.CircleMarker({
    center: [p.lng, p.lat],
    radius: 8,
    fillColor: '#ff6b00',
    fillOpacity: 1,
    strokeColor: '#ffffff',
    strokeWeight: 3,
    strokeOpacity: 1,
    zIndex: 120,
    draggable: true,
    cursor: 'grab',
    bubble: true,
  })
  marker.setMap(map)

  const labelMarker = new AMapLib.Marker({
    position: [p.lng, p.lat],
    content: `<div style="color:#fff;font-size:11px;font-weight:700;width:16px;height:16px;
              display:flex;align-items:center;justify-content:center;pointer-events:none;
              user-select:none;text-shadow:0 1px 2px rgba(0,0,0,0.5);">${pointIndex + 1}</div>`,
    offset: new AMapLib.Pixel(-8, -8),
    zIndex: 121,
  })
  labelMarker.setMap(map)

  marker.on('dragging', () => {
    const center = marker.getCenter()
    drawPoints.value[pointIndex] = { lat: center.getLat(), lng: center.getLng() }
    labelMarker.setPosition([center.getLng(), center.getLat()])
    updateDrawPolygon()
  })

  marker.on('dragend', () => {
    if (drawPoints.value.length >= 3) triggerAnalysis()
  })

  drawMarkers.push({ circle: marker, label: labelMarker, pointIndex })
}

function updateDrawPolygon() {
  if (drawOverlay) { map?.remove(drawOverlay); drawOverlay = null }
  if (!map || !AMapLib || drawPoints.value.length < 2) return

  const path = drawPoints.value.map(p => [p.lng, p.lat])
  if (drawPoints.value.length === 2) {
    drawOverlay = new AMapLib.Polyline({
      path, strokeColor: '#ff6b00', strokeWeight: 3, strokeOpacity: 0.8,
      lineJoin: 'round',
    })
  } else {
    drawOverlay = new AMapLib.Polygon({
      path, fillColor: '#ff6b00', fillOpacity: 0.15,
      strokeColor: '#ff6b00', strokeWeight: 3, strokeOpacity: 0.8,
    })
  }
  drawOverlay.setMap(map)
}

function clearDrawOverlay() {
  if (drawOverlay) { map?.remove(drawOverlay); drawOverlay = null }
  drawMarkers.forEach(m => { map?.remove(m.circle); map?.remove(m.label) })
  drawMarkers = []
}

function undoPoint() {
  if (drawPoints.value.length === 0) return
  drawPoints.value.pop()
  clearDrawOverlay()
  drawPoints.value.forEach((_, i) => addDrawMarker(i))
  updateDrawPolygon()
  showAnalysis.value = false
}

function clearDraw() {
  drawMode.value = false
  drawPoints.value = []
  clearDrawOverlay()
  showAnalysis.value = false
  document.body.style.cursor = ''
}

async function triggerAnalysis() {
  if (drawPoints.value.length < 3) return
  analysisLoading.value = true
  showAnalysis.value = true
  zoomLevel.value = 1

  try {
    const resp = await fetch('/api/v1/zone/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ points: drawPoints.value.map(p => ({ lat: p.lat, lng: p.lng })) })
    })
    const json = await resp.json()
    if (json.code === 200) {
      result.value = json.data
    } else {
      result.value = {}
    }
  } catch (e) {
    result.value = {}
  } finally {
    analysisLoading.value = false
  }
}

// ── 轨迹绘制 ──
async function loadAllTracks() {
  loading.value = true
  try {
    const resp = await fetch('/api/v1/ships/all-tracks')
    const json = await resp.json()
    if (json.code === 200 && json.data) {
      allTracks.value = json.data
      shipTotal.value = json.data.length
      if (map) { await nextTick(); drawAllTracks() }
    }
  } catch (e) {
    console.error('轨迹加载失败:', e)
  } finally {
    loading.value = false
  }
}

function drawAllTracks(filterText = '') {
  clearTracks()
  if (!map || !allTracks.value.length) return
  const AMap = window.AMap
  if (!AMap) return

  const allPaths = []
  const filter = filterText.toLowerCase().trim()
  const c = colors.value

  allTracks.value.forEach(ship => {
    const mmsi = ship.mmsi; const name = ship.name; const track = ship.track
    if (!track || track.length < 2) return
    const matched = filter && (mmsi.toLowerCase().includes(filter) || name.toLowerCase().includes(filter))

    let baseColor
    if (ship.violated) baseColor = c.violating || '#ea4335'
    else baseColor = ship.color === 'red' ? '#ea4335' : ship.color === 'yellow' ? (c.anchored || '#f59e0b') : (c.normal || '#0f9d58')

    const strokeColor = matched ? '#ff6b00' : baseColor
    const weight = matched ? 5 : (ship.violated ? 3 : 2)
    const opacity = matched ? 1.0 : (ship.violated ? 0.9 : 0.4)

    // 质量信息（hover 徽章）
    const qinfo = {
      mmsi, name,
      speed: track[0]?.speed || '?',
      violated: ship.violated,
      violationType: ship.violationType || '违规船舶',
      qualityLevel: ship.qualityLevel || 'good',
      cleanedPoints: ship.cleanedPoints || 0,
      dataQualitySuspect: !!ship.dataQualitySuspect,
      onLandPoints: ship.onLandPoints || 0,
      landCrossings: ship.landCrossings || 0,
    }

    // 断段：AIS中断/位置跳变/大间距/陆地 处断开；跳变画红色虚线 leg，间隔画灰色虚线 leg；横穿陆地直接断开不连线
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

    // 历史数据（跳变点还在）画红色虚线跳变段；新清洗数据无跳变点则为空
    jumpLegs.forEach(leg => {
      const jumpLine = new AMap.Polyline({
        path: leg, strokeColor: '#ffb74d', strokeWeight: weight + 1, strokeOpacity: 0.95,
        strokeStyle: 'dashed', lineJoin: 'round', lineCap: 'round',
      })
      jumpLine.setMap(map)
      polylines.push(jumpLine)
    })

    // 数据间隔（无 tag 但间距过大）：灰色虚线示意，不画实线强行连接
    gapLegs.forEach(leg => {
      const gapLine = new AMap.Polyline({
        path: leg, strokeColor: 'rgba(176,190,197,0.7)', strokeWeight: weight, strokeOpacity: 0.55,
        strokeStyle: 'dashed', lineJoin: 'round', lineCap: 'round',
      })
      gapLine.setMap(map)
      polylines.push(gapLine)
    })

    // 落在陆地上的孤立点：画小圆点标记异常，不参与连线
    landPoints.forEach(pt => {
      const marker = new AMap.CircleMarker({
        center: pt, radius: 4, strokeColor: '#a1887f', strokeOpacity: 0.9,
        fillColor: '#ff5722', fillOpacity: 0.9, zIndex: 200,
      })
      marker.setMap(map)
      polylines.push(marker)
    })
  })

  if (allPaths.length > 0) map.setFitView(null, false, [50, 50, 50, 50])
}

// 相邻点间距超过该值(km) → 视为数据间隔（历史数据可能无 tag，几何兜底断段，避免强行连线）
const GAP_KM = 2.0

// 两点球面距离（km）
function haversineKm(a, b) {
  const R = 6371
  const toRad = d => d * Math.PI / 180
  const p1 = toRad(a[1]), p2 = toRad(b[1])
  const dp = toRad(b[1] - a[1]), dl = toRad(b[0] - a[0])
  const h = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(h))
}

// 按质量标记断段：遇 AIS中断/位置跳变 点结束当前段；跳变点额外产出红色虚线 leg（prev→current）
// 无 tag 但相邻点间距 > GAP_KM 时同样断段（历史数据兜底），产出灰色虚线 leg 示意数据间隔
// 陆地规避：p.onLand（点落在陆地）断段+跳过该点；p.crossesLand（连线横穿陆地）直接断段不画连线
function splitTrack(track) {
  const segments = []   // 正常分段（每段是 [lng,lat] 数组）
  const jumpLegs = []   // 跳变 leg（红色虚线）
  const gapLegs = []    // 数据间隔 leg（灰色虚线，几何兜底）
  const landPoints = [] // 落在陆地上的孤立点（不连线，标异常）
  let cur = []
  for (let i = 0; i < track.length; i++) {
    const p = track[i]
    const lnglat = [p.lng, p.lat]
    const tags = p.tags || []
    const hasJump = tags.some(t => String(t).includes('位置跳变'))
    const hasGap = tags.some(t => String(t).includes('AIS中断'))

    // 陆地规避：当前点落在陆地上 → 断段并跳过该点（不强行连线到陆地）
    if (p.onLand) {
      if (cur.length) segments.push(cur)
      landPoints.push(lnglat)
      cur = []
      continue
    }
    // 连线横穿陆地（前一点→当前点）：断段，不画连线（轨迹规避陆地，优先于 tag/gap 判断）
    if (p.crossesLand && cur.length) {
      segments.push(cur)
      cur = []
    } else if ((hasJump || hasGap) && cur.length) {
      if (cur.length >= 1) segments.push(cur)
      if (hasJump) jumpLegs.push([cur[cur.length - 1], lnglat])
      cur = []
    } else if (cur.length && haversineKm(cur[cur.length - 1], lnglat) > GAP_KM) {
      // 历史数据/采样点制造的大间距：不打实线，改用灰色虚线示意
      segments.push(cur)
      gapLegs.push([cur[cur.length - 1], lnglat])
      cur = []
    }
    cur.push(lnglat)
  }
  if (cur.length) segments.push(cur)
  return { segments, jumpLegs, gapLegs, landPoints }
}

// 为分段 polyline 绑定 hover/点击
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
  polyline.on('mouseout', () => { hoverShip.value = null; polyline.setOptions({ strokeWeight: weight, strokeOpacity: opacity }) })
  polyline.on('click', () => { map.setCenter(center); map.setZoom(16) })
}

function clearTracks() { polylines.forEach(p => map?.remove(p)); polylines = [] }
function doSearch() {
  if (highlightTimer) clearTimeout(highlightTimer)
  highlightTimer = setTimeout(() => drawAllTracks(searchText.value), 300)
}

onMounted(() => { initMap(); loadAllTracks() })
onUnmounted(() => {
  if (map) { map.destroy(); map = null }
  if (typeof mqMobile.removeEventListener === 'function') mqMobile.removeEventListener('change', onMqChange)
})
</script>

<style scoped>
.map-container {
    background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow);
    height: calc(100vh - 180px); overflow: hidden; position: relative; z-index: 0;
    border: 1px solid rgba(169, 205, 226, 0.45);
}
.map-container.draw-mode { outline: 3px solid #ff8f5c; outline-offset: -3px; }
.draw-hint {
    position: absolute; top: 16px; left: 50%; transform: translateX(-50%); z-index: 2000;
    background: linear-gradient(135deg, #0a3d62, #08718f);
    color: #fff; padding: 8px 20px; border-radius: 20px; font-size: 13px; white-space: nowrap;
    box-shadow: 0 6px 20px rgba(8, 113, 143, 0.4);
}

.map-overlay-top-left { position: absolute; top: 60px; left: 16px; z-index: 1000; }
.map-legend {
    background: rgba(6, 31, 56, 0.9); padding: 14px 16px; border-radius: var(--radius-sm);
    min-width: 150px; color: #fff; backdrop-filter: blur(8px);
    border: 1px solid rgba(118, 200, 255, 0.22);
}
.map-legend h4 {
    font-size: 13px; margin-bottom: 8px; padding-left: 10px; position: relative;
}
.map-legend h4::before {
    content: ''; position: absolute; left: 0; top: 50%; transform: translateY(-50%);
    width: 3px; height: 13px; border-radius: 2px; background: var(--gradient-teal);
}
.legend-item { display: flex; align-items: center; gap: 8px; font-size: 12px; margin-bottom: 5px; color: rgba(255,255,255,0.75); }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; box-shadow: 0 0 6px currentColor; }

.map-hover-info {
    position: absolute; z-index: 2000; background: rgba(6, 31, 56, 0.94); color: #fff;
    padding: 8px 14px; border-radius: 8px; font-size: 12px; line-height: 1.6; pointer-events: none;
    transform: translate(-50%, -100%); margin-top: -12px; white-space: nowrap;
    border: 1px solid rgba(118, 200, 255, 0.25); backdrop-filter: blur(8px);
}
.map-hover-info strong { display: block; font-size: 13px; }
.map-loading, .map-loaded {
    position: absolute; bottom: 16px; left: 50%; transform: translateX(-50%); z-index: 1000;
    background: rgba(6, 31, 56, 0.85); color: #fff; padding: 6px 20px; border-radius: 20px;
    font-size: 12px; border: 1px solid rgba(118, 200, 255, 0.2); backdrop-filter: blur(8px);
}
.toolbar-hint { font-size: 12px; color: var(--text-secondary); }

/* ========= 区域分析浮动窗口 ========= */
.zone-window {
  position: fixed; z-index: 99999;
  width: 300px;
  max-height: 90vh;
  background: linear-gradient(170deg, rgba(8, 36, 61, 0.96), rgba(6, 31, 56, 0.94));
  backdrop-filter: blur(16px) saturate(1.4);
  border-radius: 14px;
  border: 1px solid rgba(118, 200, 255, 0.25);
  box-shadow: 0 8px 32px rgba(2, 15, 34, 0.55), 0 0 0 1px rgba(255,143,92,0.12);
  color: #fff;
  overflow: hidden;
  user-select: none;
}
.zw-header {
  display: flex; align-items: center; gap: 6px;
  padding: 10px 12px 8px;
  border-bottom: 1px solid rgba(118, 200, 255, 0.14);
  cursor: move;
  touch-action: none;
  background: linear-gradient(90deg, rgba(12,140,233,0.14), rgba(24,184,166,0.06) 60%, transparent);
}
.zw-title { font-size: 13px; font-weight: 700; flex-shrink: 0; }
.zw-stats { font-size: 11px; color: rgba(150, 210, 245, 0.75); flex: 1; text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.zw-zoom-btns { display: flex; gap: 2px; flex-shrink: 0; }
.zw-zbtn {
  background: rgba(118, 200, 255, 0.1); border: 1px solid rgba(118, 200, 255, 0.18);
  color: rgba(200, 230, 255, 0.8); font-size: 12px; width: 22px; height: 22px;
  border-radius: 5px; cursor: pointer; display: flex; align-items: center; justify-content: center;
  transition: all 0.15s;
}
.zw-zbtn:hover { background: rgba(12, 140, 233, 0.35); color: #fff; }
.zw-close { background: none; border: none; font-size: 18px; color: rgba(180, 220, 250, 0.5); cursor: pointer; line-height: 1; padding: 0 2px; }
.zw-close:hover { color: #fff; }
.zw-loading { text-align: center; padding: 30px 0; color: rgba(180, 220, 250, 0.6); font-size: 13px; }

.zw-body {
  padding: 8px 12px 12px;
  overflow: auto;
  max-height: calc(90vh - 50px);
  transition: transform 0.2s ease;
}

.zw-chart-section { margin-bottom: 8px; }
.zw-chart-label { font-size: 10px; font-weight: 600; color: rgba(150, 210, 245, 0.75); text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 4px; }
.zw-axis-hint { font-size: 9px; font-weight: 400; color: rgba(150, 210, 245, 0.45); text-transform: none; letter-spacing: 0; }
.zw-chart-wrap { background: rgba(12, 140, 233, 0.07); border: 1px solid rgba(118, 200, 255, 0.1); border-radius: 8px; padding: 6px; }

.zw-polar { width: 100%; height: auto; display: block; }
.zw-bar { width: 100%; height: auto; display: block; }

.zw-summary {
  display: flex; gap: 12px; justify-content: center;
  padding: 6px 0 2px;
  font-size: 11px; color: rgba(150, 210, 245, 0.75);
  border-top: 1px solid rgba(118, 200, 255, 0.12);
  margin-top: 4px;
}

.slide-up-enter-active, .slide-up-leave-active { transition: all 0.3s ease; }
.slide-up-enter-from, .slide-up-leave-to { transform: translateY(20px); opacity: 0; }

/* ── 移动端适配 ── */
@media (max-width: 768px) {
    .map-container {
        height: calc(100dvh - 150px);
        min-height: 360px;
    }
    /* 区域窗口：全宽 - 24px，置于标签栏上方、不可拖 */
    .zone-window.is-mobile {
        width: calc(100vw - 24px) !important;
        max-height: 65vh !important;
        left: 12px !important;
        top: calc(var(--tabbar-height) + var(--tabbar-safe-bottom) + 12px) !important;
        right: auto !important;
    }
    .draw-hint {
        top: 10px; left: 50%; transform: translateX(-50%);
        width: calc(100% - 24px); text-align: center; white-space: normal;
        font-size: 12px; padding: 6px 12px; line-height: 1.5;
    }
    .map-overlay-top-left { top: 74px; left: 12px; }
    .map-legend { padding: 10px 12px; min-width: 0; }
    .map-hover-info { max-width: 70vw; white-space: normal; }
}
</style>
