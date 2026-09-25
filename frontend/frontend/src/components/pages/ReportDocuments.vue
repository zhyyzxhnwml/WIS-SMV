<template>
  <div class="page">
    <PageHeader icon="clipboard" title="违规监控" subtitle="所有违规船舶信息汇总">
      <button class="btn btn-primary" @click="loadData"><AppIcon name="refresh-cw" inline /> 刷新</button>
    </PageHeader>

    <!-- 搜索 / 三级筛选工具栏：类目 → 类型 → 细类 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <input type="text" class="search-input" placeholder="搜索MMSI / 违规描述..." v-model="searchText" @input="loadData">
        <select class="form-select" v-model="filterCategory" @change="onCategoryChange">
          <option value="">全部类目</option>
          <option v-for="c in catalog.categories" :key="c.name" :value="c.name">{{ c.label }} ({{ c.ship_count }})</option>
        </select>
        <select class="form-select" v-model="filterType" @change="onTypeChange" style="min-width:150px;">
          <option value="">全部类型</option>
          <option v-for="t in filteredTypes" :key="t.code" :value="t.code">{{ t.label }} ({{ t.ship_count }})</option>
        </select>
        <select class="form-select" v-model="filterSubtype" @change="loadData" style="min-width:150px;"
                :disabled="!filteredSubtypes.length">
          <option value="">全部细类</option>
          <option v-for="s in filteredSubtypes" :key="s.code" :value="s.code">
            {{ s.label }}{{ rangeText(s) }} ({{ s.ship_count }})
          </option>
        </select>
      </div>
      <div class="toolbar-right">
        <span class="toolbar-hint">共 <strong>{{ total }}</strong> 条记录</span>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <h3>违规船舶列表</h3>
        <span class="card-subtitle">系统检测到的违规船舶记录</span>
      </div>
      <div class="card-body" style="padding:0;">
        <table class="data-table">
          <thead>
            <tr>
              <th>船舶/MMSI</th>
              <th>违规类型</th>
              <th>细类</th>
              <th>风险等级</th>
              <th>数据质量</th>
              <th>违反法规</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="list.length === 0">
              <td colspan="7">
                <div class="empty-state">
                  <span class="empty-icon"><AppIcon name="wave" :size="40" style="color:var(--text-secondary);" /></span>
                  <span class="empty-text">暂无违规记录</span>
                  <span class="empty-hint">系统检测到违规行为后会实时展示在这里</span>
                </div>
              </td>
            </tr>
            <tr v-for="row in list" :key="row.id" :class="'alert-' + row.riskLevel">
              <td><strong>{{ row.shipName }}</strong><br><small style="color:var(--text-secondary);">{{ row.mmsi }}</small></td>
              <td>
                <span :class="'tag tag-' + row.tagType">{{ row.violationType }}</span>
                <br><small class="cat-hint">{{ row.categoryLabel }}</small>
              </td>
              <td>
                <template v-if="row.subtypeLabel">
                  <span class="subtype-label">{{ row.subtypeLabel }}</span>
                  <br><small class="subtype-metric">{{ metricText(row) }}</small>
                </template>
                <span v-else style="color:var(--text-secondary);">-</span>
              </td>
              <td><span :class="'risk-level ' + row.riskLevel">{{ riskLabel(row.riskLevel) }}</span></td>
              <td>
                <span class="ql-badge" :class="'ql-' + row.qualityLevel">{{ qlLabel(row.qualityLevel) }}</span>
                <span v-if="row.qualityLevel === 'poor'" class="ql-suspect" title="数据质量较差，结论需人工复核"><AppIcon name="alert-triangle" inline /></span>
              </td>
              <td style="max-width:220px;font-size:12px;color:var(--primary);">
                <span v-if="row.regulationRef">{{ row.regulationRef }}</span>
                <span v-else style="color:var(--text-secondary);">-</span>
              </td>
              <td>
                <button class="btn-link" @click="viewDetail(row)">查看详情</button>
              </td>
            </tr>
          </tbody>
        </table>

        <!-- 移动端：违规卡片（桌面表格由 .data-table 自动隐藏） -->
        <div class="violation-cards">
          <div v-if="list.length === 0" class="vc-empty">
            <span><AppIcon name="wave" :size="36" /></span>
            <p>暂无违规记录</p>
          </div>
          <div
            v-for="row in list"
            :key="row.id"
            class="vc-card"
            :class="'risk-' + row.riskLevel"
          >
            <div class="vc-head">
              <div class="vc-ship">
                <strong>{{ row.shipName }}</strong>
                <small>{{ row.mmsi }}</small>
              </div>
              <span :class="'risk-level ' + row.riskLevel">{{ riskLabel(row.riskLevel) }}</span>
            </div>
            <div class="vc-tags">
              <span :class="'tag tag-' + row.tagType">{{ row.violationType }}</span>
              <span v-if="row.subtypeLabel" class="vc-subtype">{{ row.subtypeLabel }} · {{ metricText(row) }}</span>
              <span class="ql-badge" :class="'ql-' + row.qualityLevel">{{ qlLabel(row.qualityLevel) }}<template v-if="row.qualityLevel === 'poor'"> <AppIcon name="alert-triangle" inline /></template></span>
              <span class="vc-reg" v-if="row.regulationRef"><AppIcon name="book" inline /> {{ row.regulationRef }}</span>
            </div>
            <p class="vc-desc">{{ row.description || '-' }}</p>
            <div class="vc-actions">
              <button class="btn-link" @click="viewDetail(row)">查看详情 →</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 违规详情弹窗（宽弹窗 + 证据卡） -->
    <div v-if="showDetailModal" class="toast-overlay" @click.self="closeDetail">
      <div class="toast-card alert-card modal-evidence">
        <div class="modal-head">
          <span class="modal-icon"><AppIcon name="alert-octagon" :size="32" style="color:#ea4335;" /></span>
          <div class="modal-title">
            <h3>违规详情</h3>
            <small>
              {{ detailItem?.mmsi }}
              · {{ detailItem?.categoryLabel }} / {{ detailItem?.violationType }}
              <template v-if="detailItem?.subtypeLabel"> · {{ detailItem.subtypeLabel }}</template>
            </small>
          </div>
          <button class="modal-close" @click="closeDetail" aria-label="关闭">×</button>
        </div>

        <div v-if="evLoading" class="ev-status">证据加载中…</div>
        <div v-else-if="evError" class="ev-status ev-status-error">{{ evError }}</div>

        <!-- 为什么违规：直接用实测数据回答 -->
        <div v-if="whyItems.length" class="why-bar">
          <div v-for="w in whyItems" :key="w.label" class="why-item" :class="'why-' + (w.tone || 'info')">
            <span class="why-label">{{ w.label }}</span>
            <span class="why-value">{{ w.value }}</span>
          </div>
        </div>

        <div class="alert-detail">
          <div class="alert-row">
            <span class="alert-label">违规类型</span>
            <span class="alert-value">
              <span :class="'tag tag-' + (detailItem?.tagType || 'danger')">{{ detailItem?.violationType }}</span>
              <span v-if="detailItem?.subtypeLabel" class="subtype-inline">· {{ detailItem.subtypeLabel }}</span>
            </span>
          </div>
          <div class="alert-row">
            <span class="alert-label">风险等级</span>
            <span class="alert-value"><span :class="'risk-level ' + (detailItem?.riskLevel || 'high')">{{ riskLabel(detailItem?.riskLevel) }}</span></span>
          </div>
          <div class="alert-row" v-if="detailItem?.qualityLevel">
            <span class="alert-label">数据质量</span>
            <span class="alert-value">
              <span class="ql-badge" :class="'ql-' + detailItem.qualityLevel">{{ qlLabel(detailItem.qualityLevel) }}</span>
              <!-- 只在 poor 时提示：42/42 都是 fair，按"非 good 就提示"会满屏噪音 -->
              <span v-if="detailItem.qualityLevel === 'poor'" style="color:#d63a3a;font-size:12px;margin-left:6px;"><AppIcon name="alert-triangle" inline /> 数据存疑</span>
            </span>
          </div>
          <div class="alert-row">
            <!-- 这是 analysis_time（分析/入库时刻），不是违规发生时刻 ——
                 本批轨迹是 2022 年的、分析发生在 2026 年，标成"违规时间"会误导 -->
            <span class="alert-label">检测时间</span>
            <span class="alert-value">{{ detailItem?.time ? formatTime(detailItem.time) : '实时检测' }}</span>
          </div>
          <div class="alert-row" v-if="detailItem?.regulationRef">
            <span class="alert-label">违反法规</span>
            <span class="alert-value" style="color:var(--primary);">{{ detailItem.regulationRef }}</span>
          </div>
          <div class="alert-row" v-if="detailItem?.description">
            <span class="alert-label">违规详情</span>
            <span class="alert-value">{{ detailItem.description }}</span>
          </div>
          <div class="alert-row" v-if="detailItem?.aiSuggestion">
            <!-- recommended_action 是枚举（自动告警/人工复核/持续监控），
                 不是 LLM 的分析正文 —— 正文在上面「违规详情」里 -->
            <span class="alert-label">处置通道</span>
            <span class="alert-value">{{ detailItem.aiSuggestion }}</span>
          </div>
        </div>

        <!-- 证据区 -->
        <div v-if="evData?.evidence" class="ev-blocks">
          <SpeedTimeChart :data="evData.evidence" />
          <EvidenceCard :data="evData.evidence" />

          <!-- 判定链路 L1 → L2 → L3 → 法规 → 处置 -->
          <div v-if="evData.chain?.length" class="ev-section">
            <div class="ev-section-title">判定链路</div>
            <div class="chain">
              <template v-for="(c, i) in evData.chain" :key="c.stage">
                <div class="chain-node" :class="{ 'chain-bad': !c.ok }">
                  <div class="chain-stage">{{ c.stage }}</div>
                  <div class="chain-name">{{ c.name }}</div>
                  <div class="chain-summary">{{ c.summary }}</div>
                  <div class="chain-detail" :title="c.detail">{{ c.detail }}</div>
                </div>
                <div v-if="i < evData.chain.length - 1" class="chain-arrow">→</div>
              </template>
            </div>
          </div>

          <!-- 证据充分度 -->
          <div v-if="evData.completeness" class="ev-section">
            <div class="ev-section-title">
              证据充分度 {{ completenessPct }}%
              <span class="ev-section-hint">（轨迹 / 事件 / 证据点 / 法规 / 复核 五项具备情况）</span>
            </div>
            <div class="cp-bar"><div class="cp-fill" :style="{ width: completenessPct + '%' }"></div></div>
            <div class="cp-items">
              <span v-for="c in completenessChecks" :key="c.label" :class="c.ok ? 'cp-ok' : 'cp-no'">
                {{ c.ok ? '✓' : '✗' }} {{ c.label }}
              </span>
            </div>
          </div>
        </div>

        <div class="toast-actions" style="gap:12px;">
          <button class="btn btn-secondary" @click="closeDetail">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, computed } from 'vue'
