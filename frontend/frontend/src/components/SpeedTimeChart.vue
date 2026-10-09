<template>
  <div class="stc">
    <div class="stc-head">
      <span class="stc-title">航速 — 时间曲线</span>
      <span class="stc-hint">
        {{ points.length }} 个轨迹点 · 覆盖 {{ coverageText }}
        <template v-if="bands.length"> · 标注 {{ bands.length }} 段异常区间</template>
      </span>
    </div>
    <div ref="chartEl" class="stc-chart"></div>
    <div class="stc-legend">
      <span class="lg"><i class="ic ic-line"></i>航速（节）</span>
      <span v-for="b in bands" :key="b.key" class="lg">
        <i class="ic ic-band"></i>{{ b.label }}
      </span>
    </div>
    <p v-if="bands.length" class="stc-note">{{ noteText }}</p>
  </div>
</template>

<script setup>
/**
 * SpeedTimeChart.vue — 航速-时间曲线 + 违规区间色带
 *
 * 【为什么单独一个组件】
 *   不塞进 EvidenceCard.vue：那个组件被 AIChat / FloatingAIChat 共用，
 *   改它等于同时改聊天 UI。这里只服务违规监管的证据弹窗。
 *
 * 【为什么 x 轴必须是 type:'time'】
 *   这批数据跨度 17.5 小时，其中 11.3 小时是 AIS 空档。用 category 轴的话
 *   空档会被压成和相邻 30 秒一样宽 —— 图会"证明"没有中断，正好把要说明的
 *   事情掩盖掉。时间轴上空档自然留白，色带落上去，图自己就说明了中断。
 *
 * 【时区】
 *   track[].timestamp 形如 '2022-09-10T06:29:21Z'。直接交给 ECharts 会按 UTC
 *   解析，而全站其它地方（formatTime() 只做 replace('T',' ')）是字面量显示，
 *   差 8 小时。统一用 new Date(s.replace('Z','')) 当本地时间解释，
 *   与 shipclaw/evidence.py::_parse_iso 的口径一致。
 */
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import * as echarts from 'echarts'

const props = defineProps({
  /** 证据接口的 evidence 对象：{ track[], events[] } */
  data: { type: Object, required: true },
})

const chartEl = ref(null)
let chart = null
let ro = null

const COLORS = { line: '#0c8ce9', band: 'rgba(239, 83, 80, 0.14)', edge: '#ef5350' }

/** ISO 字符串 → 本地时间毫秒（口径见文件头注释）。 */
function tsOf(s) {
  if (!s) return null
  const t = new Date(String(s).replace('Z', '')).getTime()
  return isNaN(t) ? null : t
}

function fmtDur(min) {
  if (min == null) return ''
  const m = Number(min)
  if (m >= 60) return `${(m / 60).toFixed(1)} 小时`
  return `${m.toFixed(0)} 分钟`
}

/** [时间戳, 航速] 序列；时间戳解析不出来或航速缺失的点直接跳过。 */
const points = computed(() => {
  const out = []
  for (const p of props.data?.track || []) {
    const t = tsOf(p.timestamp)
    if (t == null) continue
    const v = p.speed == null || isNaN(Number(p.speed)) ? null : Number(p.speed)
    out.push([t, v])
  }
  return out
})

const coverageText = computed(() => {
  const pts = points.value
  if (pts.length < 2) return '-'
  return fmtDur((pts[pts.length - 1][0] - pts[0][0]) / 60000)
})

/**
 * 违规区间。取值优先级 —— AIS 中断类事件的 start_time/end_time 是**退化**的
 * （两者都等于"信号恢复时刻"，画出来是个零宽度的带子），所以不能直接用：
 *   1. pre_point / post_point   中断前最后信号 → 恢复后首点  ← 这批数据的唯一可靠路径
 *   2. start_ts ≠ end_ts        正常事件直接用
 *   3. end − duration_minutes   只有结束时刻和时长
 *   4. 都没有                     只画点标记，不画色带
 */
