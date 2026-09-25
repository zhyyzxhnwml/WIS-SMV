<template>
  <div class="monitor-page">
    <PageHeader icon="radar" title="级联判断后可视化" subtitle="L1 候选事件 · L2 ML 路由 · L3 LLM 判定（全部以船舶为单位）">
      <div class="layer-toggle">
        <button :class="['btn', layer==='l3' ? 'btn-primary' : 'btn-secondary']" @click="switchLayer('l3')">L3 判定</button>
        <button :class="['btn', layer==='l2' ? 'btn-primary' : 'btn-secondary']" @click="switchLayer('l2')">L2 路由</button>
        <button :class="['btn', layer==='l1' ? 'btn-primary' : 'btn-secondary']" @click="switchLayer('l1')">L1 事件</button>
      </div>
      <button class="btn btn-primary" @click="reload"><AppIcon name="refresh-cw" inline /> 刷新</button>
    </PageHeader>

    <div class="toolbar">
      <div class="toolbar-left">
        <input type="text" class="search-input" placeholder="搜索MMSI高亮显示..." v-model="searchText" @input="doSearch">
        <span class="toolbar-hint">共 <strong>{{ shipTotal }}</strong> 艘 · 违规 <strong>{{ summary.l3Verdict.violated || 0 }}</strong> 艘 · 正常 <strong>{{ summary.l3Verdict.normal || 0 }}</strong> 艘</span>
      </div>
    </div>

    <div class="map-container" ref="mapContainer">
      <div class="map-overlay-top-left">
        <div class="map-legend">
          <h4>图例（{{ layer === 'l3' ? 'L3 判定' : layer === 'l2' ? 'L2 路由' : 'L1 事件' }}）</h4>
          <template v-if="layer === 'l3'">
            <div class="legend-item"><span class="dot" style="background:var(--violating-color,#ea4335);"></span> 违规</div>
            <div class="legend-item"><span class="dot" style="background:var(--normal-color,#0f9d58);"></span> 正常</div>
            <div class="legend-item"><span class="dot" style="background:#f59e0b;"></span> 高风险</div>
            <div class="legend-item"><span class="dot" style="background:#ff9800;"></span> 中风险</div>
          </template>
          <template v-else-if="layer === 'l2'">
            <div class="legend-item"><span class="dot" style="background:#0c8ce9;"></span> obvious_issue</div>
            <div class="legend-item"><span class="dot" style="background:#18b8a6;"></span> has_risk</div>
            <div class="legend-item"><span class="dot" style="background:#7c6ff0;"></span> needs_review</div>
            <div class="legend-item"><span class="dot" style="background:#0f9d58;"></span> normal</div>
          </template>
          <template v-else>
            <div class="legend-item"><span class="dot" style="background:#ea4335;"></span> high 优先级事件</div>
            <div class="legend-item"><span class="dot" style="background:#ffb74d;"></span> middle 优先级事件</div>
            <div class="legend-item"><span class="dot" style="background:#90a4ae;"></span> 无事件</div>
          </template>
        </div>
      </div>
      <div v-if="hoverShip" class="map-hover-info" :style="hoverStyle">
        <strong>{{ hoverShip.mmsi }}</strong>
        <span v-if="layer==='l3'">{{ hoverShip.l3Label }} · 风险 {{ hoverShip.riskLabel }} · 置信度 {{ (hoverShip.confidence*100).toFixed(0) }}%</span>
        <span v-if="layer==='l2'">{{ hoverShip.l2Category }} · 路由 {{ hoverShip.l2Route }} · 置信度 {{ (hoverShip.l2Conf*100).toFixed(0) }}%</span>
        <span v-if="layer==='l1'">{{ hoverShip.l1Events }}</span>
      </div>
      <div class="map-loading" v-if="loading"><AppIcon name="hourglass" inline /> 加载数据中...</div>
      <div class="map-loaded" v-else><AppIcon name="check-circle" inline /> {{ trackCount }} 条轨迹</div>
    </div>

    <!-- 船舶单位统计 -->
    <div class="stats-row">
      <div class="card">
        <div class="card-header"><h3>L1 事件类型（船舶数）</h3><span class="card-subtitle">加速器检测的候选事件</span></div>
        <div class="card-body chart-box"><div ref="l1ChartEl" class="chart-container"></div></div>
      </div>
      <div class="card">
        <div class="card-header"><h3>L3 风险等级（船舶数）</h3><span class="card-subtitle">确定性分级引擎判定</span></div>
        <div class="card-body chart-box"><div ref="riskChartEl" class="chart-container"></div></div>
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
import { fetchCascadeTracks } from '../../api/ships.js'

