<template>
  <div class="monitor-page">
    <PageHeader icon="check-circle" title="第 2 步 · 数据清洗后" subtitle="上一步的原始轨迹与噪声点淡化保留，本步叠加上清洗后保留的轨迹">
      <button class="btn btn-primary" @click="reload"><AppIcon name="refresh-cw" inline /> 刷新</button>
    </PageHeader>

    <div class="toolbar">
      <div class="toolbar-left">
        <input type="text" class="search-input" placeholder="搜索MMSI高亮显示..." v-model="searchText" @input="doSearch">
        <span class="toolbar-hint">共 <strong>{{ shipTotal }}</strong> 艘</span>
      </div>
    </div>

    <div class="map-container" ref="mapContainer">
      <div class="map-overlay-top-left">
        <div class="map-legend">
          <h4>图例</h4>
          <div class="legend-item" style="opacity:0.55;"><span class="dot" style="background:#b0bec5;width:14px;border-radius:0;"></span> 上一步：原始轨迹</div>
          <div class="legend-item" style="opacity:0.55;"><span class="dot" style="background:#ff9800;"></span> 上一步：噪声点</div>
          <div class="legend-item" style="margin-bottom:7px;padding-bottom:7px;border-bottom:1px solid rgba(255,255,255,0.15);">
            <span class="dot" style="background:#fff;"></span> <strong>本步：保留轨迹 ↓</strong>
          </div>
          <div class="legend-item"><span class="dot" style="background:var(--normal-color,#0f9d58);"></span> 正常航行</div>
          <div class="legend-item"><span class="dot" style="background:var(--violating-color,#ea4335);"></span> 违规船舶</div>
          <div class="legend-item"><span class="dot" style="background:#ffb74d;border-radius:0;width:14px;"></span> 位置跳变段（虚线）</div>
          <div class="legend-item"><span class="dot" style="background:#4fc3f7;border-radius:0;width:14px;"></span> AIS中断断点</div>
          <div class="legend-item"><span class="dot" style="background:rgba(176,190,197,0.7);border-radius:0;width:14px;"></span> 数据间隔（灰虚线）</div>
          <div class="legend-item"><span class="dot" style="background:rgba(141,110,99,0.8);border-radius:0;width:14px;"></span> 横穿陆地段（灰虚线）</div>
          <div class="legend-item"><span class="dot" style="background:#ff5722;"></span> 陆地点（异常）</div>
        </div>
      </div>
      <div v-if="hoverShip" class="map-hover-info" :style="hoverStyle">
        <strong>{{ hoverShip.mmsi }}</strong>
        <span>速度 {{ hoverShip.speed }} kn</span>
        <span v-if="hoverShip.violated" style="color:#ff6b6b;">{{ hoverShip.violationType }}</span>
        <span v-if="hoverShip.qualityLevel">质量: {{ qlLabel(hoverShip.qualityLevel) }} · 剔除 {{ hoverShip.cleanedPoints }}</span>
        <span v-if="hoverShip.onLandPoints" style="color:#ffb74d;">陆地点 {{ hoverShip.onLandPoints }} · 横穿 {{ hoverShip.landCrossings }}</span>
      </div>
      <div class="map-loading" v-if="loading"><AppIcon name="hourglass" inline /> 加载数据中...</div>
      <div class="map-loaded" v-else><AppIcon name="check-circle" inline /> 本步保留 {{ ships.length }} 条轨迹 · 底层淡化 {{ rawShips.length }} 条</div>
    </div>

    <div class="stats-row">
      <div class="card">
        <div class="card-header"><h3>清洗前后点数对比（按船舶）</h3><span class="card-subtitle">原始 vs 保留点 · 船舶单位均值</span></div>
        <div class="card-body chart-box"><div ref="compareChartEl" class="chart-container"></div></div>
      </div>
      <div class="card">
        <div class="card-header"><h3>质量等级分布（按船舶）</h3><span class="card-subtitle">good / fair / poor</span></div>
        <div class="card-body chart-box"><div ref="qlChartEl" class="chart-container"></div></div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import * as echarts from 'echarts'
import PageHeader from '../../components/PageHeader.vue'
import AppIcon from '../../components/AppIcon.vue'
import { useTrackMap } from '../../composables/useTrackMap.js'
import { getRawTracks, getAllTracks, byMmsi } from '../../store/pipeline.js'

const { mapContainer, initMap, destroyMap, drawLayer, drawPoints, clearTracks,
        fitOrKeep, hoverShip, hoverStyle } = useTrackMap()

