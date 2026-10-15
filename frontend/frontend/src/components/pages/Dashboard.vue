<template>
  <div class="page">
    <PageHeader icon="bar-chart" title="数据分析" subtitle="辖区船舶动态与违规行为分析总览" />

    <!-- ==================== 顶部统计卡片 ==================== -->
    <div class="stats-grid">
      <div class="stat-card stat-blue">
        <div class="stat-icon"><AppIcon name="ship" :size="30" /></div>
        <div class="stat-info">
          <span class="stat-value">{{ stats.shipsTotal }}</span>
          <span class="stat-label">辖区船舶总数</span>
        </div>
      </div>
      <div class="stat-card stat-orange">
        <div class="stat-icon"><AppIcon name="alert-triangle" :size="30" /></div>
        <div class="stat-info">
          <span class="stat-value">{{ stats.violationShips }}</span>
          <span class="stat-label">违规船舶数</span>
        </div>
      </div>
      <div class="stat-card stat-green">
        <div class="stat-icon"><AppIcon name="radar" :size="30" /></div>
        <div class="stat-info">
          <span class="stat-value">{{ formatNumber(stats.aisTotal) }}</span>
          <span class="stat-label">AIS 数据点</span>
        </div>
      </div>
    </div>

    <!-- ==================== 图表区域 ==================== -->
    <div class="chart-grid">
      <div class="card">
        <div class="card-header">
          <h3>辖区活跃船舶</h3>
          <span class="card-subtitle">活跃 = 当日有 ≥1 条有效 AIS 报文的船舶（航行中 avg≥0.5节 / 锚泊 avg&lt;0.5节）</span>
        </div>
        <div class="card-body">
          <div ref="chartTraffic" class="chart-container"></div>
        </div>
      </div>
      <div class="card">
        <div class="card-header">
          <h3>违规类目分布</h3>
          <span class="card-subtitle">按一级类目统计，点击类目下钻到具体违规类型</span>
        </div>
        <div class="card-body">
          <div ref="chartViolationType" class="chart-container"></div>
        </div>
      </div>
    </div>

    <!-- ==================== 第二行图表 ==================== -->
    <div class="chart-grid">
      <div class="card">
        <div class="card-header">
          <h3>船舶航速分布</h3>
          <span class="card-subtitle">船舶平均航速区间统计</span>
        </div>
        <div class="card-body">
          <div ref="chartSpeedDist" class="chart-container"></div>
        </div>
      </div>
      <div class="card">
        <div class="card-header">
          <h3>违规态势趋势</h3>
          <span class="card-subtitle">每日违规事件数量变化</span>
        </div>
        <div class="card-body">
          <div ref="chartViolationTrend" class="chart-container"></div>
        </div>
      </div>
    </div>

    <!-- ==================== 实时违规告警表 ==================== -->
    <div class="card" style="margin-top:20px;">
      <div class="card-header">
        <h3>实时违规告警</h3>
        <span class="card-subtitle">最新违规事件实时推送</span>
      </div>
      <div class="card-body" style="padding:0;">
        <table class="data-table">
          <thead>
            <tr>
              <th>时间</th>
              <th>船名</th>
              <th>MMSI</th>
              <th>违规类型</th>
              <th>风险等级</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="alerts.length === 0">
              <td colspan="6" style="text-align:center;color:var(--text-secondary);padding:40px;">暂无违规告警</td>
            </tr>
            <tr
              v-for="alert in alerts"
              :key="alert.id"
              class="alert-row"
              :class="'alert-' + alert.riskLevel"
              :style="{ background: alert.flash ? 'rgba(234,67,53,0.05)' : '' }"
            >
              <td>{{ formatTime(alert.time) }}</td>
              <td>{{ alert.shipName }}</td>
              <td>{{ alert.mmsi }}</td>
              <td><span :class="'tag tag-' + alert.tagType">{{ alert.violationType }}</span></td>
              <td><span :class="'risk-level ' + alert.riskLevel">{{ riskLabel(alert.riskLevel) }}</span></td>
              <td><span :class="'status-tag ' + alert.status">{{ statusLabel(alert.status) }}</span></td>
            </tr>
          </tbody>
        </table>

        <!-- 移动端：告警卡片（桌面表格由 .data-table 自动隐藏） -->
        <div class="alert-cards">
          <div v-if="alerts.length === 0" class="ac-empty">暂无违规告警</div>
          <div
            v-for="alert in alerts"
            :key="alert.id"
            class="ac-card"
            :class="'risk-' + alert.riskLevel"
            :style="{ background: alert.flash ? 'rgba(234,67,53,0.05)' : '' }"
          >
            <div class="ac-head">
              <span class="ac-time"><AppIcon name="clock" inline /> {{ formatTime(alert.time) }}</span>
              <span :class="'status-tag ' + alert.status">{{ statusLabel(alert.status) }}</span>
            </div>
            <div class="ac-title">
              <span class="ac-ship">{{ alert.shipName }}</span>
              <span class="ac-mmsi">{{ alert.mmsi }}</span>
            </div>
            <div class="ac-foot">
              <span :class="'tag tag-' + alert.tagType">{{ alert.violationType }}</span>
              <span :class="'risk-level ' + alert.riskLevel">{{ riskLabel(alert.riskLevel) }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import * as echarts from 'echarts'
import PageHeader from '../../components/PageHeader.vue'
import AppIcon from '../../components/AppIcon.vue'
import { fetchStats, fetchTrafficTrend, fetchViolationDistribution, fetchAlerts } from '../../api/dashboard.js'
import { fetchViolationCatalog } from '../../api/violations.js'

const chartTraffic = ref(null)
const chartViolationType = ref(null)
const chartSpeedDist = ref(null)
const chartViolationTrend = ref(null)
let trafficChart = null
let violationChart = null
let speedChart = null
let trendChart = null
let flashInterval = null

const stats = ref({ shipsTotal: 0, violationShips: 0, violationTotal: 0, aisTotal: 0, avgSpeed: 0, avgMaxSpeed: 0, maxSpeedRecord: 0, shipTypes: [], dataTimeStart: '', dataTimeEnd: '' })
const alerts = ref([])

function formatNumber(n) {
  if (!n) return 0
  return n.toLocaleString()
}

function riskLabel(r) {
  return r === 'high' ? '高风险' : (r === 'mid' || r === 'middle') ? '中风险' : '低风险'
}

function statusLabel(s) {
  return s === 'pending' ? '待处置' : s === 'processing' ? '处置中' : '已处置'
}

function formatTime(t) {
  return t ? t.slice(11, 19) : ''
}

// ── 初始化所有图表 ──
async function initCharts(trendData, distData) {
  if (!chartTraffic.value || !chartViolationType.value || !chartSpeedDist.value || !chartViolationTrend.value) return

  // 兼容旧数据：ship_daily_stats 回填前 sailingShips/anchoredShips 可能缺失
  const _active = trendData.activeShips || []
  const _sailing = trendData.sailingShips || _active.map(() => 0)
  const _anchored = trendData.anchoredShips || _active.map(() => 0)

  // 1. 辖区活跃船舶
  trafficChart = echarts.init(chartTraffic.value)
  trafficChart.setOption({
    tooltip: { trigger: 'axis' },
    legend: {
      data: ['活跃船舶', '航行中', '锚泊'],
      bottom: 0,
      textStyle: { fontSize: 12 },
      tooltip: { show: true },
      formatter: name => name === '活跃船舶' ? '活跃（当日有≥1条有效AIS报文的船舶）' : name
    },
    // top 必须留出轴名的高度：ECharts 默认把 yAxis.name 画在轴的**顶端**，
    // 原来是 top:10，名字被画到画布外，等于单位标签全程不显示（实测截图确认）。
    // bottom 也要够：x 轴名 + 刻度标签 + 底部图例三层不能叠。
    grid: { left: 56, right: 50, top: 36, bottom: 58 },
    xAxis: {
      type: 'category', data: trendData.dates,
      name: '日期（月-日）', nameLocation: 'middle', nameGap: 30,
      nameTextStyle: { fontSize: 11, color: '#5d7c92' },
      axisLabel: { fontSize: 11, color: '#5d7c92' }
    },
    yAxis: [
      { type: 'value', name: '船舶数（艘）', nameGap: 16,
        nameTextStyle: { fontSize: 11, color: '#5d7c92', align: 'left' },
        splitLine: { lineStyle: { type: 'dashed', color: '#d6e7f2' } }, axisLabel: { color: '#5d7c92' } },
    ],
    series: [
      { name: '活跃船舶', type: 'line', smooth: true, symbol: 'circle', symbolSize: 6,
        lineStyle: { color: '#0c8ce9', width: 2.5 },
        itemStyle: { color: '#0c8ce9' },
        areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: 'rgba(12,140,233,0.28)' }, { offset: 1, color: 'rgba(12,140,233,0.02)' }]) },
        data: _active },
      { name: '航行中', type: 'line', smooth: true, symbol: 'diamond', symbolSize: 5,
        lineStyle: { color: '#18b8a6', width: 2, type: 'dashed' },
        itemStyle: { color: '#18b8a6' },
        data: _sailing },
      { name: '锚泊', type: 'line', smooth: true, symbol: 'triangle', symbolSize: 5,
        lineStyle: { color: '#f5a524', width: 2, type: 'dashed' },
        itemStyle: { color: '#f5a524' },
        data: _anchored },
    ]
  })

  // 2. 违规类目分布（环形图，点击类目下钻到二级类型）
  violationChart = echarts.init(chartViolationType.value)
  // 类目配色。本轮类目从 7 个加到 8 个（新增「锚泊管理」），所以补齐到 8 色，
  // 并把灰色留给末位 —— 色彩按**全量类目表**的位置取，不按筛后的下标取：
  // 类目视图只画 ship_count>0 的扇区，按筛后下标取色会让某个空类目一出现，
  // 后面的扇区整体串色（「其他」拿到红色，与 AIS设备类同色）。
  const catColors = ['#ef5350', '#0c8ce9', '#f5a524', '#18b8a6', '#7c6ff0', '#ff8f5c', '#5c9e3d', '#9e9e9e']
  const catColorOf = (name) => {
    const i = (catalogData?.categories || []).findIndex(c => c.name === name)
    return catColors[(i < 0 ? 0 : i) % catColors.length]
  }
  let currentCatDrill = null   // 当前下钻类目名，null=类目视图
  let catalogData = null       // 缓存 catalog 数据

  function renderViolationPie(catName) {
    if (!catalogData || !violationChart) return
    if (!catName) {
      // 类目视图
      currentCatDrill = null
      const data = catalogData.categories
        .filter(c => c.ship_count > 0)
        .map(c => ({ value: c.ship_count, name: c.label, _name: c.name,
                     itemStyle: { color: catColorOf(c.name) } }))
      violationChart.setOption({
        title: { text: '违规类目分布', subtext: '点击类目下钻', left: 'center', top: 0,
                 textStyle: { fontSize: 13, color: '#0d2b42' }, subtextStyle: { fontSize: 10, color: '#9e9e9e' } },
        tooltip: { trigger: 'item', formatter: p => `${p.name}<br/>违规船舶: ${p.value}艘 (${p.percent}%)` },
        legend: { bottom: 0, textStyle: { fontSize: 11, color: '#5d7c92' } },
        series: [{
          type: 'pie', radius: ['40%', '70%'], center: ['50%', '48%'],
          avoidLabelOverlap: true,
          label: { show: true, formatter: p => p.value > 0 ? `${p.name}\n${p.value}艘` : '', fontSize: 10, color: '#0d2b42' },
          emphasis: { label: { show: true, fontSize: 13, fontWeight: 'bold' } },
          data: data,
        }]
      }, true)
    } else {
      // 二级类型视图（下钻）
      currentCatDrill = catName
      const cat = catalogData.categories.find(c => c.name === catName)
      const catLabel = cat ? cat.label : catName
      const types = catalogData.types.filter(t => t.category === catName && t.ship_count > 0)
        .sort((a, b) => b.ship_count - a.ship_count)
        .map((t, i) => ({ value: t.ship_count, name: t.label, _code: t.code,
                            itemStyle: { color: catColors[i % catColors.length] } }))
      violationChart.setOption({
        title: { text: `${catLabel} — 违规类型`, subtext: '点击空白返回类目视图', left: 'center', top: 0,
                 textStyle: { fontSize: 13, color: '#0d2b42' }, subtextStyle: { fontSize: 10, color: '#9e9e9e' } },
        tooltip: { trigger: 'item', formatter: p => `${p.name}<br/>违规船舶: ${p.value}艘 (${p.percent}%)` },
        legend: { bottom: 0, type: 'scroll', textStyle: { fontSize: 11, color: '#5d7c92' } },
        series: [{
          type: 'pie', radius: ['35%', '68%'], center: ['50%', '48%'],
          avoidLabelOverlap: true,
          label: { show: true, formatter: p => p.value > 0 ? `${p.name}\n${p.value}艘` : '', fontSize: 10, color: '#0d2b42' },
          emphasis: { label: { show: true, fontSize: 13, fontWeight: 'bold' } },
          data: types,
        }]
      }, true)
    }
    violationChart.resize()
  }

  // 异步加载 catalog
  fetchViolationCatalog().then(catalog => {
    catalogData = catalog
    renderViolationPie(null)
  }).catch(() => {
    // 降级：用 distData 渲染旧样式
    violationChart.setOption({
      tooltip: { trigger: 'item', formatter: '{b}: {c}起 ({d}%)' },
      legend: { bottom: 0, textStyle: { fontSize: 12, color: '#5d7c92' } },
      series: [{
        type: 'pie', radius: ['40%', '70%'], center: ['50%', '45%'],
        avoidLabelOverlap: true, label: { show: true, formatter: '{b}', fontSize: 11, color: '#0d2b42' },
        emphasis: { label: { show: true, fontSize: 14, fontWeight: 'bold' } },
        color: catColors,
        data: distData.types.map((d, i) => ({ value: d.count, name: d.type, itemStyle: { color: catColors[i % catColors.length] } }))
      }]
    })
  })

  // 点击类目下钻 / 点击空白返回
  violationChart.off('click')
  violationChart.on('click', params => {
    if (!catalogData) return
    if (!currentCatDrill) {
      // 类目视图 → 下钻到该类目
      const cat = catalogData.categories.find(c => c.label === params.name)
      if (cat) renderViolationPie(cat.name)
    } else {
      // 二级视图 → 返回类目视图
      renderViolationPie(null)
    }
  })

  // 3. 航速分布柱状图 — 从后端获取真实数据
  speedChart = echarts.init(chartSpeedDist.value)
  let speedLabels = ['<5节', '5-10节', '10-15节', '>15节']
  let speedCounts = [0, 0, 0, 0]
  try {
    const resp = await fetch('/api/v1/ships/speed-stats')
    const json = await resp.json()
    if (json.code === 200 && json.data) {
      speedLabels = json.data.labels
      speedCounts = json.data.counts
    }
  } catch (e) {
    console.error('速度统计加载失败，使用兜底数据:', e)
  }
  const speedBarColors = ['#0c8ce9', '#18b8a6', '#f5a524', '#ef5350']
  speedChart.setOption({
    tooltip: { trigger: 'axis', formatter: params => `${params[0].name}<br/>船舶数: ${params[0].value}艘` },
    grid: { left: 56, right: 20, top: 36, bottom: 56 },
    xAxis: {
      type: 'category', data: speedLabels,
      name: '航速区间', nameLocation: 'middle', nameGap: 28,
      nameTextStyle: { fontSize: 11, color: '#5d7c92' },
      axisLabel: { fontSize: 11, color: '#5d7c92' }
    },
    yAxis: { type: 'value', name: '船舶数（艘）', nameGap: 16,
      nameTextStyle: { fontSize: 11, color: '#5d7c92', align: 'left' },
      splitLine: { lineStyle: { type: 'dashed', color: '#d6e7f2' } }, axisLabel: { color: '#5d7c92' } },
    series: [{
      type: 'bar', barWidth: 30,
      itemStyle: {
        borderRadius: [6, 6, 0, 0],
        color: params => new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: speedBarColors[params.dataIndex] || '#0c8ce9' },
          { offset: 1, color: '#f4fbfd' }
        ])
      },
      data: speedCounts
    }]
  })

  // 4. 违规态势趋势（折线图）
  trendChart = echarts.init(chartViolationTrend.value)
  const trendDays = distData.dailyTrend.map(d => d.day)
  const trendCounts = distData.dailyTrend.map(d => d.count)
  trendChart.setOption({
    tooltip: { trigger: 'axis' },
    // right 留宽：markLine 的均值标签贴在线的末端，原来 right:20 装不下
    // 「均值 34」被画布右边缘裁掉，只剩一个「均」字（实测截图确认）
    grid: { left: 56, right: 60, top: 36, bottom: 56 },
    xAxis: {
      type: 'category', data: trendDays,
      name: '日期（月-日）', nameLocation: 'middle', nameGap: 28,
      nameTextStyle: { fontSize: 11, color: '#5d7c92' },
      axisLabel: { fontSize: 11, color: '#5d7c92' }
    },
    yAxis: { type: 'value', name: '违规事件数（起）', nameGap: 16, minInterval: 1,
      nameTextStyle: { fontSize: 11, color: '#5d7c92', align: 'left' },
      splitLine: { lineStyle: { type: 'dashed', color: '#d6e7f2' } }, axisLabel: { color: '#5d7c92' } },
    series: [{
      type: 'line', smooth: true, symbol: 'circle', symbolSize: 8,
      lineStyle: { color: '#ef5350', width: 2 },
      itemStyle: { color: '#ef5350' },
      areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: 'rgba(239,83,80,0.28)' }, { offset: 1, color: 'rgba(239,83,80,0.02)' }]) },
      markLine: { data: [{ type: 'average', name: '平均值' }], label: { formatter: '均值 {c}', position: 'insideEndTop' }, lineStyle: { color: '#f5a524', type: 'dashed' } },
      data: trendCounts,
    }]
  })

  window.addEventListener('resize', handleResize)
}