import PageHeader from '../../components/PageHeader.vue'
import AppIcon from '../../components/AppIcon.vue'
import EvidenceCard from '../../components/EvidenceCard.vue'
import SpeedTimeChart from '../../components/SpeedTimeChart.vue'
import { fetchViolations, fetchViolationCatalog, fetchViolationEvidence } from '../../api/violations.js'

const list = ref([])
const total = ref(0)
const showDetailModal = ref(false)
const detailItem = ref(null)
const evData = ref(null)
const evLoading = ref(false)
const evError = ref('')
const searchText = ref('')
const filterType = ref('')
const filterCategory = ref('')
const filterSubtype = ref('')
const catalog = ref({ categories: [], types: [], subtypes: [] })

// 一级 → 二级：按选中类目过滤类型
const filteredTypes = computed(() => {
  if (!filterCategory.value) return catalog.value.types || []
  return (catalog.value.types || []).filter(t => t.category === filterCategory.value)
})

/**
 * 二级 → 三级：可选的细类。
 * 选了类型 → 只列该类型的细类；只选类目 → 列该类目下所有类型的细类；
 * 都没选 → 列全部。细类的 value 一律用完整码（ais_gap#suspected_off），
 * 后端按下推的等值条件匹配，裸键匹配不到。
 */
const filteredSubtypes = computed(() => {
  if (!filterType.value) {
    const codes = new Set((filteredTypes.value || []).map(t => t.code))
    return (catalog.value.subtypes || []).filter(s => codes.has(s.type_code))
  }
  const t = (catalog.value.types || []).find(x => x.code === filterType.value)
  return t?.subtypes || []
})

