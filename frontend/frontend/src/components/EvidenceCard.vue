<template>
  <div class="ev-card">
    <!-- ① 判定卡片 -->
    <div class="ev-verdict" :class="data.violated ? 'is-violation' : 'is-normal'">
      <div class="ev-ship">
        <span class="ev-mmsi">MMSI {{ data.mmsi }}</span>
        <span v-if="data.ship_type" class="ev-ship-type">{{ data.ship_type }}</span>
      </div>
      <div class="ev-verdict-row">
        <span class="ev-tag" :class="'risk-' + (data.verdict?.risk_level || 'low')">
          {{ data.violated ? (data.verdict?.violation_label || data.verdict?.violation_type || '疑似违规') : '正常航行' }}
        </span>
        <span class="ev-badge" :class="'risk-' + (data.verdict?.risk_level || 'low')">
          {{ riskText(data.verdict?.risk_level) }}
        </span>
        <span v-if="data.violated" class="ev-conf">置信度 {{ fmtPct(data.verdict?.confidence) }}</span>
        <span v-if="data.verdict?.quality_level" class="ev-quality">AIS质量 {{ data.verdict.quality_level }}</span>
      </div>
      <div v-if="data.violated && data.stats" class="ev-stats">
        <span>全程均速 {{ fmt1(data.stats.avg_speed) }} 节</span>
        <span>最高 {{ fmt1(data.stats.max_speed) }} 节</span>
        <span>轨迹 {{ data.stats.track_points }} 点</span>
      </div>
      <div v-if="data.annotations?.length" class="ev-anns">L1 打标：{{ data.annotations.join('、') }}</div>
    </div>

    <!-- ② 轨迹图 -->
    <div v-if="data.track?.length" class="ev-section">
      <div class="ev-section-title">航行轨迹</div>
      <div ref="chartEl" class="ev-chart"></div>
      <div class="ev-legend">
        <span class="lg"><i class="ic ic-line"></i>轨迹（全程首→尾）</span>
        <span class="lg"><i class="ic ic-green"></i>轨迹起点</span>
        <span class="lg"><i class="ic ic-blue"></i>轨迹终点</span>
        <span class="lg"><i class="ic ic-red"></i>违规事件点</span>
        <span v-if="hasGap" class="lg"><i class="ic ic-yellow"></i>中断前最后信号</span>
        <span v-if="hasGap" class="lg"><i class="ic ic-dash"></i>信号中断期（无回传）</span>
      </div>
    </div>

    <!-- ③ 事件表格 -->
    <div v-if="data.events?.length" class="ev-section">
      <div class="ev-section-title">违规事件（{{ data.events.length }}）</div>
      <div class="ev-table-wrap">
        <table class="ev-table">
          <thead>
            <tr>
              <th>时间区间</th>
              <th>事件类型</th>
              <th>区域</th>
              <th>证据点</th>
              <th>均速</th>
              <th>最高速</th>
              <th>持续</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(ev, i) in data.events" :key="i">
              <td>{{ ev.start_time }}<br /><small>~ {{ ev.end_time }}</small></td>
              <td>{{ ev.type_label || ev.type }}</td>
              <td>{{ ev.zone_name }}</td>
              <td>{{ (ev.evidence_points || []).length }}</td>
              <td>{{ fmt1(ev.avg_speed) }} 节</td>
              <td>{{ fmt1(ev.max_speed) }} 节</td>
              <td>{{ fmt0(ev.duration_minutes) }} 分</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- ④ 法规引用 / LLM 复核 / 建议处置 -->
    <div v-if="data.regulation" class="ev-section">
      <div class="ev-section-title">法规引用</div>
      <p class="ev-text">{{ data.regulation }}</p>
    </div>
    <div v-if="data.final_assessment" class="ev-section">
      <div class="ev-section-title">LLM 复核</div>
      <p class="ev-text">{{ data.final_assessment }}</p>
    </div>
    <div v-if="data.recommended_action" class="ev-section ev-action">
      <span class="ev-action-label">建议处置</span>
      <span>{{ data.recommended_action }}</span>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import * as echarts from 'echarts'

const props = defineProps({
  data: { type: Object, required: true }
})

const chartEl = ref(null)
let chart = null
let ro = null

// 是否存在 AIS 中断类事件（有 pre/post 上下文点）
const hasGap = computed(() =>
  (props.data.events || []).some(ev => ev.pre_point && ev.post_point)
)

function riskText(r) {
  return r === 'high' ? '高风险' : r === 'mid' || r === 'middle' ? '中风险' : '低风险'
}

