<template>
  <div class="page">
    <PageHeader icon="clipboard" title="违规监控" subtitle="所有违规船舶信息汇总">
      <button class="btn btn-primary" @click="loadData"><AppIcon name="refresh-cw" inline /> 刷新</button>
    </PageHeader>

    <!-- 搜索 / 筛选工具栏 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <input type="text" class="search-input" placeholder="搜索船名 / MMSI..." v-model="searchText" @input="loadData">
        <select class="form-select" v-model="filterCategory" @change="onCategoryChange">
          <option value="">全部类目</option>
          <option v-for="c in catalog.categories" :key="c.name" :value="c.name">{{ c.label }} ({{ c.ship_count }})</option>
        </select>
        <select class="form-select" v-model="filterType" @change="loadData" style="min-width:140px;">
          <option value="">全部类型</option>
          <option v-for="t in filteredTypes" :key="t.code" :value="t.code">{{ t.label }} ({{ t.ship_count }})</option>
        </select>
      </div>
      <div class="toolbar-right">
        <span class="toolbar-hint">共 <strong>{{ list.length }}</strong> 条记录</span>
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
              <th>风险等级</th>
              <th>数据质量</th>
              <th>违反法规</th>
              <th>违规描述</th>
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
              <td><span :class="'tag tag-' + row.tagType">{{ row.violationType }}</span></td>
              <td><span :class="'risk-level ' + row.riskLevel">{{ riskLabel(row.riskLevel) }}</span></td>
              <td>
                <span class="ql-badge" :class="'ql-' + row.qualityLevel">{{ qlLabel(row.qualityLevel) }}</span>
                <span v-if="row.qualityLevel && row.qualityLevel !== 'good'" class="ql-suspect"><AppIcon name="alert-triangle" inline /></span>
              </td>
              <td style="max-width:220px;font-size:12px;color:var(--primary);">
                <span v-if="row.regulationRef">{{ row.regulationRef }}</span>
                <span v-else style="color:var(--text-secondary);">-</span>
              </td>
              <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;">{{ row.description || '-' }}</td>
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
              <span class="ql-badge" :class="'ql-' + row.qualityLevel">{{ qlLabel(row.qualityLevel) }}<template v-if="row.qualityLevel && row.qualityLevel !== 'good'"> <AppIcon name="alert-triangle" inline /></template></span>
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

    <!-- 违规详情弹窗 -->
    <div v-if="showDetailModal" class="toast-overlay" @click.self="showDetailModal = false">
      <div class="toast-card alert-card">
        <div class="alert-icon"><AppIcon name="alert-octagon" :size="56" style="color:#ea4335;" /></div>
        <h3 style="color:#ea4335;">违规详情</h3>
        <div class="toast-body" style="text-align:left;padding:16px 0;">
          <div class="alert-detail">
            <div class="alert-row">
              <span class="alert-label">船舶编号</span>
              <span class="alert-value">{{ detailItem?.mmsi }}</span>
            </div>
            <div class="alert-row">
              <span class="alert-label">违规类型</span>
              <span class="alert-value"><span :class="'tag tag-' + (detailItem?.tagType || 'danger')">{{ detailItem?.violationType }}</span></span>
            </div>
            <div class="alert-row">
              <span class="alert-label">风险等级</span>
              <span class="alert-value"><span :class="'risk-level ' + (detailItem?.riskLevel || 'high')">{{ riskLabel(detailItem?.riskLevel) }}</span></span>
            </div>
            <div class="alert-row" v-if="detailItem?.qualityLevel">
              <span class="alert-label">数据质量</span>
              <span class="alert-value">
                <span class="ql-badge" :class="'ql-' + detailItem.qualityLevel">{{ qlLabel(detailItem.qualityLevel) }}</span>
                <span v-if="detailItem.qualityLevel !== 'good'" style="color:#d63a3a;font-size:12px;margin-left:6px;"><AppIcon name="alert-triangle" inline /> 数据存疑</span>
              </span>
            </div>
            <div class="alert-row">
              <span class="alert-label">违规时间</span>
              <span class="alert-value">{{ detailItem?.time ? formatTime(detailItem.time) : (detailItem?.createdAt ? formatTime(detailItem.createdAt) : '实时检测') }}</span>
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
              <span class="alert-label">AI 分析</span>
              <span class="alert-value">{{ detailItem.aiSuggestion }}</span>
            </div>
          </div>
        </div>
        <div class="toast-actions" style="gap:12px;">
          <button class="btn btn-secondary" @click="showDetailModal = false">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, computed } from 'vue'
import PageHeader from '../../components/PageHeader.vue'
import AppIcon from '../../components/AppIcon.vue'
import { fetchViolations, fetchViolationCatalog } from '../../api/violations.js'

const list = ref([])
const showDetailModal = ref(false)
const detailItem = ref(null)
const searchText = ref('')
const filterType = ref('')
const filterCategory = ref('')
const catalog = ref({ categories: [], types: [] })

// 根据选中类目过滤二级类型
const filteredTypes = computed(() => {
  if (!filterCategory.value) return catalog.value.types || []
  return (catalog.value.types || []).filter(t => t.category === filterCategory.value)
})

function onCategoryChange() {
  filterType.value = ''
  loadData()
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
      type: filterType.value,
      search: searchText.value,
    })
    list.value = result.list
  } catch (e) {
    console.error('数据加载失败:', e)
  }
}

function viewDetail(row) {
  detailItem.value = row
  showDetailModal.value = true
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
.alert-icon {
  font-size: 56px; margin-bottom: 8px;
  animation: alertPulse 1s ease-in-out infinite alternate;
}
@keyframes alertPulse {
  from { transform: scale(1); }
  to { transform: scale(1.1); }
}
.alert-detail {
  background: linear-gradient(180deg, rgba(253, 232, 232, 0.55), rgba(253, 232, 232, 0.25));
  border: 1px solid rgba(239, 83, 80, 0.15);
  border-radius: 12px;
  padding: 12px 16px;
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
.alert-label { color: var(--text-secondary); font-weight: 500; }
.alert-value { font-weight: 600; text-align: right; }

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
    .vc-reg { font-size: 11px; color: var(--primary); word-break: break-all; }
    .vc-desc {
        margin: 0 0 10px; font-size: 12px; line-height: 1.6; color: var(--text-secondary);
        display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
    }
    .vc-actions { display: flex; justify-content: flex-end; }
    .vc-empty { text-align: center; color: var(--text-secondary); padding: 24px 0; font-size: 14px; }
    .vc-empty span { font-size: 36px; display: block; margin-bottom: 8px; }
}
</style>