const rawShips = ref([])   // 上一步（原始数据）的船，作为淡化底层
const ships = ref([])
const shipTotal = ref(0)
const searchText = ref('')
const loading = ref(true)
const qlStats = ref({})
let compareChart = null, qlChart = null

function colorOf(ship, c) {
  // all-tracks 已经把速度/违规折算成 color 了（violating/red/yellow/green），直接用
  if (ship.violated || ship.color === 'violating') return { color: c.violating || '#ea4335', weight: 3, opacity: 0.9 }
  if (ship.color === 'red') return { color: '#ea4335', weight: 2, opacity: 0.8 }
  if (ship.color === 'yellow') return { color: c.anchored || '#f59e0b', weight: 2, opacity: 0.8 }
  return { color: c.normal || '#0f9d58', weight: 2, opacity: 0.4 }
}
function infoOf(ship) {
  return {
    mmsi: ship.mmsi, name: ship.name,
    speed: ship.track?.[0]?.speed || '?',
    violated: ship.violated, violationType: ship.violationType || '违规船舶',
    qualityLevel: ship.qualityLevel || 'good',
    cleanedPoints: ship.cleanedPoints || 0,
    dataQualitySuspect: !!ship.dataQualitySuspect,
    onLandPoints: ship.onLandPoints || 0, landCrossings: ship.landCrossings || 0,
  }
}

async function reload() {
  loading.value = true
  try {
    // 累积叠加：本步要叠在「原始数据」之上，所以原始轨迹和噪声点也得拿到
    // （store 里按 promise 记忆，上一步已经拉过就直接复用，不会重复请求）
    //
    // 本步的轨迹取 /ships/all-tracks，不能用 /ships：后者既不带 track 字段
    // （画不出线），又多取一处 pageSize 默认只有 50 的分页，只画得出前 50 条船。
    const [raw, list] = await Promise.all([getRawTracks(), getAllTracks()])
    rawShips.value = raw?.ships || []
    ships.value = list || []
    shipTotal.value = ships.value.length
    await nextTick()
    drawCumulative()
    computeQualityStats()
    buildCharts()
  } catch (e) { console.error('cleaned tracks load failed', e) }
  finally { loading.value = false }
}

/**
 * 累积叠加：先铺一层上一步的原始轨迹与噪声点（淡化），
 * 再把本步保留的轨迹画在上面 —— 一眼能看出这一步把哪些点剔掉了。
 */
function drawCumulative() {
  clearTracks()
  if (rawShips.value.length) {
    drawLayer(rawShips.value, {
      colorOf: () => ({ color: '#b0bec5', weight: 1, opacity: 0.16 }),
      zIndex: 1, count: false,
      showJumpLegs: false, showGapLegs: false, showLandPoints: false,
    })
    const noise = []
    for (const s of rawShips.value) {
      for (const p of (s.rawTrack || [])) {
        if (p.flags?.length) {
          noise.push({ lng: p.lng, lat: p.lat,
            color: p.flags.some(f => f.includes('on_land')) ? '#ff5722' : '#ff9800' })
        }
      }
    }
    drawPoints(noise, { radius: 3, opacity: 0.3, zIndex: 2 })
  }
  drawLayer(ships.value, { colorOf, infoOf, filterText: searchText.value, zIndex: 6 })
  fitOrKeep()
}

function doSearch() { drawCumulative() }

function qlLabel(l) { return l === 'good' ? '良好' : l === 'fair' ? '一般' : '较差' }

function computeQualityStats() {
  const counts = { good: 0, fair: 0, poor: 0 }
  let origSum = 0, retSum = 0, n = 0
  // 点数对比是船舶单位的：原始/保留点数在 /ships/raw-tracks 的每船 stats 里，
  // 按 mmsi 对到本步的船上（两边都来自同一份检测结果，mmsi 能对上）
  const rawBy = byMmsi(rawShips.value)
  for (const s of ships.value) {
    const ql = s.qualityLevel || 'good'
    counts[ql in counts ? ql : 'good']++
    const st = rawBy.get(String(s.mmsi))?.stats
    origSum += st?.originalPoints || 0
    retSum += st?.retainedPoints || 0
    n++
  }
  qlStats.value = {
    counts, n,
    avgOrig: n ? Math.round(origSum / n) : 0,
    avgRet: n ? Math.round(retSum / n) : 0,
  }
}

