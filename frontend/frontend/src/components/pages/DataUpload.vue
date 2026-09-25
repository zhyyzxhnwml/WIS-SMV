<template>
  <div class="page">
    <PageHeader icon="upload" title="数据上传" subtitle="上传 AIS CSV 数据，系统自动解析并关联到您的账号" />

    <div class="card" style="max-width:720px;margin:0 auto;">
      <div class="card-header">
        <h3><AppIcon name="upload" inline /> 上传船舶数据</h3>
        <span class="card-subtitle">上传 CSV 格式的 AIS 数据，系统自动解析并关联到您的账号</span>
      </div>
      <div class="card-body">
        <!-- 上传区域 -->
        <div class="upload-zone"
             @dragover.prevent="dragOver = true"
             @dragleave="dragOver = false"
             @drop.prevent="handleDrop"
             :class="{ 'drag-over': dragOver }"
             @click="triggerFileInput">
          <input type="file" ref="fileInput" accept=".csv" style="display:none" @change="handleFileSelect">
          <div class="upload-icon"><AppIcon name="folder" :size="48" /></div>
          <div class="upload-text">
            <strong class="upload-main">点击选择 CSV 文件</strong> 或拖拽到此处
          </div>
          <div class="upload-hint">支持标准 AIS CSV 格式（mmsi, lat, lon, speed, time 等字段）</div>
          <div class="upload-hint hint-warn">数据仅关联到您的账号，不影响其他用户</div>
        </div>

        <!-- 选中文件后显示 -->
        <div v-if="selectedFile" class="file-info">
          <span class="file-name"><AppIcon name="file-text" inline /> {{ selectedFile.name }}</span>
          <span class="file-size">{{ formatSize(selectedFile.size) }}</span>
        </div>

        <!-- 上传进度/结果 -->
        <div v-if="uploading" class="upload-progress">
          <div class="progress-bar"><div class="progress-fill"></div></div>
          <span>正在解析上传...</span>
        </div>

        <div v-if="result" :class="['result-box', result.shipsImported > 0 ? 'success' : 'warn']">
          <strong v-if="result.shipsImported > 0"><AppIcon name="check-circle" inline /> 上传成功</strong>
          <strong v-else><AppIcon name="alert-triangle" inline /> 上传完成</strong>
          <p>{{ result.message }}</p>
          <p style="font-size:12px;color:var(--text-secondary);margin-top:4px;">
            共 {{ result.totalRows }} 行，有效 {{ result.validRows }} 行，船舶 {{ result.shipsImported }} 艘
          </p>
        </div>

        <button class="btn btn-primary upload-btn" @click="doUpload"
                :disabled="!selectedFile || uploading" v-if="selectedFile">
          <AppIcon name="send" inline /> {{ uploading ? '上传中...' : '开始上传' }}
        </button>
      </div>
    </div>

    <!-- 已上传数据概览 -->
    <div class="card" style="max-width:720px;margin:20px auto 0;">
      <div class="card-header">
        <h3><AppIcon name="bar-chart" inline /> 我的数据</h3>
        <span class="card-subtitle">您已上传的数据统计</span>
      </div>
      <div class="card-body">
        <div v-if="!userStats" style="text-align:center;padding:20px;color:var(--text-secondary);">加载中...</div>
        <div v-else class="data-stats">
          <div class="data-stat-item">
            <span class="ds-value">{{ userStats.ships }}</span>
            <span class="ds-label">船舶数</span>
          </div>
          <div class="data-stat-item">
            <span class="ds-value">{{ userStats.aisPoints }}</span>
            <span class="ds-label">AIS 数据点</span>
          </div>
        </div>
        <button class="btn btn-secondary" @click="loadUserStats" style="margin-top:12px;"><AppIcon name="refresh-cw" inline /> 刷新统计</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import PageHeader from '../../components/PageHeader.vue'
import AppIcon from '../../components/AppIcon.vue'

const fileInput = ref(null)
const selectedFile = ref(null)
const uploading = ref(false)
const dragOver = ref(false)
const result = ref(null)
const userStats = ref(null)

function triggerFileInput() { fileInput.value?.click() }

function handleFileSelect(e) {
  const f = e.target.files[0]
  if (f) { selectedFile.value = f; result.value = null }
}

function handleDrop(e) {
  dragOver.value = false
  const f = e.dataTransfer?.files?.[0]
  if (f && f.name.endsWith('.csv')) { selectedFile.value = f; result.value = null }
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + 'B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + 'KB'
  return (bytes / 1024 / 1024).toFixed(1) + 'MB'
}

async function doUpload() {
  if (!selectedFile.value) return
  uploading.value = true
  result.value = null
  const form = new FormData()
  form.append('file', selectedFile.value)

  try {
    const resp = await fetch('/api/v1/data/upload', { method: 'POST', body: form })
    const json = await resp.json()
    if (json.code === 200) {
      result.value = json.data
      loadUserStats()
    } else {
      result.value = { shipsImported: 0, message: json.message || '上传失败', totalRows: 0, validRows: 0 }
    }
  } catch (e) {
    result.value = { shipsImported: 0, message: '请求失败: ' + e.message, totalRows: 0, validRows: 0 }
  } finally {
    uploading.value = false
  }
}

