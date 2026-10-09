<template>
  <div class="render-blocks">
    <div v-for="(block, i) in blocks" :key="i" class="rb-block" :class="'rb-' + block.kind">
      <!-- 标题 -->
      <div v-if="block.title" class="rb-title">{{ block.title }}</div>
      <div v-if="block.note" class="rb-note">{{ block.note }}</div>

      <!-- 指标卡片 -->
      <div v-if="block.kind === 'metric'" class="rb-metric">
        <div v-for="(item, j) in block.spec.items" :key="j" class="rb-metric-item">
          <span class="rb-metric-value">{{ item.value }}</span>
          <span class="rb-metric-label">{{ item.label }}</span>
          <span v-if="item.hint" class="rb-metric-hint">{{ item.hint }}</span>
        </div>
      </div>

      <!-- 图表 -->
      <div v-else-if="block.kind === 'chart'" class="rb-chart">
        <div :ref="el => { if (el) chartRefs[i] = el }" class="rb-chart-canvas" :style="{ height: chartHeight(block) }"></div>
      </div>

      <!-- 表格 -->
      <div v-else-if="block.kind === 'table'" class="rb-table-wrap">
        <table class="rb-table">
          <thead>
            <tr>
              <th v-for="col in block.spec.columns" :key="col.key"
                  :style="{ textAlign: col.align || 'left' }">{{ col.label }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, ri) in block.spec.rows" :key="ri">
              <td v-for="(cell, ci) in row" :key="ci"
                  :style="{ textAlign: (block.spec.columns[ci] && block.spec.columns[ci].align) || 'left' }">{{ cell }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 文档 -->
      <div v-else-if="block.kind === 'doc'" class="rb-doc">
        <div v-for="(sec, si) in block.spec.sections" :key="si" class="rb-doc-section">
          <div class="rb-doc-heading">{{ sec.heading }}</div>
          <p v-for="(p, pi) in (sec.paragraphs || [])" :key="pi" class="rb-doc-p">{{ p }}</p>
          <ul v-if="sec.list && sec.list.length" class="rb-doc-list">
            <li v-for="(li, lii) in sec.list" :key="lii">{{ li }}</li>
          </ul>
        </div>
      </div>

      <!-- 文本兜底 -->
      <div v-else-if="block.kind === 'text'" class="rb-text">{{ block.spec.text }}</div>

      <!-- 未知 kind -->
      <div v-else class="rb-text" style="color:#f5a524;">[未支持的渲染类型: {{ block.kind }}]</div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUpdated, nextTick, watch } from 'vue'
// 必须按模块引入。这里原本写的是 `if (!window.echarts) return`，
// 但全仓库（含 index.html）**从来没有任何地方给 window.echarts 赋值** ——
// 别的图都走 `import * as echarts from 'echarts'`。于是这个守卫恒为真，
// 渲染函数每次都在第一行返回：AI 对话里的图表块一直是空的，从未画出来过。
import * as echarts from 'echarts'

const props = defineProps({
  blocks: { type: Array, default: () => [] }
})

const chartRefs = ref([])

function chartHeight(block) {
  if (block.spec.chart_type === 'pie') return '220px'
  return '200px'
}