function buildCharts() {
  if (!compareChart) compareChart = echarts.init(compareChartEl.value)
  compareChart.setOption({
    tooltip: {},
    // top 留 36 给 y 轴名（默认画在轴顶端，顶部不留位会被裁掉）；点数可达 4 位，left 给够
    grid: { left: 64, right: 20, top: 36, bottom: 58 },
    xAxis: { type: 'category', data: ['原始点数均值', '保留点数均值'], name: '统计口径',
      nameLocation: 'middle', nameGap: 30,
      nameTextStyle: { fontSize: 11, color: '#5d7c92' }, axisLabel: { color: '#5d7c92' } },
    yAxis: { type: 'value', name: '轨迹点数（点）', nameGap: 14,
      nameTextStyle: { fontSize: 11, color: '#5d7c92', align: 'left' }, axisLabel: { color: '#5d7c92' } },
    series: [{
      type: 'bar',
      data: [qlStats.value.avgOrig, qlStats.value.avgRet],
      itemStyle: { color: params => params.dataIndex === 0 ? '#90a4ae' : '#18b8a6' },
    }],
  })
  const ql = qlStats.value.counts
  if (!qlChart) qlChart = echarts.init(qlChartEl.value)
  qlChart.setOption({
    tooltip: {},
    grid: { left: 56, right: 20, top: 36, bottom: 58 },
    xAxis: { type: 'category', data: ['良好(good)', '一般(fair)', '较差(poor)'], name: '质量等级',
      nameLocation: 'middle', nameGap: 30,
      nameTextStyle: { fontSize: 11, color: '#5d7c92' }, axisLabel: { color: '#5d7c92' } },
    yAxis: { type: 'value', name: '船舶数（艘）', nameGap: 14,
      nameTextStyle: { fontSize: 11, color: '#5d7c92', align: 'left' }, axisLabel: { color: '#5d7c92' } },
    series: [{ type: 'bar', data: [ql.good, ql.fair, ql.poor], itemStyle: { color: '#0c8ce9' } }],
  })
}

const compareChartEl = ref(null), qlChartEl = ref(null)

onMounted(async () => { await initMap(); await reload() })
onUnmounted(() => { destroyMap(); compareChart?.dispose(); qlChart?.dispose() })
</script>

<style scoped>
.monitor-page { display: flex; flex-direction: column; gap: 16px; }
.toolbar { display: flex; align-items: center; gap: 12px; padding: 10px 14px; background: var(--gradient-card); border: 1px solid rgba(169, 205, 226, 0.4); border-radius: var(--radius); box-shadow: var(--shadow); }
.toolbar-left { display: flex; align-items: center; gap: 12px; flex: 1; }
.toolbar-hint { font-size: 12px; color: var(--text-secondary); }
.search-input { padding: 6px 12px; border: 1px solid rgba(169, 205, 226, 0.45); border-radius: 8px; font-size: 13px; width: 240px; background: #fff; }
.map-container { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); height: 420px; overflow: hidden; position: relative; z-index: 0; border: 1px solid rgba(169, 205, 226, 0.45); }
.map-overlay-top-left { position: absolute; top: 60px; left: 16px; z-index: 1000; }
.map-legend { background: rgba(6, 31, 56, 0.9); padding: 14px 16px; border-radius: var(--radius-sm); min-width: 150px; color: #fff; backdrop-filter: blur(8px); border: 1px solid rgba(118, 200, 255, 0.22); }
.map-legend h4 { font-size: 13px; margin-bottom: 8px; padding-left: 10px; position: relative; }
.map-legend h4::before { content: ''; position: absolute; left: 0; top: 50%; transform: translateY(-50%); width: 3px; height: 13px; border-radius: 2px; background: var(--gradient-teal); }
.legend-item { display: flex; align-items: center; gap: 8px; font-size: 12px; margin-bottom: 5px; color: rgba(255,255,255,0.75); }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.map-hover-info { position: absolute; z-index: 2000; background: rgba(6, 31, 56, 0.94); color: #fff; padding: 8px 14px; border-radius: 8px; font-size: 12px; line-height: 1.6; pointer-events: none; transform: translate(-50%, -100%); margin-top: -12px; white-space: nowrap; border: 1px solid rgba(118, 200, 255, 0.25); }
.map-hover-info strong { display: block; font-size: 13px; }
.map-loading, .map-loaded { position: absolute; bottom: 16px; left: 50%; transform: translateX(-50%); z-index: 1000; background: rgba(6, 31, 56, 0.85); color: #fff; padding: 6px 20px; border-radius: 20px; font-size: 12px; border: 1px solid rgba(118, 200, 255, 0.2); }
.stats-row { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
.chart-box { min-height: 260px; }
.chart-container { width: 100%; height: 260px; }
@media (max-width: 768px) { .stats-row { grid-template-columns: 1fr; } .map-container { height: 320px; } .search-input { width: 100%; } }
</style>