async function loadUserStats() {
  try {
    const resp = await fetch('/api/v1/data/user-stats')
    const json = await resp.json()
    if (json.code === 200) userStats.value = json.data
  } catch (e) { console.error('加载统计数据失败:', e) }
}

onMounted(() => loadUserStats())
</script>

<style scoped>
.upload-zone {
  border: 2px dashed rgba(12, 140, 233, 0.35);
  border-radius: 18px;
  padding: 44px 20px;
  text-align: center;
  cursor: pointer;
  transition: all 0.3s ease;
  background: linear-gradient(160deg, #f4fbff 0%, #ffffff 60%, #f0f8fd 100%);
  position: relative;
  overflow: hidden;
}
.upload-zone::before {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px;
  background: linear-gradient(90deg, transparent, var(--teal), var(--primary), var(--purple), transparent);
  opacity: 0.6;
}
.upload-zone:hover, .upload-zone.drag-over {
  border-color: var(--primary);
  background: linear-gradient(160deg, #eaf6fe 0%, #f8fdff 60%, #e6f3fb 100%);
  box-shadow: 0 0 0 4px rgba(12, 140, 233, 0.12), var(--shadow-lg);
  transform: translateY(-2px);
}
.upload-icon { font-size: 48px; margin-bottom: 12px; display: inline-block; color: var(--primary); }
.upload-zone:hover .upload-icon { animation: waveBounce 0.8s ease; }
@keyframes waveBounce {
  0% { transform: translateY(0) rotate(0); }
  40% { transform: translateY(-10px) rotate(-5deg); }
  70% { transform: translateY(2px) rotate(3deg); }
  100% { transform: translateY(0) rotate(0); }
}
.upload-text { font-size: 15px; margin-bottom: 8px; color: var(--text); }
.upload-main { color: var(--primary); font-weight: 700; }
.upload-hint { font-size: 12px; color: var(--text-secondary); margin-top: 6px; }
.hint-warn { color: var(--warning) !important; font-weight: 500; }
.file-info {
  display: flex; justify-content: space-between; align-items: center;
  padding: 12px 16px;
  background: linear-gradient(90deg, rgba(12, 140, 233, 0.08), rgba(24, 184, 166, 0.06), rgba(12, 140, 233, 0.04));
  border: 1px solid rgba(12, 140, 233, 0.2);
  border-radius: 12px; margin-top: 16px;
}
.file-name { font-weight: 600; font-size: 14px; color: var(--text); }
.file-size { font-size: 12px; color: var(--text-secondary); }
.upload-progress {
  display: flex; align-items: center; gap: 12px;
  padding: 16px 0; font-size: 13px; color: var(--text-secondary);
}
.progress-bar { flex: 1; height: 8px; background: rgba(214, 231, 242, 0.7); border-radius: 4px; overflow: hidden; }
.progress-fill {
  height: 100%; width: 100%;
  background: linear-gradient(90deg, var(--teal), var(--primary), var(--purple));
  background-size: 200% 100%; border-radius: 4px;
  animation: progressFlow 1.6s ease infinite;
}
@keyframes progressFlow {
  0% { background-position: 0% 0; width: 30%; }
  50% { background-position: 100% 0; width: 70%; }
  100% { background-position: 0% 0; width: 30%; }
}
.result-box {
  padding: 16px; border-radius: 12px; margin-top: 16px; font-size: 14px; color: var(--text);
}
.result-box.success {
  background: linear-gradient(160deg, rgba(15, 169, 131, 0.1), rgba(15, 169, 131, 0.04));
  border: 1px solid rgba(15, 169, 131, 0.35);
}
.result-box.warn {
  background: linear-gradient(160deg, rgba(245, 165, 36, 0.12), rgba(245, 165, 36, 0.05));
  border: 1px solid rgba(245, 165, 36, 0.4);
}
.upload-btn { margin-top: 16px; width: 100%; padding: 12px !important; font-size: 15px !important; }
.data-stats { display: flex; gap: 24px; }
.data-stat-item {
  text-align: center; flex: 1;
  padding: 16px 8px; border-radius: var(--radius-sm);
  background: linear-gradient(160deg, #f4fbff 0%, #ffffff 70%);
  border: 1px solid rgba(169, 205, 226, 0.35);
  transition: all var(--transition);
}
.data-stat-item:hover { transform: translateY(-2px); box-shadow: var(--shadow); }
.ds-value {
  font-size: 32px; font-weight: 700; display: block;
  background: linear-gradient(135deg, #0a3d62 0%, var(--primary) 80%);
  -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
}
.ds-label { font-size: 13px; color: var(--text-secondary); }

/* 小屏：上传区收紧 */
@media (max-width: 480px) {
    .upload-zone { padding: 28px 16px; }
}
</style>