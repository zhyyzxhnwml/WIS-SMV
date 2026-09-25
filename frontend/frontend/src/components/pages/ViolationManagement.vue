<template>
  <div class="page">
    <PageHeader icon="alert-triangle" title="违规监管" subtitle="违规船舶监测、筛选与处置" />

    <!-- ==================== 顶部工具栏 ==================== -->
    <div class="toolbar">
      <div class="toolbar-left">
        <input type="text" class="search-input" placeholder="搜索MMSI..." v-model="searchText" @input="loadData">
        <select class="form-select" v-model="filterType" @change="loadData">
          <option value="">全部违规类型</option>
          <option>AIS信号异常/关闭</option>
          <option>航向异常/偏离航线</option>
          <option>超速/限速违规</option>
          <option>进入禁航/限制区域</option>
          <option>违规捕捞</option>
          <option>碰撞风险</option>
          <option>其他违规</option>
        </select>
      </div>
      <div class="toolbar-right">
        <button class="btn btn-primary" @click="loadData"><AppIcon name="refresh-cw" inline /> 刷新</button>
      </div>
    </div>

    <!-- ==================== 违规船舶列表 ==================== -->
    <div class="card">
      <div class="card-header">
        <h3>违规船舶列表</h3>
        <span class="card-subtitle">系统检测到的违规船舶记录</span>
      </div>
      <div class="card-body" style="padding:0;">
        <table class="data-table">
          <thead>
            <tr>
              <th>时间</th>
              <th>船舶</th>
              <th>违规类型</th>
              <th>风险等级</th>
              <th>违规描述</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="list.length === 0">
              <td colspan="6" style="text-align:center;color:var(--text-secondary);padding:40px;">暂无违规记录</td>
            </tr>
            <tr v-for="row in list" :key="row.id" :class="'alert-' + row.riskLevel">
              <td>{{ formatTime(row.time) }}</td>
              <td>
                <strong>{{ row.shipName }}</strong><br>
                <small style="color:#999;">{{ row.mmsi }}</small>
              </td>
              <td>
                <span :class="'tag tag-' + row.tagType" :title="row.violationMeaning || ''">{{ row.violationType }}</span>
                <small v-if="row.rawType" style="display:block;color:#999;font-size:11px;">{{ row.rawType }}</small>
              </td>
              <td><span :class="'risk-level ' + row.riskLevel">{{ riskLabel(row.riskLevel) }}</span></td>
              <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;" :title="row.description || ''">
                {{ row.description || '-' }}
              </td>
              <td>
                <button class="btn-link" @click="viewViolation(row)"><AppIcon name="clipboard" inline /> 查看</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- ==================== 违规详情弹窗 ==================== -->
    <div v-if="showAlertToast" class="toast-overlay" @click.self="showAlertToast = false">
      <div class="toast-card alert-card">
        <div class="alert-icon"><AppIcon name="alert-octagon" :size="56" style="color:#ea4335;" /></div>
        <h3 style="color:#ea4335;">船舶违规详情</h3>
        <div class="toast-body" style="text-align:left;padding:16px 0;">
          <div class="alert-detail">
            <div class="alert-row">
              <span class="alert-label">船舶编号</span>
              <span class="alert-value">{{ alertShip?.mmsi }}</span>
            </div>
            <div class="alert-row">
              <span class="alert-label">违规类型</span>
              <span class="alert-value"><span :class="'tag tag-' + (alertShip?.tagType || 'danger')">{{ alertShip?.violationType }}</span></span>
            </div>
            <div class="alert-row">
              <span class="alert-label">风险等级</span>
              <span class="alert-value"><span :class="'risk-level ' + (alertShip?.riskLevel || 'high')">{{ riskLabel(alertShip?.riskLevel) }}</span></span>
            </div>
            <div class="alert-row">
              <span class="alert-label">违规时间</span>
              <span class="alert-value">{{ alertShip?.time ? formatTime(alertShip.time) : '实时检测' }}</span>
            </div>
            <div class="alert-row" v-if="alertShip?.violationMeaning">
              <span class="alert-label">违规含义</span>
              <span class="alert-value">{{ alertShip.violationMeaning }}</span>
            </div>
            <div class="alert-row" v-if="alertShip?.description">
              <span class="alert-label">违规详情</span>
              <span class="alert-value">{{ alertShip.description }}</span>
            </div>
            <div class="alert-row" v-if="alertShip?.regulationRef">
              <span class="alert-label">法规依据</span>
              <span class="alert-value" style="text-align:left;max-width:260px;line-height:1.6;">{{ alertShip.regulationRef }}</span>
            </div>
            <div class="alert-row" v-if="alertShip?.aiSuggestion">
              <span class="alert-label">AI 分析</span>
              <span class="alert-value">{{ alertShip.aiSuggestion }}</span>
            </div>
          </div>
        </div>
        <div class="toast-actions" style="gap:12px;">
          <button class="btn btn-secondary" @click="showAlertToast = false">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import PageHeader from '../../components/PageHeader.vue'
import AppIcon from '../../components/AppIcon.vue'
import { fetchViolations } from '../../api/violations.js'

const list = ref([])
const filterType = ref('')
const searchText = ref('')

const showAlertToast = ref(false)
const alertShip = ref(null)

function riskLabel(r) {
  return r === 'high' ? '高风险' : (r === 'mid' || r === 'middle') ? '中风险' : '低风险'
}

function formatTime(t) {
  return t ? t.replace('T', ' ').slice(0, 16) : ''
}

async function loadData() {
  try {
    const result = await fetchViolations({
      type: filterType.value,
      search: searchText.value,
    })
    list.value = result.list
  } catch (e) {
    console.error('违规数据加载失败:', e)
  }
}

function viewViolation(row) {
  alertShip.value = row
  showAlertToast.value = true
}

onMounted(() => loadData())
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
</style>