function renderCharts() {
  props.blocks.forEach((block, i) => {
    if (block.kind !== 'chart') return
    const el = chartRefs.value[i]
    if (!el) return
    let inst = echarts.getInstanceByDom(el)
    if (!inst) inst = echarts.init(el)
    const spec = block.spec
    const colors = ['#ef5350', '#0c8ce9', '#f5a524', '#18b8a6', '#7c6ff0', '#ff8f5c', '#9e9e9e']

    if (spec.chart_type === 'pie') {
      const seriesData = spec.series[0]?.data || []
      inst.setOption({
        tooltip: { trigger: 'item', formatter: p => `${p.name}: ${p.value} (${p.percent}%)` },
        legend: { bottom: 0, type: 'scroll', textStyle: { fontSize: 10, color: '#b9c6dd' } },
        series: [{
          type: 'pie', radius: ['38%', '68%'], center: ['50%', '44%'],
          avoidLabelOverlap: true,
          label: { show: true, formatter: p => p.value > 0 ? `${p.name}` : '', fontSize: 10, color: '#e8f0fe' },
          emphasis: { label: { show: true, fontSize: 12, fontWeight: 'bold' } },
          data: seriesData.map((d, di) => ({ name: d.name, value: d.value, itemStyle: { color: colors[di % colors.length] } })),
        }],
      }, true)
    } else {
      // bar / line
      const x = spec.x || []
      // 后端 chart_block 一直在传 unit（"起" 等），但这个组件从来没读过 spec.unit，
      // 等于单位算好了发过来又被丢掉，图上没有任何量纲。没有 y_name 时退用 unit。
      // top 也要跟着让位：ECharts 把 yAxis.name 画在轴顶端，top:10 会把它裁掉。
      const yName = spec.y_name || spec.unit || ''
      inst.setOption({
        tooltip: { trigger: 'axis' },
        grid: { left: 40, right: 16, top: yName ? 26 : 10, bottom: 30 },
        xAxis: { type: 'category', data: x, axisLabel: { fontSize: 10, color: '#b9c6dd', rotate: x.length > 6 ? 30 : 0 }, axisLine: { lineStyle: { color: 'rgba(255,255,255,0.15)' } } },
        yAxis: { type: 'value', name: yName, nameGap: 12, nameTextStyle: { fontSize: 10, color: '#b9c6dd', align: 'left' }, splitLine: { lineStyle: { type: 'dashed', color: 'rgba(255,255,255,0.08)' } }, axisLabel: { color: '#b9c6dd', fontSize: 10 } },
        series: (spec.series || []).map((s, si) => ({
          name: s.name, type: spec.chart_type, smooth: spec.chart_type === 'line',
          data: s.data || [],
          itemStyle: { color: s.color || colors[si % colors.length] },
          lineStyle: spec.chart_type === 'line' ? { color: s.color || colors[si % colors.length] } : undefined,
          areaStyle: spec.chart_type === 'line' ? { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: (s.color || colors[si % colors.length]) + '55' }, { offset: 1, color: (s.color || colors[si % colors.length]) + '05' }]) } : undefined,
          barMaxWidth: 28,
        })),
      }, true)
    }
  })
}

onMounted(() => nextTick(renderCharts))
onUpdated(() => nextTick(renderCharts))
watch(() => props.blocks, () => nextTick(renderCharts), { deep: true })
</script>

<style scoped>
.render-blocks { display: flex; flex-direction: column; gap: 8px; margin-top: 8px; }
.rb-block {
  background: rgba(255,255,255,0.05);
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 9px;
  padding: 9px 11px;
}
.rb-title { font-size: 12px; font-weight: 700; color: #e8f0fe; margin-bottom: 2px; }
.rb-note { font-size: 10px; color: rgba(185,198,221,0.55); margin-bottom: 6px; }

/* 指标卡 */
.rb-metric { display: grid; grid-template-columns: repeat(auto-fill, minmax(70px, 1fr)); gap: 8px; }
.rb-metric-item { display: flex; flex-direction: column; align-items: center; text-align: center; }
.rb-metric-value { font-size: 17px; font-weight: 700; color: #e8f0fe; }
.rb-metric-label { font-size: 10px; color: #9db4d9; margin-top: 1px; }
.rb-metric-hint { font-size: 9px; color: rgba(157,180,217,0.5); }

/* 图表 */
.rb-chart-canvas { width: 100%; min-height: 180px; }

/* 表格 */
.rb-table-wrap { overflow-x: auto; max-height: 280px; overflow-y: auto; }
.rb-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.rb-table th { background: rgba(26,115,232,0.2); color: #cfe3ff; font-weight: 600; padding: 6px 8px; text-align: left; position: sticky; top: 0; white-space: nowrap; }
.rb-table td { padding: 5px 8px; color: #b9c6dd; border-bottom: 1px solid rgba(255,255,255,0.05); white-space: nowrap; }
.rb-table tr:hover td { background: rgba(26,115,232,0.08); }

/* 文档 */
.rb-doc-section { margin-bottom: 10px; }
.rb-doc-section:last-child { margin-bottom: 0; }
.rb-doc-heading { font-size: 12px; font-weight: 700; color: #cfe3ff; margin-bottom: 4px; }
.rb-doc-p { font-size: 12px; color: #b9c6dd; line-height: 1.6; margin: 0 0 4px; }
.rb-doc-list { margin: 2px 0 0; padding-left: 18px; }
.rb-doc-list li { font-size: 12px; color: #b9c6dd; line-height: 1.6; }

/* 文本 */
.rb-text { font-size: 12px; color: #b9c6dd; line-height: 1.6; white-space: pre-wrap; }
</style>