function fmtPct(v) {
  if (v == null) return '-'
  return (Number(v) * 100).toFixed(0) + '%'
}

function fmt1(v) {
  if (v == null || isNaN(Number(v))) return '-'
  return Number(v).toFixed(1)
}

function fmt0(v) {
  if (v == null || isNaN(Number(v))) return '-'
  return Number(v).toFixed(0)
}

function buildOption() {
  const rawPts = props.data.track || []
  const pts = rawPts.map(p => ({ value: [p.lon, p.lat], ...p }))
  const evPts = (props.data.events || [])
    .flatMap(ev => (ev.evidence_points || []).map(p => ({ ...p, evLabel: ev.type_label || ev.type })))
    .map(p => ({ value: [p.lon, p.lat], ...p }))

  // 轨迹点抽稀：最多画 60 个点，避免密集段散点叠成一片（"重影"）
  const MAX_DOTS = 60
  let sparse = pts
  if (pts.length > MAX_DOTS) {
    const step = pts.length / MAX_DOTS
    sparse = pts.filter((_, i) => Math.floor(i / step) !== Math.floor((i + 1) / step))
  }

  // 每轴独立小数位：按各自数据范围自适应——跨度越大位数越少，防长标签互相重叠（"重影"）
  const lats = pts.map(p => p.lat)
  const lons = pts.map(p => p.lon)
  const latSpan = lats.length ? Math.max(...lats) - Math.min(...lats) : 0
  const lonSpan = lons.length ? Math.max(...lons) - Math.min(...lons) : 0
  const pickPrec = span => span > 0.1 ? 2 : span > 0.01 ? 3 : 4
  const fmtLon = v => Number(v).toFixed(pickPrec(lonSpan))
  const fmtLat = v => Number(v).toFixed(pickPrec(latSpan))

  // AIS 中断类事件上下文：中断前最后信号点（黄 pin）+ 中断期虚线（信号无回传段）
  const prePts = []
  const gapLinks = []
  ;(props.data.events || []).forEach(ev => {
    if (ev.pre_point && ev.post_point) {
      prePts.push({ value: [ev.pre_point.lon, ev.pre_point.lat], ...ev.pre_point, evLabel: '中断前最后信号' })
      gapLinks.push({ coords: [[ev.pre_point.lon, ev.pre_point.lat], [ev.post_point.lon, ev.post_point.lat]] })
    }
  })

  return {
    tooltip: {
      trigger: 'item',
      formatter: (p) => {
        const d = p.data
        if (!d) return ''
        const tag = d.evLabel ? '事件点 · ' + d.evLabel : (d._mk || '轨迹点')
        return `<b>${tag}</b><br/>`
          + `时间：${d.timestamp || '-'}<br/>`
          + `航速：${d.speed != null ? Number(d.speed).toFixed(1) : '-'} 节<br/>`
          + `航向：${d.course != null ? Number(d.course).toFixed(0) : '-'}°`
      }
    },
    grid: { left: 10, right: 10, top: 10, bottom: 10, containLabel: true },
    xAxis: {
      type: 'value', name: '经度', scale: true,
      splitNumber: 3,
      axisLabel: { formatter: fmtLon, color: '#5d7c92', hideOverlap: true, rotate: 20 },
      nameTextStyle: { color: '#5d7c92' },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value', name: '纬度', scale: true,
      splitNumber: 3,
      axisLabel: { formatter: fmtLat, color: '#5d7c92', hideOverlap: true },
      nameTextStyle: { color: '#5d7c92' },
      splitLine: { show: false },
    },
    series: [
      {
        name: '信号中断期', type: 'lines', data: gapLinks, z: 4,
        lineStyle: { type: 'dashed', color: '#ef5350', width: 1.5, opacity: 0.55 },
        effect: { show: true, symbol: 'arrow', symbolSize: 5, color: '#ef5350' },
      },
      {
        name: '轨迹', type: 'line', data: pts, symbol: 'none',
        lineStyle: { color: '#0c8ce9', width: 1.6, opacity: 0.85 }, connectNulls: true,
      },
      {
        name: '轨迹点', type: 'scatter', data: sparse, symbolSize: 4,
        itemStyle: { color: '#0c8ce9', opacity: 0.7 },
      },
      {
        name: '起点', type: 'scatter',
        data: pts.length ? [{ value: [pts[0].lon, pts[0].lat], ...pts[0], _mk: '起点' }] : [],
        symbol: 'pin', symbolSize: 26, z: 6,
        itemStyle: { color: '#0fa983' },
        label: { show: true, formatter: '起', color: '#fff', fontSize: 10, position: 'inside' },
      },
      {
        name: '终点', type: 'scatter',
        data: pts.length ? [{ value: [pts[pts.length - 1].lon, pts[pts.length - 1].lat], ...pts[pts.length - 1], _mk: '终点' }] : [],
        symbol: 'pin', symbolSize: 26, z: 6,
        itemStyle: { color: '#0c8ce9' },
        label: { show: true, formatter: '终', color: '#fff', fontSize: 10, position: 'inside' },
      },
      {
        name: '中断前最后信号', type: 'scatter', data: prePts,
        symbol: 'pin', symbolSize: 22, z: 6,
        itemStyle: { color: '#f5a524' },
        label: { show: true, formatter: '前', color: '#fff', fontSize: 10, position: 'inside' },
      },
      {
        name: '违规事件点', type: 'effectScatter', data: evPts, symbolSize: 13,
        rippleEffect: { scale: 3 }, z: 7,
        itemStyle: { color: '#ef5350' },
      },
    ]
  }
}

