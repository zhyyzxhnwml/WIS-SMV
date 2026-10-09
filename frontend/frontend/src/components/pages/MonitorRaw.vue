<template>
  <div class="monitor-page">
    <PageHeader icon="upload" title="第 1 步 · 原始数据" subtitle="清洗前的轨迹点（噪声高亮）。这是流水线的起点，后面每一步都会把它淡化保留，只做加法">
      <button class="btn btn-primary" @click="reload"><AppIcon name="refresh-cw" inline /> 刷新</button>
    </PageHeader>

    <div class="toolbar">
      <div class="toolbar-left">
        <input type="text" class="search-input" placeholder="搜索MMSI高亮显示..." v-model="searchText" @input="doSearch">
        <span class="toolbar-hint">共 <strong>{{ summary.shipTotal }}</strong> 艘 · 有噪声 <strong>{{ summary.noisyShips }}</strong> 艘</span>
      </div>
    </div>

    <div class="map-container" ref="mapContainer">
      <div class="map-overlay-top-left">
        <div class="map-legend">
          <h4>图例</h4>
          <div class="legend-item"><span class="dot" style="background:rgba(170,170,170,0.75);"></span> 原始轨迹</div>
          <div class="legend-item"><span class="dot" style="background:#ff9800;"></span> 噪声点（跳变/重复/时间异常）</div>
          <div class="legend-item"><span class="dot" style="background:#ff5722;"></span> 陆地点</div>
          <div class="legend-item"><span class="dot" style="background:#ffb74d;border-radius:0;width:14px;"></span> 位置跳变段（虚线）</div>
        </div>
      </div>
      <div v-if="hoverShip" class="map-hover-info" :style="hoverStyle">
        <strong>{{ hoverShip.mmsi }}</strong>
        <span>原始 {{ hoverShip.originalPts }} 点 · 保留 {{ hoverShip.retainedPts }} 点</span>
        <span v-if="hoverShip.dupPts">重复点 {{ hoverShip.dupPts }}</span>
        <span v-if="hoverShip.invalidPts">剔除 {{ hoverShip.invalidPts }} 点（{{ hoverShip.removalPct }}）</span>
      </div>
      <div class="map-loading" v-if="loading"><AppIcon name="hourglass" inline /> 加载数据中...</div>
      <div class="map-loaded" v-else><AppIcon name="check-circle" inline /> {{ shipList.length }} 条原始轨迹</div>
    </div>

    <!-- 船舶单位清洗统计 -->
    <div class="stats-row">
      <div class="card">
        <div class="card-header"><h3>质量问题涉及的船舶数</h3><span class="card-subtitle">按问题类型（不是按点）</span></div>
        <div class="card-body chart-box"><div ref="issueChartEl" class="chart-container"></div></div>
      </div>
      <div class="card">
        <div class="card-header"><h3>单船剔除率分布</h3><span class="card-subtitle">每船被清洗剔除的异常点占比</span></div>
        <div class="card-body chart-box"><div ref="rateChartEl" class="chart-container"></div></div>
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
import { getRawTracks } from '../../store/pipeline.js'

const { mapContainer, initMap, destroyMap, drawShips, drawPoints, hoverShip, hoverStyle } = useTrackMap()

const shipList = ref([])
const summary = ref({ shipTotal: 0, noisyShips: 0, hasRawShips: 0, issueShipCounts: [], removalRateBins: [] })
const searchText = ref('')
const loading = ref(true)
let issueChart = null, rateChart = null

function colorOf(ship) {
  if (!ship.hasRaw) return { color: '#cccccc', weight: 1, opacity: 0.25 }
  if (ship.stats.invalidPoints > 0) return { color: '#ff9800', weight: 2, opacity: 0.8 }
  return { color: 'rgba(170,170,170,0.7)', weight: 1, opacity: 0.6 }
}
function infoOf(ship) {
  const rr = ship.stats.removalRate
  return {
    mmsi: ship.mmsi,
    originalPts: ship.stats.originalPoints,
    retainedPts: ship.stats.retainedPoints,
    dupPts: ship.stats.duplicatePoints,
    invalidPts: ship.stats.invalidPoints,
    removalPct: rr > 0 ? (rr * 100).toFixed(1) + '%' : '0%',
  }
}

