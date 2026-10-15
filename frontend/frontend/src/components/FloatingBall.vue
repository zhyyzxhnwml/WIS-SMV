<template>
  <div class="ai-float" :class="{ 'is-mobile': isMobile }" :style="{ left: pos.left + 'px', top: pos.top + 'px' }">
    <Transition name="pop">
      <div
        v-if="open"
        class="ai-window"
        :style="{
          width: pos.width + 'px',
          height: pos.height + 'px',
          left: winLeft + 'px',
          top: winTop + 'px',
        }"
      >
        <div class="ai-window-header" @pointerdown="dragWindow">
          <span class="ai-avatar"><AppIcon name="link" :size="16" /></span>
          <div class="ai-window-title">
            <strong>级联日志</strong>
            <small>{{ running ? '● 运行中' : '○ 待命' }} · {{ logs.length }}条</small>
          </div>
          <button class="ai-min" title="收起" @pointerdown.stop @click="open = false">—</button>
        </div>
        <div class="cascade-body" ref="body">
          <div v-if="logs.length === 0" class="cascade-empty">等待级联智能体启动...</div>
          <div v-else class="terminal-line" v-for="(log, i) in logs" :key="i" :class="'terminal-' + log.stage">
            <span class="t-time">{{ log.time }}</span>
            <span class="t-stage" :class="'stage-' + log.stage">{{ stageLabel(log.stage) }}</span>
            <span class="t-msg">{{ log.message }}</span>
          </div>
        </div>
        <div class="cascade-status">
          <span class="cascade-dot" :class="running ? 'dot-running' : 'dot-idle'"></span>
          <span>船舶 {{ totalShips }} | 违规 {{ violatingShips }} | 准确率 {{ accuracy }}%</span>
        </div>
      </div>
    </Transition>

    <button
      class="cascade-fab"
      title="级联日志"
      @pointerdown="dragFab"
    >
      <span><AppIcon name="link" :size="18" /></span>
      <span class="fab-label">级联日志</span>
      <span class="fab-badge" v-if="violatingShips > 0">{{ violatingShips }}</span>
    </button>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import AppIcon from './AppIcon.vue'

// 悬浮球宽度。别当成常量手写死：这里原来写 100，实际渲染 110，而收起面板那边
// 写 100、实际 104 —— 同一套 `innerWidth - FAB_W - 16` 于是算出了 16px 的错位，
// 两个球叠在一起右边就不齐。改为挂载时按 DOM 实量（值本身只是首帧兜底）。
let FAB_W = 100
const FAB_H = 48
const MIN_W = 320
const MIN_H = 400
const BASE_W = 360

const open = ref(false)
const body = ref(null)
const winLeft = ref(0)
const winTop = ref(0)
const logs = ref([])
const running = ref(false)
const totalShips = ref(0)
const violatingShips = ref(0)
const accuracy = ref(0)
let timer = null

// 移动端检测（≤768px 切底部弹层模式，拖拽让位给点按）
const mqMobile = window.matchMedia('(max-width: 768px)')
const isMobile = ref(mqMobile.matches)
function onMqChange(e) { isMobile.value = e.matches }
if (typeof mqMobile.addEventListener === 'function') {
  mqMobile.addEventListener('change', onMqChange)
}

const pos = reactive(loadPos())

const windowScale = computed(() => clamp(pos.width / BASE_W, 0.75, 1.6))

function loadPos() {
  let saved = null
  try { saved = JSON.parse(localStorage.getItem('cascade-float-pos') || 'null') } catch (e) {}
  return { left: 0, top: 0, width: saved?.width || 360, height: saved?.height || 480 }
}

function clamp(v, min, max) { return Math.min(Math.max(v, min), max) }

function savePos() {
  try { localStorage.setItem('cascade-float-pos', JSON.stringify({ width: pos.width, height: pos.height })) } catch (e) {}
}

function stageLabel(stage) {
  const map = { init: '初始化', scan: '扫描', l1: 'L1加速器', l2: 'L2路由', l3: 'L3判定', result: '结果', done: '完成', error: '错误', warn: '警告' }
  return map[stage] || stage
}

async function fetchData() {
  try {
    const [logResp, statResp] = await Promise.all([
      fetch('/api/v1/cascade/logs'),
      fetch('/api/v1/cascade/stats')
    ])
    const logJson = await logResp.json()
    if (logJson.code === 200) {
      logs.value = logJson.data
      // 判断是否在运行（最新日志在数组末尾）
      const last = logJson.data.length > 0 ? logJson.data[logJson.data.length - 1] : null
      running.value = last ? (last.stage !== 'done' && last.stage !== 'error') : false
    }
    const statJson = await statResp.json()
    if (statJson.code === 200) {
      totalShips.value = statJson.data.totalShips
      violatingShips.value = statJson.data.suspiciousShips
      accuracy.value = statJson.data.accuracy
    }
  } catch (e) {
    console.error('级联数据加载失败:', e)
  }
}