const bands = computed(() => {
  const out = []
  ;(props.data?.events || []).forEach((ev, i) => {
    const label = ev.type_label || ev.type || '违规'
    const pre = tsOf(ev.pre_point?.timestamp)
    const post = tsOf(ev.post_point?.timestamp)

    let x0 = null
    let x1 = null
    let how = ''
    if (pre != null && post != null) {
      x0 = Math.min(pre, post); x1 = Math.max(pre, post)
      how = ev.gap_minutes ? `无回传 ${fmtDur(ev.gap_minutes)}` : fmtDur((x1 - x0) / 60000)
    } else {
      const a = tsOf(ev.start_ts)
      const b = tsOf(ev.end_ts)
      if (a != null && b != null && a !== b) {
        x0 = Math.min(a, b); x1 = Math.max(a, b)
      } else if (b != null && ev.duration_minutes > 0) {
        x1 = b; x0 = b - Number(ev.duration_minutes) * 60000
      }
      if (x0 != null) how = fmtDur(ev.duration_minutes ?? (x1 - x0) / 60000)
    }

    const mark = pre != null ? pre : (tsOf(ev.end_ts) ?? tsOf(ev.start_ts))
    if (x0 == null && mark == null) return
    out.push({ key: `${i}-${ev.type || ''}`, label, x0, x1, mark, how })
  })
  return out
})

const noteText = computed(() =>
  bands.value.some(b => b.x0 != null)
    ? '红色色带内轨迹为空 —— 该时段没有任何 AIS 回传，这正是信号中断的直接证据。'
    : '该违规事件没有可靠的起止时刻，仅在时间轴上标出发生点。'
)

/**
 * 送进 series 的数据 = 真实轨迹点 + 每个空档中点插一个 null。
 *
 * 【为什么必须插 null】connectNulls:false 只在遇到 null 时断线；而"空档里没有点"
 * 这件事 ECharts 是看不见的 —— 时间轴上它只会把空档前后两点直接连成一条直线，
 * 于是 11 小时的无回传被画成"一直在匀速航行"，图反而替违规方作证。
 * 在空档中点插一个 null，线段才真的断开。
 */
const seriesData = computed(() => {
  const out = points.value.slice()
  for (const b of bands.value) {
    if (b.x0 == null || b.x1 == null) continue
    out.push([(b.x0 + b.x1) / 2, null])
  }
  out.sort((a, c) => a[0] - c[0])
  return out
})