async function reload() {
  loading.value = true
  try {
    // 走 store：这一步是流水线起点，拉到的数据后面几步要复用（淡化铺底）
    const data = await getRawTracks()
    summary.value = data.summary
    shipList.value = data.ships
    await nextTick()
    drawShips(shipList.value, { colorOf, infoOf, filterText: searchText.value })
    // 噪声点高亮：所有有 flags 的点画小圆点
    const noise = []
    for (const s of shipList.value) {
      for (const p of s.rawTrack) {
        if (p.flags?.length) {
          noise.push({ lng: p.lng, lat: p.lat, color: p.flags.some(f => f.includes('on_land')) ? '#ff5722' : '#ff9800' })
        }
      }
    }
    drawPoints(noise, { radius: 4, opacity: 0.85 })
    buildCharts()
  } catch (e) { console.error('raw tracks load failed', e) }
  finally { loading.value = false }
}

function doSearch() {
  drawShips(shipList.value, { colorOf, infoOf, filterText: searchText.value })
}

function buildCharts() {
  // 质量问题 → 船舶数条形
  const issues = summary.value.issueShipCounts || []
  if (!issueChart) issueChart = echarts.init(issueChartEl.value)
  issueChart.setOption({
    tooltip: { trigger: 'axis' },
    // 横向条形图：数值轴是 x → 轴名给 x；y 是问题类型名，本身自带说明，不加轴名
    // inverse: 接口按 ships 降序返回，横向条形图的类目默认自下而上，
    // 不反转的话最大的那根会落在最底下，读起来是倒序
    grid: { left: 100, right: 24, top: 16, bottom: 48 },
    xAxis: { type: 'value', minInterval: 1, name: '船舶数（艘）',
      nameLocation: 'middle', nameGap: 28,
      nameTextStyle: { fontSize: 11, color: '#5d7c92' }, axisLabel: { color: '#5d7c92' } },
    yAxis: { type: 'category', inverse: true, data: issues.map(i => i.label), axisLabel: { color: '#5d7c92' } },
    // 字段是 ships 不是 ship —— 原来取 i.ship 恒为 undefined，
    // 这张图画出来一根柱子都没有（同页右侧那张取 b.count 是对的，所以只有这张空）
    series: [{ type: 'bar', data: issues.map(i => i.ships), itemStyle: { color: '#ff9800' } }],
  })
  // 剔除率分布 箱形图
  const bins = summary.value.removalRateBins || []
  if (!rateChart) rateChart = echarts.init(rateChartEl.value)
  rateChart.setOption({
    tooltip: {},
    // top 留 36 给 y 轴名：ECharts 把 yAxis.name 画在轴顶端，原来 top:20 会被裁掉
    grid: { left: 56, right: 20, top: 36, bottom: 58 },
    xAxis: { type: 'category', data: bins.map(b => b.label), name: '剔除率区间',
      nameLocation: 'middle', nameGap: 30,
      nameTextStyle: { fontSize: 11, color: '#5d7c92' }, axisLabel: { color: '#5d7c92' } },
    yAxis: { type: 'value', name: '船舶数（艘）', nameGap: 14,
      nameTextStyle: { fontSize: 11, color: '#5d7c92', align: 'left' }, axisLabel: { color: '#5d7c92' } },
    series: [{ type: 'bar', data: bins.map(b => b.count), itemStyle: { color: '#18b8a6' } }],
  })
}

const issueChartEl = ref(null), rateChartEl = ref(null)

onMounted(async () => {
  await initMap()
  await reload()
})
onUnmounted(() => { destroyMap(); issueChart?.dispose(); rateChart?.dispose() })
</script>

<style scoped>
.monitor-page { display: flex; flex-direction: column; gap: 16px; }
.toolbar { display: flex; align-items: center; gap: 12px; padding: 10px 14px; background: var(--gradient-card); border: 1px solid rgba(169, 205, 226, 0.4); border-radius: var(--radius); box-shadow: var(--shadow); }
.toolbar-left { display: flex; align-items: center; gap: 12px; flex: 1; }
.toolbar-hint { font-size: 12px; color: var(--text-secondary); }
.search-input { padding: 6px 12px; border: 1px solid rgba(169, 205, 226, 0.45); border-radius: 8px; font-size: 13px; width: 240px; background: #fff; }
.map-container { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); height: 420px; overflow: hidden; position: relative; z-index: 0; border: 1px solid rgba(169, 205, 226, 0.45); }
.map-overlay-top-left { position: absolute; top: 60px; left: 16px; z-index: 1000; }
.map-legend { background: rgba(6, 31, 56, 0.9); padding: 14px 16px; border-radius: var(--radius-sm); min-width: 160px; color: #fff; backdrop-filter: blur(8px); border: 1px solid rgba(118, 200, 255, 0.22); }
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