function rangeText(s) {
  if (s.min == null && s.max == null) return ''
  if (s.min == null) return `（≤${s.max}${s.unit}）`
  if (s.max == null) return `（>${s.min}${s.unit}）`
  return `（${s.min}~${s.max}${s.unit}）`
}

/** 细类列的量值文本：`680 分钟`。 */
function metricText(row) {
  const v = Number(row.metric || 0)
  if (!v) return ''
  return `${v % 1 === 0 ? v : v.toFixed(1)}${row.subtypeUnit || ''}`
}

function riskLabel(r) {
  return r === 'high' ? '高风险' : (r === 'mid' || r === 'middle') ? '中风险' : '低风险'
}

function qlLabel(l) {
  return l === 'good' ? '良好' : l === 'fair' ? '一般' : '较差'
}

function formatTime(t) {
  return t ? t.replace('T', ' ').slice(0, 16) : ''
}

function fmt1(v) {
  return v == null || isNaN(Number(v)) ? '-' : Number(v).toFixed(1)
}

function fmtDur(min) {
  const m = Number(min)
  if (!isFinite(m)) return '-'
  return m >= 60 ? `${(m / 60).toFixed(1)} 小时` : `${m.toFixed(0)} 分钟`
}

/** ISO → 本地毫秒，与 evidence.py::_parse_iso 同口径（把 Z 当本地时间）。 */
function tsMs(s) {
  if (!s) return null
  const t = new Date(String(s).replace('Z', '')).getTime()
  return isNaN(t) ? null : t
}