function buildOption() {
  const withBand = bands.value.filter(b => b.x0 != null)

  // markArea 需要按"每段一个数据对"给出，且挂在某个 series 上
  const markAreaData = withBand.slice(0, 1).map(b => ([
    { xAxis: b.x0, itemStyle: { color: COLORS.band },
      label: { show: true, position: 'insideTop', color: COLORS.edge,
               fontSize: 11, formatter: `${b.label} · ${b.how}` } },
    { xAxis: b.x1 },
  ]))
  const markLineData = []
  for (const b of withBand) {
    markLineData.push({ xAxis: b.x0, lineStyle: { color: COLORS.edge, type: 'dashed', width: 1 } })
    markLineData.push({ xAxis: b.x1, lineStyle: { color: COLORS.edge, type: 'dashed', width: 1 } })
  }

  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      formatter: (ps) => {
        const p = (ps || []).find(x => x.value && x.value[1] != null) || (ps || [])[0]
        if (!p) return ''
        const d = new Date(p.value[0])
        const pad = n => String(n).padStart(2, '0')
        const t = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
                + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
        const sp = p.value[1] == null ? '无回传（空档）' : `${Number(p.value[1]).toFixed(1)} 节`
        return `${t}<br/>航速：${sp}`
      },
    },
    // right 加宽给轴名让位；grid 的 bottom(56) 已够放"刻度 + 轴名"两行，
    // 但不能再往下压 —— 底部 bottom:12 处有 dataZoom 滑块（高 18）
    grid: { left: 30, right: 60, top: 34, bottom: 56, containLabel: true },
    xAxis: {
      type: 'time',
      name: '时间',
      // 轴名放轴末端（时间轴的常规位置）。不用 nameLocation:'middle'：
      // 那需要再往下留约 47px，会撞上 dataZoom 滑块。
      nameGap: 30,
      nameTextStyle: { color: '#5d7c92', fontSize: 11 },
      axisLabel: { color: '#5d7c92', hideOverlap: true },
      axisLine: { lineStyle: { color: '#d6e7f2' } },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      name: '航速（节）',
      scale: true,
      nameTextStyle: { color: '#5d7c92' },
      axisLabel: { color: '#5d7c92' },
      splitLine: { lineStyle: { color: '#eef3f8' } },
    },
    dataZoom: [
      { type: 'inside' },
      // 17.5 小时跨度 + 11.3 小时空档，不给滑块的话两端会挤成一团
      { type: 'slider', height: 18, bottom: 12, borderColor: '#d6e7f2' },
    ],
    series: [{
      name: '航速',
      type: 'line',
      // 必须 .value —— 在 <script> 里 seriesData 是 ref 对象而不是数组，
      // 直接塞进去 ECharts 的 option 合并会递归进 Vue 的 ref 内部结构
      // （RangeError: Maximum call stack size exceeded）
      data: seriesData.value,
      showSymbol: false,
      // 不开 sampling：只有 300 点不需要抽稀，而 lttb 会对着 null 做算术。
      // 必须 false：中断段本来就没有点，连起来等于把"没有回传"画成"一直在航行"。
      connectNulls: false,
      lineStyle: { color: COLORS.line, width: 1.6 },
      itemStyle: { color: COLORS.line },
      markArea: markAreaData.length ? { silent: true, data: markAreaData } : undefined,
      markLine: markLineData.length
        // label 必须关掉：markLine 默认把 xAxis 的原始值当标签打印，
        // 时间轴上那就是一串 epoch 毫秒（1662787766000），纯噪音
        ? { silent: true, symbol: 'none', label: { show: false }, data: markLineData }
        : undefined,
    }],
  }
}

function render() {
  if (!chartEl.value || !points.value.length) return
  if (!chart) chart = echarts.init(chartEl.value)
  chart.setOption(buildOption(), true)
}

onMounted(() => {
  render()
  if (chartEl.value) {
    ro = new ResizeObserver(() => chart && chart.resize())
    ro.observe(chartEl.value)
  }
})

// 弹窗里切换记录时数据会变，图表要跟着重建
watch(() => props.data, () => nextTick(render), { deep: false })

onUnmounted(() => {
  ro && ro.disconnect()
  chart && chart.dispose()
  chart = null
})
</script>

<style scoped>
.stc {
  background: #fff;
  border: 1px solid #d6e7f2;
  border-radius: 10px;
  padding: 10px 12px 12px;
  color: #0d2b42;
}
.stc-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.stc-title { font-size: 12px; font-weight: 600; }
.stc-hint { font-size: 11px; color: #5d7c92; }
.stc-chart { width: 100%; height: 260px; margin-top: 4px; }
.stc-legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 11px; color: #5d7c92; }
.stc-legend .ic {
  display: inline-block; width: 10px; height: 10px; border-radius: 50%;
  margin-right: 3px; vertical-align: -1px;
}
.stc-legend .ic-line { background: none; border-top: 2px solid #0c8ce9; border-radius: 0; width: 12px; }
.stc-legend .ic-band { background: rgba(239, 83, 80, 0.28); border: 1px solid #ef5350; border-radius: 2px; width: 12px; }
.stc-note { margin: 8px 0 0; font-size: 11px; line-height: 1.6; color: #b45309; }
</style>