const { mapContainer, initMap, destroyMap, drawShips, hoverShip, hoverStyle, trackCount } = useTrackMap()

const ships = ref([])
const summary = ref({ shipTotal: 0, l1EventTypes: [], l2Routes: [], l3Verdict: { normal: 0, violated: 0 }, l3Risk: [] })
const shipTotal = ref(0)
const layer = ref('l3')
const searchText = ref('')
const loading = ref(true)
let l1Chart = null, riskChart = null

function colorOf(ship, c) {
  if (layer.value === 'l3') {
    const r = ship.l3?.riskLevel
    if (ship.l3?.violated) {
      if (r === 'high') return { color: '#f59e0b', weight: 3, opacity: 0.95 }
      if (r === 'mid') return { color: '#ff9800', weight: 3, opacity: 0.9 }
      return { color: c.violating || '#ea4335', weight: 3, opacity: 0.9 }
    }
    return { color: c.normal || '#0f9d58', weight: 2, opacity: 0.4 }
  }
  if (layer.value === 'l2') {
    const rt = ship.l2?.routing || ''
    const map = { obvious_issue: '#0c8ce9', has_risk: '#18b8a6', needs_review: '#7c6ff0' }
    return { color: map[rt] || c.normal || '#0f9d58', weight: 2, opacity: 0.6 }
  }
  // L1: 有 high 优先级事件 → 红，有 middle → 橙，无事件 → 灰
  const evs = ship.l1?.events || []
  if (evs.some(e => e.priority === 'high')) return { color: '#ea4335', weight: 3, opacity: 0.9 }
  if (evs.length) return { color: '#ffb74d', weight: 2, opacity: 0.7 }
  return { color: '#90a4ae', weight: 2, opacity: 0.4 }
}
function infoOf(ship) {
  const riskMap = { high: '高', mid: '中', low: '低' }
  return {
    mmsi: ship.mmsi,
    l3Label: ship.l3?.violationLabel || '正常',
    riskLabel: riskMap[ship.l3?.riskLevel] || '低',
    confidence: ship.l3?.confidence || 0,
    l2Category: ship.l2?.mlCategory || '-',
    l2Route: ship.l2?.routing || '-',
    l2Conf: ship.l2?.mlConfidence || 0,
    l1Events: (ship.l1?.events || []).map(e => e.typeLabel).filter(Boolean).join('、') || '无事件',
  }
}

async function reload() {
  loading.value = true
  try {
    const data = await fetchCascadeTracks()
    summary.value = data.summary
    ships.value = data.ships
    shipTotal.value = data.summary.shipTotal || (data.ships?.length || 0)
    await nextTick()
    drawShips(ships.value, { colorOf, infoOf, filterText: searchText.value })
    buildCharts()
  } catch (e) { console.error('cascade tracks load failed', e) }
  finally { loading.value = false }
}
function doSearch() { drawShips(ships.value, { colorOf, infoOf, filterText: searchText.value }) }
function switchLayer(L) { layer.value = L; drawShips(ships.value, { colorOf, infoOf, filterText: searchText.value }) }

function buildCharts() {
  const l1 = summary.value.l1EventTypes || []
  if (!l1Chart) l1Chart = echarts.init(l1ChartEl.value)
  l1Chart.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 130, right: 20, top: 10, bottom: 20 },
    xAxis: { type: 'value', minInterval: 1 },
    yAxis: { type: 'category', data: l1.map(t => t.label) },
    series: [{ type: 'bar', data: l1.map(t => t.ships), itemStyle: { color: '#7c6ff0' } }],
  })
  const risk = summary.value.l3Risk || []
  const riskLabelMap = { high: '高风险', mid: '中风险', low: '低风险' }
  if (!riskChart) riskChart = echarts.init(riskChartEl.value)
  riskChart.setOption({
    tooltip: {},
    grid: { left: 70, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: risk.map(r => riskLabelMap[r.level] || r.level) },
    yAxis: { type: 'value', name: '船舶数' },
    series: [{ type: 'bar', data: risk.map(r => r.ships),
      itemStyle: { color: p => ['#ea4335', '#ff9800', '#0f9d58'][p.dataIndex] || '#0c8ce9' } }],
  })
}

const l1ChartEl = ref(null), riskChartEl = ref(null)

onMounted(async () => { await initMap(); await reload() })
onUnmounted(() => { destroyMap(); l1Chart?.dispose(); riskChart?.dispose() })
</script>

<style scoped>
.monitor-page { display: flex; flex-direction: column; gap: 16px; }
.layer-toggle { display: flex; gap: 4px; }
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