/** 空档区间内实际有几个 AIS 回传点 —— 0 就是"中断"最直接的证据。 */
function countPointsInGap(track, ev) {
  const a = tsMs(ev?.pre_point?.timestamp)
  const b = tsMs(ev?.post_point?.timestamp)
  if (a == null || b == null) return null
  const lo = Math.min(a, b)
  const hi = Math.max(a, b)
  let n = 0
  for (const p of track || []) {
    const t = tsMs(p.timestamp)
    if (t != null && t > lo && t < hi) n++
  }
  return n
}

/** 指标条：全部取自实测数据，不编造。 */
const whyItems = computed(() => {
  const d = evData.value
  if (!d) return []
  const evidence = d.evidence || {}
  const ev = (evidence.events || [])[0] || {}
  const c = d.completeness || {}
  const out = []

  if (ev.gap_minutes) {
    const mins = Number(ev.gap_minutes)
    // 用分钟不用小时：细类分档的界就是 120/360 分钟，同量纲才好对照
    out.push({ label: '无 AIS 回传', value: `${mins.toFixed(0)} 分钟`, tone: mins >= 360 ? 'danger' : 'warn' })
    const inside = countPointsInGap(evidence.track || [], ev)
    if (inside != null) {
      out.push({ label: '空档内回传点', value: `${inside} 点`, tone: inside === 0 ? 'ok' : 'warn' })
    }
    if (ev.pre_point?.timestamp && ev.post_point?.timestamp) {
      // slice(5) 从 'YYYY-MM-DD HH:MM' 的第 5 位起 → 'MM-DD HH:MM'
      out.push({
        label: '中断区间',
        value: `${formatTime(ev.pre_point.timestamp).slice(5)} ~ ${formatTime(ev.post_point.timestamp).slice(5)}`,
      })
    }
  } else {
    // speed_scope='context' 表示均速取自空档两侧各 20 点，不是区间均速，口径如实标注
    if (ev.avg_speed != null) {
      out.push({ label: ev.speed_scope === 'context' ? '中断前后均速' : '区间均速', value: `${fmt1(ev.avg_speed)} 节` })
    }
    if (ev.max_speed != null) out.push({ label: '最高航速', value: `${fmt1(ev.max_speed)} 节` })
    if (ev.duration_minutes != null) out.push({ label: '持续时长', value: fmtDur(ev.duration_minutes) })
  }

  if (detailItem.value?.subtypeLabel) {
    out.push({ label: '细类判定', value: detailItem.value.subtypeLabel, tone: 'info' })
  }
  if (c.coverageHours) out.push({ label: '轨迹覆盖', value: `${c.coverageHours} 小时` })
  if (c.trackPoints) out.push({ label: '证据轨迹点', value: `${c.trackPoints} 点` })
  if (c.confidence) out.push({ label: '判定置信度', value: `${(Number(c.confidence) * 100).toFixed(0)}%` })
  return out
})

