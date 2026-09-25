<template>
  <div class="page">
    <PageHeader icon="zap" title="级联日志" subtitle="三级级联智能体运行日志与状态统计" />

    <!-- 顶部统计卡片 -->
    <div class="stats-grid">
      <div class="stat-card stat-blue">
        <div class="stat-icon"><AppIcon name="zap" :size="30" /></div>
        <div class="stat-info">
          <span class="stat-value">{{ stats.totalShips }}</span>
          <span class="stat-label">已分析船舶</span>
        </div>
      </div>
      <div class="stat-card stat-orange">
        <div class="stat-icon"><AppIcon name="search" :size="30" /></div>
        <div class="stat-info">
          <span class="stat-value">{{ stats.suspiciousShips }}</span>
          <span class="stat-label">嫌疑船舶</span>
        </div>
      </div>
      <div class="stat-card stat-green">
        <div class="stat-icon"><AppIcon name="check-circle" :size="30" /></div>
        <div class="stat-info">
          <span class="stat-value">{{ stats.normalShips }}</span>
          <span class="stat-label">正常船舶</span>
        </div>
      </div>
      <div class="stat-card stat-purple">
        <div class="stat-icon"><AppIcon name="trending-up" :size="30" /></div>
        <div class="stat-info">
          <span class="stat-value">{{ stats.accuracy }}%</span>
          <span class="stat-label">三级分析准确率</span>
        </div>
      </div>
    </div>

    <!-- 级联日志终端 -->
    <div class="card" style="margin-top:20px;">
      <div class="card-header" style="display:flex;justify-content:space-between;align-items:center;">
        <h3>级联日志</h3>
        <div style="display:flex;gap:8px;align-items:center;">
          <span class="log-status" :class="running ? 'running' : 'idle'">{{ running ? '● 运行中' : '○ 待命' }}</span>
          <button class="btn-refresh" @click="loadLogs">刷新</button>
          <label class="auto-refresh-label">
            <input type="checkbox" v-model="autoRefresh" /> 自动刷新
          </label>
        </div>
      </div>
      <div class="terminal" ref="terminalRef">
        <div v-if="logs.length === 0" class="terminal-line terminal-idle">等待级联智能体启动...</div>
        <div v-for="(log, i) in logs" :key="i" class="terminal-line" :class="'terminal-' + log.stage">
          <span class="terminal-time">{{ log.time }}</span>
          <span class="terminal-stage" :class="'stage-' + log.stage">{{ stageLabel(log.stage) }}</span>
          <span class="terminal-msg">{{ log.message }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import PageHeader from '../../components/PageHeader.vue'
import AppIcon from '../../components/AppIcon.vue'

const stats = ref({
  totalShips: 0, suspiciousShips: 0, normalShips: 0,
  accuracy: 0, l1Processed: 0, l2Routed: 0, l3Judged: 0
})
const logs = ref([])
const running = ref(false)
const autoRefresh = ref(true)
const terminalRef = ref(null)
let timer = null

function stageLabel(stage) {
  const map = { init: '初始化', scan: '扫描', l1: 'L1加速器', l2: 'L2路由', l3: 'L3判定', result: '结果', done: '完成', error: '错误', warn: '警告' }
  return map[stage] || stage
}

async function loadStats() {
  try {
    const resp = await fetch('/api/v1/cascade/stats')
    const json = await resp.json()
    if (json.code === 200) stats.value = json.data
  } catch (e) { console.error(e) }
}

async function loadLogs() {
  try {
    const resp = await fetch('/api/v1/cascade/logs')
    const json = await resp.json()
    if (json.code === 200) {
      logs.value = json.data
      // 判断是否在运行（最新日志在数组末尾）
      const last = json.data.length > 0 ? json.data[json.data.length - 1] : null
      running.value = last ? (last.stage !== 'done' && last.stage !== 'error') : false
      // 自动滚到底部（最新日志在最下面）
      await nextTick()
      if (terminalRef.value) terminalRef.value.scrollTop = terminalRef.value.scrollHeight
    }
  } catch (e) { console.error(e) }
}

onMounted(() => {
  loadStats()
  loadLogs()
  timer = setInterval(() => {
    if (autoRefresh.value) {
      loadLogs()
      loadStats()
    }
  }, 3000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.stats-grid {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px;
}
.stat-card {
  background: var(--gradient-card); border-radius: var(--radius); padding: 20px;
  box-shadow: var(--shadow); display: flex; align-items: center; position: relative; overflow: hidden;
  border: 1px solid rgba(169, 205, 226, 0.4);
  transition: transform var(--transition), box-shadow var(--transition);
}
.stat-card:hover { transform: translateY(-3px); box-shadow: var(--shadow-lg); }
.stat-card::after {
  content: ''; position: absolute; right: -30px; bottom: -30px;
  width: 90px; height: 90px; border-radius: 50%;
  background: radial-gradient(circle, rgba(12,140,233,0.1), transparent 70%);
}
.stat-card::before { content: ''; position: absolute; top: 0; left: 0; width: 4px; height: 100%; }
.stat-blue::before { background: linear-gradient(180deg, #22c9b8, #0d9488); }
.stat-orange::before { background: linear-gradient(180deg, #ffb74d, #f57c00); }
.stat-green::before { background: linear-gradient(180deg, #39d6b0, #0fa983); }
.stat-purple::before { background: linear-gradient(180deg, #9f8ff5, #7c6ff0); }
.stat-icon { font-size: 32px; margin-right: 16px; }
.stat-blue .stat-icon { color: var(--teal-dark); }
.stat-orange .stat-icon { color: #d97706; }
.stat-green .stat-icon { color: var(--success); }
.stat-purple .stat-icon { color: #5b4bc4; }
.stat-info { flex: 1; position: relative; z-index: 1; }
.stat-value { font-size: 28px; font-weight: 700; display: block; line-height: 1.2; }
.stat-label { font-size: 13px; color: var(--text-secondary); }
.stat-orange .stat-value { color: #b45309; }
.stat-green .stat-value { color: #0b8a69; }
.stat-purple .stat-value { color: #5b4bc4; }

/* 终端样式 — 深海洋暗色 */
.terminal {
  background: linear-gradient(180deg, #071f3a 0%, #061f38 60%, #040f1f 100%);
  color: #cfe6f7;
  font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
  font-size: 13px;
  line-height: 1.6;
  padding: 16px;
  border: 1px solid rgba(118, 200, 255, 0.18);
  border-top: none;
  border-radius: 0 0 var(--radius) var(--radius);
  max-height: 600px;
  overflow-y: auto;
  min-height: 300px;
  box-shadow: inset 0 4px 20px rgba(4, 15, 31, 0.4);
}
.terminal::-webkit-scrollbar { width: 8px; }
.terminal::-webkit-scrollbar-track { background: #061f38; }
.terminal::-webkit-scrollbar-thumb { background: rgba(118, 200, 255, 0.22); border-radius: 4px; }
.terminal::-webkit-scrollbar-thumb:hover { background: rgba(118, 200, 255, 0.4); }

.terminal-line {
  padding: 2px 0;
  white-space: pre-wrap;
  word-break: break-all;
}
.terminal-time {
  color: rgba(150, 210, 245, 0.4);
  margin-right: 8px;
}
.terminal-stage {
  display: inline-block;
  min-width: 80px;
  font-weight: 600;
  margin-right: 8px;
}
.stage-init { color: #4db8ff; }
.stage-scan { color: #79d0ff; }
.stage-l1 { color: #9f8ff5; }
.stage-l2 { color: #ff8f5c; }
.stage-l3 { color: #ff6b5f; }
.stage-result { color: #39d6b0; }
.stage-done { color: #2ec4a0; }
.stage-error { color: #ff5555; }
.stage-warn { color: #f5a524; }

.terminal-msg { color: #cfe6f7; }
.terminal-l3 .terminal-msg { color: #ffb74d; }
.terminal-result .terminal-msg { color: #39d6b0; }
.terminal-error .terminal-msg { color: #ef5350; }
.terminal-done .terminal-msg { color: #2ec4a0; font-weight: 600; }
.terminal-idle { color: rgba(150, 210, 245, 0.35); font-style: italic; }

.log-status { font-size: 12px; font-weight: 600; }
.log-status.running { color: #2ec4a0; text-shadow: 0 0 8px rgba(46, 196, 160, 0.5); }
.log-status.idle { color: rgba(150, 210, 245, 0.5); }

.btn-refresh {
  background: var(--gradient-primary); color: #fff; border: none; border-radius: 999px;
  padding: 4px 14px; font-size: 12px; cursor: pointer; box-shadow: 0 2px 8px rgba(12, 140, 233, 0.3);
  transition: all var(--transition);
}
.btn-refresh:hover { opacity: 0.9; transform: translateY(-1px); }

.auto-refresh-label {
  font-size: 12px; color: var(--text-secondary); display: flex; align-items: center; gap: 4px; cursor: pointer;
}
.auto-refresh-label input { cursor: pointer; accent-color: var(--primary); }

/* 移动端：卡片头换行、终端自适应 */
@media (max-width: 768px) {
    .card-header { align-items: flex-start !important; flex-wrap: wrap; row-gap: 8px; }
    .card-header > div { width: 100%; justify-content: space-between; flex-wrap: wrap; }
    .terminal { max-height: 50vh; min-height: 0; font-size: 12px; padding: 12px; }
}
</style>