onMounted(() => {
  const fabEl = document.querySelector('.cascade-fab')
  if (fabEl) FAB_W = fabEl.getBoundingClientRect().width
  pos.left = Math.max(0, window.innerWidth - FAB_W - 16)
  pos.top = 80
  fetchData()
  timer = setInterval(fetchData, 3000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
  if (typeof mqMobile.removeEventListener === 'function') mqMobile.removeEventListener('change', onMqChange)
})

function beginDrag(e, handlers) {
  if (e.button !== undefined && e.button !== 0) return
  e.preventDefault()
  e.stopPropagation()
  const startX = e.clientX, startY = e.clientY
  const target = e.currentTarget || e.target
  document.body.classList.add('dragging')
  try { target.setPointerCapture?.(e.pointerId) } catch (err) {}
  const move = (ev) => handlers.move(ev, { dx: ev.clientX - startX, dy: ev.clientY - startY })
  const up = () => {
    document.removeEventListener('pointermove', move)
    document.removeEventListener('pointerup', up)
    document.removeEventListener('pointercancel', up)
    document.body.classList.remove('dragging')
    if (handlers.end) handlers.end()
  }
  document.addEventListener('pointermove', move)
  document.addEventListener('pointerup', up)
  document.addEventListener('pointercancel', up)
}

function dragFab(e) {
  if (open.value) { open.value = false; return }
  if (isMobile.value) { open.value = true; return } // 移动端点按直接开合
  const startLeft = pos.left, startTop = pos.top
  let moved = false
  beginDrag(e, {
    move: (_ev, d) => {
      if (Math.abs(d.dx) + Math.abs(d.dy) > 4) moved = true
      pos.left = clamp(startLeft + d.dx, 0, window.innerWidth - FAB_W)
      pos.top = clamp(startTop + d.dy, 0, window.innerHeight - FAB_H)
    },
    end: () => { if (!moved) open.value = true }
  })
}

function dragWindow(e) {
  if (isMobile.value) return // 移动端为底部弹层，不可拖
  const startLeft = pos.left, startTop = pos.top
  beginDrag(e, {
    move: (_ev, d) => {
      pos.left = clamp(startLeft + d.dx, 0, window.innerWidth - FAB_W)
      pos.top = clamp(startTop + d.dy, 0, window.innerHeight - 80)
    }
  })
}

watch(open, (v) => {
  if (isMobile.value) return // 移动端位置由 CSS 接管
  if (v) {
    winLeft.value = FAB_W - pos.width
    winTop.value = -(pos.height + 14)
    const topEdge = pos.top + winTop.value
    if (topEdge < 0) pos.top = Math.min(pos.height + 24, window.innerHeight - FAB_H - 8)
    scrollToBottom()
  }
})

async function scrollToBottom() {
  await nextTick()
  if (body.value) body.value.scrollTop = body.value.scrollHeight
}
</script>

<style scoped>
.ai-float { position: fixed; z-index: 9999; }

.cascade-fab {
    position: relative; display: flex; align-items: center; gap: 6px;
    background: linear-gradient(135deg, #0a3d62 0%, #0c8ce9 100%);
    border: 2px solid rgba(118, 200, 255, 0.45); color: #fff;
    padding: 6px 14px 6px 10px; border-radius: 999px; cursor: grab;
    font-size: 14px; user-select: none; touch-action: none;
    box-shadow: 0 6px 20px rgba(12, 140, 233, 0.4);
    transition: transform 0.2s ease;
}
.cascade-fab::after {
    content: ''; position: absolute; inset: -2px; border-radius: 999px;
    background: linear-gradient(90deg, transparent, rgba(24,184,166,0.6), transparent);
    opacity: 0; transition: opacity 0.3s; z-index: -1;
}
.cascade-fab:hover { transform: translateY(-2px) scale(1.04); box-shadow: 0 8px 24px rgba(12, 140, 233, 0.5); }
.cascade-fab:hover::after { opacity: 0.5; animation: fabSweep 1.4s ease infinite; }
@keyframes fabSweep {
    0% { transform: translateX(-100%); }
    100% { transform: translateX(100%); }
}
.cascade-fab:active { cursor: grabbing; transform: scale(1.02); }
/* 几何量和 FloatingAIChat 的 .ai-fab 对齐（标签字号、徽章形状与偏移），
   两个球上下叠着，差一档就参差。 */
.fab-label { font-weight: 600; letter-spacing: 0.5px; font-size: 13px; }
.fab-badge {
    position: absolute; top: -6px; right: -6px;
    background: linear-gradient(135deg, #ef5350, #ff8a65); color: #fff;
    min-width: 20px; height: 20px; border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    padding: 0 5px;
    font-size: 11px; font-weight: 700; border: 2px solid #fff;
    box-shadow: 0 2px 8px rgba(239, 83, 80, 0.5);
}

.ai-window {
    position: absolute;
    background: linear-gradient(180deg, #071f3a 0%, #061f38 60%, #040f1f 100%);
    border-radius: 12px;
    box-shadow: 0 16px 48px rgba(2, 15, 34, 0.6);
    border: 1px solid rgba(118, 200, 255, 0.22);
    display: flex; flex-direction: column; overflow: hidden;
    min-width: 320px; min-height: 400px;
}
.ai-window-header {
    display: flex; align-items: center; gap: 10px; padding: 10px 14px;
    background: linear-gradient(90deg, rgba(10, 61, 98, 0.9), rgba(8, 36, 61, 0.85));
    color: #cfe6f7; cursor: grab; user-select: none; touch-action: none;
    border-bottom: 1px solid rgba(118, 200, 255, 0.18);
}
.ai-window-header:active { cursor: grabbing; }
.ai-avatar {
    width: 28px; height: 28px; border-radius: 50%;
    background: linear-gradient(135deg, rgba(12, 140, 233, 0.4), rgba(24, 184, 166, 0.35));
    border: 1px solid rgba(118, 200, 255, 0.35);
    display: flex; align-items: center; justify-content: center;
    font-size: 14px; flex-shrink: 0;
}
.ai-window-title { flex: 1; }
.ai-window-title strong { font-size: 13px; display: block; color: #eaf6ff; }
.ai-window-title small { font-size: 11px; opacity: 0.6; color: rgba(150, 210, 245, 0.7); }
.ai-min { background: none; border: none; color: rgba(150, 210, 245, 0.6); cursor: pointer; font-size: 16px; padding: 0 4px; border-radius: 4px; }
.ai-min:hover { color: #7ff0e2; background: rgba(12, 140, 233, 0.2); }

/* 终端日志区域 */
.cascade-body {
    flex: 1; overflow-y: auto; padding: 10px 12px;
    font-family: 'Cascadia Code', 'Fira Code', 'Consolas', 'Courier New', monospace;
    font-size: 12px; line-height: 1.7;
    color: #cfe6f7;
}
.cascade-body::-webkit-scrollbar { width: 6px; }
.cascade-body::-webkit-scrollbar-track { background: #061f38; }
.cascade-body::-webkit-scrollbar-thumb { background: rgba(118, 200, 255, 0.22); border-radius: 3px; }
.cascade-empty { text-align: center; padding: 40px 0; color: rgba(150, 210, 245, 0.35); font-style: italic; }

.terminal-line {
    padding: 1px 0;
    white-space: pre-wrap;
    word-break: break-all;
}
.t-time { color: rgba(150, 210, 245, 0.4); margin-right: 6px; }
.t-stage {
    display: inline-block; min-width: 70px;
    font-weight: 600; margin-right: 6px;
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

.t-msg { color: #cfe6f7; }
.terminal-l3 .t-msg { color: #ffb74d; }
.terminal-result .t-msg { color: #39d6b0; }
.terminal-error .t-msg { color: #ef5350; }
.terminal-done .t-msg { color: #2ec4a0; font-weight: 600; }

.cascade-status {
    display: flex; align-items: center; gap: 6px;
    padding: 6px 14px; border-top: 1px solid rgba(118, 200, 255, 0.18);
    background: linear-gradient(90deg, rgba(10, 61, 98, 0.8), rgba(8, 36, 61, 0.75));
    font-size: 11px; color: rgba(150, 210, 245, 0.7);
    font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
}
.cascade-dot {
    width: 8px; height: 8px; border-radius: 50%;
    display: inline-block;
}
.dot-running { background: #2ec4a0; box-shadow: 0 0 8px rgba(46, 196, 160, 0.7); animation: dotBlink 2s ease-in-out infinite; }
.dot-idle { background: rgba(150, 210, 245, 0.4); }
@keyframes dotBlink { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }

.pop-enter-active, .pop-leave-active { transition: all 0.2s ease; }
.pop-enter-from, .pop-leave-to { opacity: 0; transform: translateY(12px) scale(0.98); }

/* ── 移动端：FAB 钉在标签栏上方，窗口变全宽底部弹层 ── */
@media (max-width: 768px) {
    .ai-float.is-mobile {
        left: auto !important; top: auto !important;
        right: 14px !important;
        bottom: calc(var(--tabbar-height) + var(--tabbar-safe-bottom) + 14px) !important;
    }
    .ai-float.is-mobile .ai-window {
        position: fixed !important;
        left: 0 !important; right: 0 !important; top: auto !important; bottom: 0 !important;
        width: 100vw !important; max-width: 100vw !important;
        height: 70vh !important; height: 70dvh !important;
        max-height: 70vh !important;
        min-width: 0 !important; min-height: 0 !important;
        border-radius: 16px 16px 0 0 !important;
    }
}
</style>