const completenessChecks = computed(() => {
  const c = evData.value?.completeness
  if (!c) return []
  return [
    { label: `航行轨迹 ${c.trackPoints || 0} 点`, ok: !!c.hasTrack },
    { label: `候选事件 ${c.eventCount || 0} 个`, ok: (c.eventCount || 0) > 0 },
    { label: `证据点 ${c.evidencePoints || 0} 个`, ok: (c.evidencePoints || 0) > 0 },
    { label: '法规引用', ok: !!c.hasRegulation },
    { label: 'LLM 复核', ok: !!c.hasAssessment },
  ]
})

const completenessPct = computed(() => {
  const ch = completenessChecks.value
  if (!ch.length) return 0
  return Math.round(ch.filter(c => c.ok).length / ch.length * 100)
})

async function loadCatalog() {
  try {
    catalog.value = await fetchViolationCatalog()
  } catch (e) {
    console.error('catalog 加载失败:', e)
  }
}

async function loadData() {
  try {
    const result = await fetchViolations({
      category: filterCategory.value,
      type: filterType.value,
      subtype: filterSubtype.value,
      search: searchText.value,
      pageSize: 200,
    })
    list.value = result.list || []
    total.value = result.total ?? list.value.length
  } catch (e) {
    console.error('数据加载失败:', e)
  }
}

function onCategoryChange() {
  // 改了上层的可选集合，下面两级的选择可能已不在集合里 —— 必须清掉，
  // 否则会把"新类目 + 旧类型"这种矛盾组合发给后端（后端求交后返回 0 条）
  filterType.value = ''
  filterSubtype.value = ''
  loadData()
}

function onTypeChange() {
  filterSubtype.value = ''
  loadData()
}

async function viewDetail(row) {
  detailItem.value = row
  showDetailModal.value = true
  evData.value = null
  evError.value = ''
  evLoading.value = true
  try {
    evData.value = await fetchViolationEvidence(row.id)
  } catch (e) {
    evError.value = '证据数据加载失败：' + (e?.message || e)
    console.error('证据加载失败:', e)
  } finally {
    evLoading.value = false
  }
}

function closeDetail() {
  showDetailModal.value = false
  detailItem.value = null
  evData.value = null
  evError.value = ''
}

onMounted(async () => {
  await loadCatalog()
  loadData()
})
</script>