onMounted(() => {
  if (!chartEl.value || !props.data.track?.length) return
  chart = echarts.init(chartEl.value)
  chart.setOption(buildOption())
  ro = new ResizeObserver(() => chart && chart.resize())
  ro.observe(chartEl.value)
})

onUnmounted(() => {
  ro && ro.disconnect()
  chart && chart.dispose()
  chart = null
})
</script>

<style scoped>
.ev-card {
  width: 100%;
  background: #fff;
  border: 1px solid #d6e7f2;
  border-radius: 10px;
  color: #0d2b42;
  font-size: 12px;
  overflow: hidden;
}
.ev-verdict {
  padding: 10px 12px;
  border-bottom: 1px dashed #d6e7f2;
}
.ev-verdict.is-violation { border-left: 3px solid #ef5350; }
.ev-verdict.is-normal { border-left: 3px solid #0fa983; }
.ev-ship { margin-bottom: 6px; }
.ev-mmsi {
  font-weight: 700;
  font-size: 13px;
  font-family: Consolas, Monaco, monospace;
}
.ev-ship-type { margin-left: 6px; color: #5d7c92; }
.ev-verdict-row { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.ev-tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 8px;
  color: #fff;
  font-weight: 600;
}
.ev-tag.risk-high { background: #ef5350; }
.ev-tag.risk-mid { background: #f5a524; }
.ev-tag.risk-low { background: #0c8ce9; }
.ev-badge { font-size: 11px; }
.ev-badge.risk-high { color: #d32f2f; }
.ev-badge.risk-mid { color: #b45309; }
.ev-badge.risk-low { color: #0c8ce9; }
.ev-conf { color: #0b8a69; }
.ev-quality { color: #5d7c92; }
.ev-stats {
  margin-top: 6px;
  color: #5d7c92;
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.ev-anns { margin-top: 4px; color: #7c6ff0; }
.ev-section {
  padding: 10px 12px;
  border-top: 1px solid #d6e7f2;
}
.ev-section-title {
  font-size: 11px;
  color: #5d7c92;
  margin-bottom: 6px;
  letter-spacing: 0.5px;
}
.ev-chart { width: 100%; height: 220px; }
.ev-legend {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 6px;
  font-size: 11px;
  color: #5d7c92;
}
.ev-legend .ic {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  margin-right: 3px;
  vertical-align: -1px;
}
.ev-legend .ic-line { background: none; border-top: 2px solid #0c8ce9; border-radius: 0; width: 12px; }
.ev-legend .ic-green { background: #0fa983; }
.ev-legend .ic-blue { background: #0c8ce9; }
.ev-legend .ic-red { background: #ef5350; }
.ev-legend .ic-yellow { background: #f5a524; }
.ev-legend .ic-dash { background: none; border-top: 2px dashed #ef5350; border-radius: 0; width: 12px; }
.ev-table-wrap { overflow-x: auto; }
.ev-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
  min-width: 480px;
}
.ev-table th {
  text-align: left;
  color: #5d7c92;
  border-bottom: 1px solid #d6e7f2;
  padding: 5px 8px;
  font-weight: 600;
}
.ev-table td {
  padding: 5px 8px;
  border-bottom: 1px solid #eef3f8;
}
.ev-table tr:hover td { background: #f0f8fd; }
.ev-text { line-height: 1.6; margin: 0; }
.ev-action { display: flex; gap: 6px; }
.ev-action-label { color: #b45309; font-weight: 600; flex-shrink: 0; }
</style>