function handleResize() {
  trafficChart?.resize()
  violationChart?.resize()
  speedChart?.resize()
  trendChart?.resize()
}

onMounted(async () => {
  try {
    const [s, t, d, a] = await Promise.all([
      fetchStats(),
      fetchTrafficTrend(),
      fetchViolationDistribution(),
      fetchAlerts()
    ])
    stats.value = s
    alerts.value = a.map(item => ({ ...item, flash: false }))

    await nextTick()
    initCharts(t, d)
  } catch (e) {
    console.error('数据加载失败:', e)
  }

  flashInterval = setInterval(() => {
    alerts.value = alerts.value.map(a => ({
      ...a,
      flash: a.riskLevel === 'high' ? !a.flash : a.flash
    }))
  }, 2000)
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  trafficChart?.dispose()
  violationChart?.dispose()
  speedChart?.dispose()
  trendChart?.dispose()
  if (flashInterval) clearInterval(flashInterval)
})
</script>

<style scoped>
.stats-grid {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 24px;
}
.stat-card {
    background: var(--gradient-card); border-radius: var(--radius); padding: 20px;
    box-shadow: var(--shadow); display: flex; flex-wrap: wrap; align-items: center;
    position: relative; overflow: hidden;
    border: 1px solid rgba(169, 205, 226, 0.4);
    transition: transform var(--transition), box-shadow var(--transition);
}
.stat-card:hover { transform: translateY(-3px); box-shadow: var(--shadow-lg); }
.stat-card::after {
    content: ''; position: absolute; right: -30px; bottom: -30px;
    width: 90px; height: 90px; border-radius: 50%;
    background: radial-gradient(circle, rgba(12,140,233,0.1), transparent 70%);
}
.stat-card::before {
    content: ''; position: absolute; top: 0; left: 0; width: 4px; height: 100%;
    border-radius: 4px 0 0 4px;
}
.stat-blue::before { background: linear-gradient(180deg, #22c9b8, #0d9488); }
.stat-orange::before { background: linear-gradient(180deg, #ffb74d, #f57c00); }
.stat-green::before { background: linear-gradient(180deg, #39d6b0, #0fa983); }
.stat-purple::before { background: linear-gradient(180deg, #9f8ff5, #7c6ff0); }
.stat-icon { font-size: 32px; margin-right: 16px; }
.stat-blue .stat-icon { color: var(--teal-dark); }
.stat-orange .stat-icon { color: #d97706; }
.stat-green .stat-icon { color: var(--success); }
.stat-info { flex: 1; position: relative; z-index: 1; }
.stat-value { font-size: 28px; font-weight: 700; display: block; line-height: 1.2; }
.stat-label { font-size: 13px; color: var(--text-secondary); }
.stat-orange .stat-value { color: #b45309; }
.stat-green .stat-value { color: #0b8a69; }
.stat-purple .stat-value { color: #5b4bc4; }
.chart-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }
.chart-container { width: 100%; height: 300px; }

/* 移动端：告警卡片（桌面隐藏，由 .data-table 出表格） */
.alert-cards { display: none; }
@media (max-width: 768px) {
    .alert-cards {
        display: flex; flex-direction: column; gap: 10px; padding: 12px;
    }
    .ac-card {
        background: var(--gradient-card);
        border: 1px solid rgba(169, 205, 226, 0.4);
        border-left: 4px solid var(--info);
        border-radius: 10px; padding: 12px 14px;
        box-shadow: 0 2px 10px rgba(6, 31, 56, 0.06);
        transition: background 0.3s;
    }
    .ac-card.risk-high { border-left-color: var(--danger); }
    .ac-card.risk-mid { border-left-color: var(--warning); }
    .ac-card.risk-low { border-left-color: var(--info); }
    .ac-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
    .ac-time { font-size: 12px; color: var(--text-secondary); }
    .ac-title { display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px; }
    .ac-ship { font-size: 15px; font-weight: 700; }
    .ac-mmsi { font-size: 12px; color: var(--text-secondary); font-family: Consolas, Monaco, monospace; }
    .ac-foot { display: flex; align-items: center; justify-content: space-between; }
    .ac-empty { text-align: center; color: var(--text-secondary); padding: 24px 0; }
}
</style>