<style scoped>
.modal-overlay, .toast-overlay {
  position: fixed; inset: 0; z-index: 10000;
  background: rgba(4, 15, 31, 0.55);
  backdrop-filter: blur(4px);
  display: flex; align-items: center; justify-content: center;
}
.toast-card {
  background: var(--gradient-card);
  border: 1px solid rgba(169, 205, 226, 0.5);
  border-radius: 20px; padding: 36px;
  text-align: center; max-width: 440px; width: 90%;
  box-shadow: 0 24px 70px rgba(2, 15, 34, 0.4);
  animation: toastIn 0.35s ease;
}
@keyframes toastIn {
  from { transform: scale(0.8) translateY(20px); opacity: 0; }
  to { transform: scale(1) translateY(0); opacity: 1; }
}
.alert-card {
  border: 1px solid rgba(239, 83, 80, 0.45);
  box-shadow: 0 24px 70px rgba(2, 15, 34, 0.4), 0 0 0 4px rgba(239, 83, 80, 0.08);
}
/* 证据弹窗：宽版，左对齐，超长可滚 */
.modal-evidence {
  max-width: 1120px; width: 92vw; max-height: 86vh;
  overflow-y: auto; text-align: left; padding: 20px 24px 24px;
}
.modal-head {
  display: flex; align-items: center; gap: 12px;
  padding-bottom: 12px; border-bottom: 1px solid rgba(239, 83, 80, 0.25);
}
.modal-icon { flex-shrink: 0; }
.modal-title { flex: 1; min-width: 0; }
.modal-title h3 { margin: 0; font-size: 17px; color: #ea4335; }
.modal-title small { color: var(--text-secondary); font-size: 12px; }
.modal-close {
  flex-shrink: 0; border: none; background: none; cursor: pointer;
  font-size: 26px; line-height: 1; color: var(--text-secondary); padding: 0 4px;
}
.modal-close:hover { color: #ea4335; }
.ev-status { padding: 14px 0; font-size: 13px; color: var(--text-secondary); text-align: center; }
.ev-status-error { color: #d63a3a; }

/* 为什么违规：指标条 */
.why-bar {
  display: flex; flex-wrap: wrap; gap: 8px;
  margin: 14px 0 4px;
}
.why-item {
  display: flex; flex-direction: column; gap: 2px;
  padding: 8px 14px; border-radius: 10px; min-width: 104px;
  background: rgba(12, 140, 233, 0.07);
  border: 1px solid rgba(12, 140, 233, 0.22);
}
.why-label { font-size: 11px; color: var(--text-secondary); white-space: nowrap; }
.why-value { font-size: 15px; font-weight: 700; color: #0d2b42; white-space: nowrap; }
.why-danger { background: rgba(239, 83, 80, 0.09); border-color: rgba(239, 83, 80, 0.35); }
.why-danger .why-value { color: #d32f2f; }
.why-warn { background: rgba(245, 165, 36, 0.10); border-color: rgba(245, 165, 36, 0.35); }
.why-warn .why-value { color: #b45309; }
.why-ok { background: rgba(15, 169, 131, 0.09); border-color: rgba(15, 169, 131, 0.3); }
.why-ok .why-value { color: #0b8a69; }

.alert-detail {
  background: linear-gradient(180deg, rgba(253, 232, 232, 0.55), rgba(253, 232, 232, 0.25));
  border: 1px solid rgba(239, 83, 80, 0.15);
  border-radius: 12px;
  padding: 12px 16px;
  margin-top: 12px;
}
.alert-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 6px 0;
  font-size: 13px;
  border-bottom: 1px solid rgba(239, 83, 80, 0.12);
}
.alert-row:last-child { border-bottom: none; }
.alert-label { color: var(--text-secondary); font-weight: 500; flex-shrink: 0; }
.alert-value { font-weight: 600; text-align: right; }
.subtype-inline { color: var(--text-secondary); font-weight: 500; margin-left: 4px; }

/* 表格里的细类 */
.cat-hint { color: var(--text-secondary); font-size: 11px; }
.subtype-label { font-weight: 600; color: #b45309; }
.subtype-metric { color: var(--text-secondary); font-size: 11px; }

/* 证据区 */
.ev-blocks { display: flex; flex-direction: column; gap: 12px; margin-top: 14px; }
/* EvidenceCard 自带的轨迹图高度是 220px，宽弹窗里给足高度 */
.modal-evidence :deep(.ev-chart) { height: 320px; }
.ev-section {
  background: #fff; border: 1px solid #d6e7f2; border-radius: 10px;
  padding: 10px 12px; color: #0d2b42;
}
.ev-section-title { font-size: 11px; color: #5d7c92; letter-spacing: 0.5px; margin-bottom: 8px; }
.ev-section-hint { color: #9db4c6; }

/* 判定链路 */
.chain { display: flex; align-items: stretch; gap: 6px; overflow-x: auto; padding-bottom: 4px; }
.chain-node {
  flex: 1 1 0; min-width: 132px;
  border: 1px solid #d6e7f2; border-left: 3px solid #0fa983;
  border-radius: 8px; padding: 8px 10px; background: #f8fcff;
}
.chain-bad { border-left-color: #ef5350; background: #fff6f6; }
.chain-stage { font-size: 10px; font-weight: 700; color: #0c8ce9; letter-spacing: 0.5px; }
.chain-name { font-size: 12px; font-weight: 600; margin: 2px 0 4px; }
.chain-summary { font-size: 11px; line-height: 1.5; }
.chain-detail {
  font-size: 10px; color: #5d7c92; line-height: 1.5; margin-top: 3px;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}
.chain-arrow { display: flex; align-items: center; color: #9db4c6; font-size: 14px; }

/* 证据充分度 */
.cp-bar { height: 8px; border-radius: 999px; background: #eef3f8; overflow: hidden; }
.cp-fill { height: 100%; background: linear-gradient(90deg, #0c8ce9, #18b8a6); transition: width 0.3s; }
.cp-items { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 8px; font-size: 11px; }
.cp-ok { color: #0b8a69; }
.cp-no { color: #d63a3a; }

/* 数据质量徽章 */
.ql-badge {
  display: inline-block; padding: 2px 10px; border-radius: 999px;
  font-size: 12px; font-weight: 600; white-space: nowrap;
  border: 1px solid rgba(255,255,255,0.6);
}
.ql-good { background: #d9f4eb; color: #0b8a69; }
.ql-fair { background: #fef3c7; color: #b45309; }
.ql-poor { background: #fde8e8; color: #d63a3a; }
.ql-suspect { color: #d63a3a; margin-left: 4px; }

/* 移动端：违规卡片（桌面隐藏，由 .data-table 出表格） */
.violation-cards { display: none; }
@media (max-width: 768px) {
    .violation-cards {
        display: flex; flex-direction: column; gap: 12px; padding: 12px;
    }
    .vc-card {
        background: var(--gradient-card);
        border: 1px solid rgba(169, 205, 226, 0.4);
        border-left: 4px solid var(--info);
        border-radius: 12px; padding: 14px 16px;
        box-shadow: 0 2px 10px rgba(6, 31, 56, 0.06);
    }
    .vc-card.risk-high { border-left-color: var(--danger); }
    .vc-card.risk-mid { border-left-color: var(--warning); }
    .vc-card.risk-low { border-left-color: var(--info); }
    .vc-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; margin-bottom: 10px; }
    .vc-ship { display: flex; flex-direction: column; gap: 2px; }
    .vc-ship strong { font-size: 15px; }
    .vc-ship small { font-size: 12px; color: var(--text-secondary); font-family: Consolas, Monaco, monospace; }
    .vc-tags { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
    .vc-subtype { font-size: 11px; font-weight: 600; color: #b45309; }
    .vc-reg { font-size: 11px; color: var(--primary); word-break: break-all; }
    .vc-desc {
        margin: 0 0 10px; font-size: 12px; line-height: 1.6; color: var(--text-secondary);
        display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
    }
    .vc-actions { display: flex; justify-content: flex-end; }
    .vc-empty { text-align: center; color: var(--text-secondary); padding: 24px 0; font-size: 14px; }
    .vc-empty span { font-size: 36px; display: block; margin-bottom: 8px; }
    /* 窄屏：弹窗铺满，链路改成竖排 */
    .modal-evidence { width: 96vw; max-height: 92vh; padding: 16px; }
    .chain { flex-direction: column; }
    .chain-arrow { justify-content: center; transform: rotate(90deg); }
    .alert-row { flex-direction: column; align-items: flex-start; gap: 3px; }
    .alert-value { text-align: left; }
}
</style>
