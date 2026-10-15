<template>
  <div class="ai-float" :class="{ 'is-mobile': isMobile }" :style="{ left: pos.left + 'px', top: pos.top + 'px' }">
    <Transition name="pop">
      <div
        v-if="open"
        class="ai-window glass"
        :style="{
          width: pos.width + 'px',
          height: pos.height + 'px',
          left: winLeft + 'px',
          top: winTop + 'px',
        }"
      >
        <!-- 标题栏 -->
        <div class="ai-window-header" @pointerdown="dragWindow">
          <span class="ai-avatar" style="background: rgba(26,115,232,0.35);">
            <img v-if="avatarIsImage" :src="aiConfig.avatar" alt="avatar" />
            <AppIcon v-else :name="aiConfig.avatar" :size="15" />
          </span>
          <div class="ai-window-title">
            <strong>智能体 · {{ aiConfig.name }}</strong>
            <small>Lv{{ status.personality?.level || 1 }} · {{ tone }} · 交互 {{ status.interaction_count || 0 }} 次</small>
          </div>
          <div class="hdr-tabs" @pointerdown.stop>
            <button
              v-for="tab in tabs"
              :key="tab.key"
              class="hdr-tab"
              :class="{ active: tabKey === tab.key }"
              @click="tabKey = tab.key"
            ><AppIcon :name="tab.icon" inline /> {{ tab.label }}</button>
          </div>
          <button class="ai-min" title="收起" @pointerdown.stop @click="open = false">—</button>
        </div>

        <!-- 对话 -->
        <div v-show="tabKey === 'chat'" class="ai-body">
          <div class="ai-chat-body" ref="chatBody">
            <div v-for="(m, i) in messages" :key="i" :class="['ai-msg', m.role]">
              <span v-if="m.role === 'ai'" class="ai-avatar sm">
                <img v-if="avatarIsImage" :src="aiConfig.avatar" alt="avatar" />
                <AppIcon v-else :name="aiConfig.avatar" :size="13" />
              </span>
              <div class="ai-bubble">
                <div class="bubble-text">{{ m.content }}</div>
                <!-- 结构化证据卡片（违规证据链技能）：判定 + 轨迹图 + 事件表格 -->
                <EvidenceCard v-if="m.structured" :data="m.structured" />
                <!-- 渲染型工具产出（画图/写表格/写文档）：按 kind 内联渲染 -->
                <RenderBlocks v-if="m.blocks && m.blocks.length" :blocks="m.blocks" />
                <!-- 工具调用记录：图标 + 名称 + 说明 + 状态 -->
                <div v-if="m.tools && m.tools.length" class="tool-calls">
                  <div v-for="(t, ti) in m.tools" :key="ti" class="tool-call">
                    <span class="tool-icon"><AppIcon :name="t.icon || 'tool'" :size="15" /></span>
                    <div class="tool-meta">
                      <span class="tool-name">{{ t.label || t.name }}</span>
                      <span class="tool-desc">{{ t.description }}</span>
                    </div>
                    <span class="tool-status" :class="t.status === 'locked' ? 'locked' : 'ok'">
                      <AppIcon :name="t.status === 'locked' ? 'lock' : 'check'" inline /> {{ t.status === 'locked' ? '未解锁' : '已调用' }}
                    </span>
                  </div>
                </div>
                <!-- 造插件回复：给一个直达草稿的按钮。
                     造完之后用户的下一步必然是去试跑/启用它，让他自己去翻菜单
                     找不到入口的话，这个功能等于没做。 -->
                <button v-if="m.forge" class="forge-link" @click="goDraft(m.forge)">
                  <AppIcon name="puzzle" inline /> 查看草稿「{{ m.forge.label }}」→
                </button>
                <span v-if="m.exp" class="bubble-exp">+{{ m.exp }} exp</span>
              </div>
              <span v-if="m.role === 'user'" class="ai-avatar sm you">{{ userName }}</span>
            </div>
            <div v-if="sending" class="ai-msg ai">
              <span class="ai-avatar sm">
                <img v-if="avatarIsImage" :src="aiConfig.avatar" alt="avatar" />
                <AppIcon v-else :name="aiConfig.avatar" :size="13" />
              </span>
              <div class="ai-bubble typing"><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>
            </div>
          </div>

          <div class="ai-chat-input">
            <div class="chat-quick-actions">
              <button v-for="q in quickQuestions" :key="q" class="quick-btn" @click="sendQuick(q)">{{ q }}</button>
            </div>
            <div class="chat-input-row">
              <input
                type="text"
                v-model="text"
                class="chat-input"
                :placeholder="'输入您的问题，发送给' + aiConfig.name + '...'"
                @keydown.enter="send"
              >
              <button class="chat-send" :disabled="sending" @click="send"><AppIcon name="send" :size="16" /></button>
            </div>
          </div>
        </div>

        <!-- 养成 -->
        <div v-show="tabKey === 'grow'" class="ai-body grow-body">
          <div class="grow-section">
            <div class="grow-head">
              <span class="grow-title">成长等级</span>
              <span class="grow-badge">Lv{{ status.personality?.level || 1 }}</span>
            </div>
            <div class="exp-bar"><div class="exp-fill" :style="{ width: (status.personality?.exp_progress || 0) * 100 + '%' }"></div></div>
            <div class="exp-meta">
              <span>EXP {{ status.personality?.exp || 0 }}</span>
              <span v-if="status.personality?.exp_to_next > 0">距 Lv{{ status.personality?.level + 1 }} 还差 {{ status.personality?.exp_to_next }}</span>
              <span v-else>已达满级</span>
            </div>
          </div>

          <div class="trait-table">
            <div v-for="t in TRAIT_NAMES" :key="t" class="trait-row">
              <span class="trait-name">{{ t }}</span>
              <div class="trait-track"><div class="trait-fill" :style="{ width: (traits[t] ?? 50) + '%' }"></div></div>
              <span class="trait-val">{{ traits[t] ?? 50 }}</span>
              <div class="trait-btns">
                <button class="trait-btn" @click="traitDelta(t, -10)">–</button>
                <button class="trait-btn" @click="traitDelta(t, 10)">+</button>
              </div>
            </div>
          </div>
          <div class="grow-actions">
            <button class="grow-save" @click="saveTraits">保存性格</button>
            <span class="grow-tip">性格会随对话自动成长，也可手动微调</span>
          </div>

          <div class="grow-section" v-if="log.length">
            <div class="grow-head"><span class="grow-title">成长日志</span></div>
            <div class="grow-log">
              <div v-for="(e, i) in log" :key="i" class="log-line" :class="{ learn: e.tag === 'learned' || e.scope === 'preference', milestone: e.milestone || e.event?.indexOf('解锁') > -1 }">
                <span class="log-time">{{ fmtTime(e.time) }}</span>
                <span class="log-emoji"><AppIcon :name="logEmoji(e)" :size="12" /></span>
                <span class="log-text">{{ e.event }}</span>
              </div>
            </div>
          </div>
        </div>

        <!-- 记忆设置 -->
        <div v-show="tabKey === 'mem'" class="ai-body mem-body">
          <div class="mem-section">
            <div class="grow-head"><span class="grow-title">智能体形象</span></div>
            <div class="form-row">
              <input type="text" class="form-input" v-model="nameInput" placeholder="名称">
              <input type="text" class="form-input" v-model="avatarInput" placeholder="头像图片地址或表情">
            </div>
            <button class="grow-save full" @click="saveProfile">保存形象</button>
          </div>

          <div class="mem-section">
            <div class="grow-head"><span class="grow-title">个性化记忆</span><span class="auto-badge">自动学习</span></div>
            <div class="form-group">
              <label>语言风格</label>
              <div class="chips">
                <button
                  v-for="s in STYLES"
                  :key="s.value"
                  class="chip"
                  :class="{ active: styleInput === s.value }"
                  @click="styleInput = s.value"
                >{{ s.label }}</button>
              </div>
            </div>
            <div class="form-group">
              <label>关注区域 <span class="hint">每行一个，对话会自动学习</span></label>
              <textarea class="form-textarea" rows="3" v-model="zonesInput" placeholder="如：福州港&#10;闽江口锚地"></textarea>
            </div>
            <div class="form-group">
              <label>常查船舶 <span class="hint">每行一个 MMSI</span></label>
              <textarea class="form-textarea" rows="2" v-model="shipsInput" placeholder="如：413123456"></textarea>
            </div>
            <button class="grow-save full" @click="savePrefs">保存记忆</button>
          </div>

          <div class="mem-section" v-if="status.last_interaction">
            <div class="grow-head"><span class="grow-title">养成统计</span></div>
            <div class="stats-grid">
              <div class="stat-item"><b>{{ status.interaction_count || 0 }}</b><span>累计交互</span></div>
              <div class="stat-item"><b>Lv{{ status.personality?.level || 1 }}</b><span>当前等级</span></div>
              <div class="stat-item"><b>{{ (status.personality?.skills_used || []).length }}</b><span>已学技能</span></div>
              <div class="stat-item"><b>{{ (status.preferences?.focus_zones || []).length }}</b><span>关注区域</span></div>
            </div>
            <div class="mem-tip">最后交互：{{ status.last_interaction.replace('T', ' ').slice(0, 16) }}</div>
          </div>
        </div>

        <!-- 工具 -->
        <div v-show="tabKey === 'tools'" class="ai-body tools-body">
          <div class="mem-section">
            <div class="grow-head"><span class="grow-title">智能体工具</span><span class="auto-badge">数据查询</span></div>
            <p class="tools-intro">对话时智能体会自动调用合适的工具查询您的数据，每次调用都会在对话中展示。</p>
            <div v-for="t in tools" :key="t.name" class="tool-row" :class="{ locked: (status.personality?.level || 1) < (t.min_level || 1) }">
              <span class="tool-row-icon"><AppIcon :name="t.icon || 'tool'" :size="17" /></span>
              <div class="tool-row-meta">
                <span class="tool-row-name">{{ t.label }} <small>Lv{{ t.min_level }}</small></span>
                <span class="tool-row-desc">{{ t.description }}</span>
              </div>
            </div>
            <p v-if="!tools.length" class="tools-intro">工具目录加载中…</p>
          </div>
        </div>

        <!-- 状态栏 -->
        <div class="ai-status">
          <span class="status-dot" :class="clawOk ? 'ok' : 'bad'"></span>
          <span>{{ clawOk ? '记忆系统在线' : '离线 · 仅基础对话' }}</span>
          <span class="flex-sep"></span>
          <span>{{ userName }}</span>
        </div>
      </div>
    </Transition>

    <!-- 悬浮球 -->
    <button class="ai-fab" title="智能体" @pointerdown="dragFab">
      <span class="fab-ico">
        <img v-if="avatarIsImage" :src="aiConfig.avatar" alt="avatar" />
        <AppIcon v-else :name="aiConfig.avatar" :size="18" />
      </span>
      <span class="fab-label">智能体</span>
      <span v-if="status.interaction_count" class="fab-badge">{{ status.interaction_count }}</span>
      <span v-if="newExp" class="fab-exp" :key="newExp">{{ newExp }}<small>exp</small></span>
    </button>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { aiConfig, updateAiProfile, isImageUrl } from '../store/ai.js'
import { user } from '../store/user.js'
import { sendChat, fetchTools } from '../api/ai.js'
import { fetchStatus, fetchPersonality, updatePersonality, fetchGrowthLog, updatePreferences } from '../api/claw.js'
import AppIcon from './AppIcon.vue'
import EvidenceCard from './EvidenceCard.vue'
import RenderBlocks from './RenderBlocks.vue'

const avatarIsImage = computed(() => isImageUrl(aiConfig.avatar))

// 造插件成功后跳到开发者页的插件分区，并带上插件 id。
// 光跳到列表不够 —— 插件多了以后用户还得自己找，入口等于没做顺；
// DevPlugins 会据 plugin 参数把它滚到眼前并高亮。
const router = useRouter()
function goDraft(forge) {
  open.value = false
  router.push({
    path: '/dev',
    query: { tab: 'plugins', ...(forge?.plugin_id ? { plugin: forge.plugin_id } : {}) }
  })
}

// 同 FloatingBall：别手写死宽度，挂载时按 DOM 实量，两个球的右边才对齐
let FAB_W = 104
const FAB_H = 48
const BASE_W = 380

// 对话状态按登录用户隔离；未登录兜底 'default'
const userId = computed(() => user.id || 'default')
const TRAIT_NAMES = ['专业度', '亲切度', '严谨度', '主动性']
const STYLES = [
  { value: 'auto', label: '自动' },
  { value: 'professional', label: '专业' },
  { value: 'casual', label: '亲切' }
]
const quickQuestions = [
  '413570530 的违规证据链',
  '查询闽海之星 413123456 的最近轨迹',
  '我的数据概况',
  '福州港主航道限速多少？',
  '我的违规记录'
]

const tabs = [
  { key: 'chat', icon: 'message-circle', label: '对话' },
  { key: 'tools', icon: 'tool', label: '工具' },
  { key: 'grow', icon: 'trending-up', label: '养成' },
  { key: 'mem', icon: 'cpu', label: '记忆' }
]

// 移动端检测（≤768px 切底部弹层模式，拖拽让位给点按）
const mqMobile = window.matchMedia('(max-width: 768px)')
const isMobile = ref(mqMobile.matches)
function onMqChange(e) { isMobile.value = e.matches }
if (typeof mqMobile.addEventListener === 'function') {
  mqMobile.addEventListener('change', onMqChange)
}

const open = ref(false)
const tabKey = ref('chat')
const chatBody = ref(null)
const text = ref('')
const messages = ref([])
const sending = ref(false)
const newExp = ref(0)
const status = ref({})
const traits = ref({})
const log = ref([])
const tools = ref([])

const nameInput = ref('')
const avatarInput = ref('')
const styleInput = ref('auto')
const zonesInput = ref('')
const shipsInput = ref('')

const pos = reactive(loadPos())
const winLeft = ref(0)
const winTop = ref(0)

const clawOk = computed(() => !!status.value?.personality)
const tone = computed(() => status.value?.personality?.tone || '平衡风格')
const userName = computed(() => (user.name || localStorage.getItem('zh-user-name') || '我').slice(0, 4))

const windowScale = computed(() => clamp(pos.width / BASE_W, 0.75, 1.6))

function clamp(v, min, max) { return Math.min(Math.max(v, min), max) }

// v2：窗口从 400×540 收到 400×500。只减高度是因为它本来高得空 —— 而宽度一收，
// 底部那排建议词就从 2 行挤成 3 行，反而更乱。换 key 是因为老 key 里存的就是旧默认值
// 本身，沿用旧 key 的话所有老用户都还是 540，改了等于没改。
const POS_KEY = 'ai-float-pos-v2'
const DEF_W = 400
const DEF_H = 500

function loadPos() {
  let saved = null
  try { saved = JSON.parse(localStorage.getItem(POS_KEY) || 'null') } catch (e) {}
  return { left: 0, top: 0, width: saved?.width || DEF_W, height: saved?.height || DEF_H }
}

function savePos() {
  try { localStorage.setItem(POS_KEY, JSON.stringify({ width: pos.width, height: pos.height })) } catch (e) {}
}

function fmtTime(t) {
  if (!t) return ''
  const d = new Date(t)
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}`
}

function logEmoji(e) {
  if (e.tag === 'learned' || e.scope === 'preference') return 'cpu'
  if (e.event?.indexOf('解锁') > -1) return 'unlock'
  if (e.milestone) return 'star'
  if (e.event?.indexOf('升级') > -1) return 'send'
  if (e.event?.indexOf('技能') > -1) return 'zap'
  return 'sparkles'
}

// ── 拖动（Pointer Events，兼容鼠标 + 触摸） ──
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
      pos.left = clamp(startLeft + d.dx, 0, window.innerWidth - 100)
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
    refreshAll()
    scrollToBottom()
  }
})

async function scrollToBottom() {
  await nextTick()
  if (chatBody.value) chatBody.value.scrollTop = chatBody.value.scrollHeight
}

// ── 数据加载 ──
async function refreshStatus() {
  try {
    status.value = await fetchStatus(userId.value)
    traits.value = { ...(status.value?.personality?.traits || {}) }
  } catch (e) {
    status.value = {}
    traits.value = {}
    console.error('状态加载失败:', e)
  }
}

async function refreshLog() {
  try { log.value = await fetchGrowthLog(userId.value, 30) } catch (e) { log.value = [] }
}

async function refreshPrefs() {
  try {
    const p = status.value?.preferences
    styleInput.value = p?.language_style || 'auto'
    zonesInput.value = (p?.focus_zones || []).join('\n')
    shipsInput.value = (p?.frequent_ships || []).join('\n')
  } catch (e) {}
}

async function refreshTools() {
  try { tools.value = await fetchTools() } catch (e) { tools.value = [] }
}

async function refreshAll() {
  await refreshStatus()
  refreshLog()
  refreshPrefs()
  refreshTools()
  if (!nameInput.value) nameInput.value = aiConfig.name
  if (!avatarInput.value) avatarInput.value = aiConfig.avatar
}

// ── 对话 ──
async function sendQuick(q) {
  text.value = q
  await send()
}

async function send() {
  const t = text.value.trim()
  if (!t || sending.value) return
  text.value = ''
  messages.value.push({ role: 'user', content: t })
  sending.value = true
  scrollToBottom()
  try {
    const r = await sendChat({ message: t, user_id: userId.value })
    const reply = r.reply || '抱歉，我暂时无法回答。'
    messages.value.push({ role: 'ai', content: reply, exp: r.exp_gained, tools: r.tool_calls || [], structured: r.structured || null, blocks: r.blocks || null, forge: r.forge || null })
    if (r.growth_event) messages.value.push({ role: 'ai', content: r.growth_event, meta: true })
    if (r.exp_gained) {
      newExp.value = r.exp_gained
      setTimeout(() => { newExp.value = 0 }, 1600)
    }
    if (r.personality) {
      traits.value = { ...r.personality.traits }
      status.value = { ...status.value, personality: r.personality }
    }
    refreshLog()
    refreshPrefs()
  } catch (e) {
    messages.value.push({ role: 'ai', content: '请求失败，请检查后端服务是否启动' })
  }
  sending.value = false
  scrollToBottom()
}

// ── 养成 ──
function traitDelta(name, delta) {
  const cur = traits.value[name] ?? 50
  traits.value[name] = clamp(Number(cur) + delta, 0, 100)
}

async function saveTraits() {
  try {
    const updated = await updatePersonality(traits.value, userId.value)
    traits.value = { ...updated.traits }
    status.value = { ...status.value, personality: updated }
    await refreshLog()
  } catch (e) {
    console.error('保存性格失败:', e)
  }
}

// ── 记忆设置 ──
async function saveProfile() {
  try {
    await updateAiProfile({
      name: nameInput.value.trim() || aiConfig.name,
      avatar: avatarInput.value.trim() || aiConfig.avatar
    })
    nameInput.value = aiConfig.name
    avatarInput.value = aiConfig.avatar
  } catch (e) {
    console.error('保存形象失败:', e)
  }
}

async function savePrefs() {
  const preferences = {
    focus_zones: zonesInput.value.split('\n').map(s => s.trim()).filter(Boolean),
    frequent_ships: shipsInput.value.split('\n').map(s => s.trim()).filter(Boolean),
    language_style: styleInput.value
  }
  try {
    const updated = await updatePreferences(preferences, userId.value)
    status.value = { ...status.value, preferences: updated }
    await refreshLog()
  } catch (e) {
    console.error('保存记忆失败:', e)
  }
}

onMounted(() => {
  const fabEl = document.querySelector('.ai-fab')
  if (fabEl) FAB_W = fabEl.getBoundingClientRect().width
  pos.left = Math.max(0, window.innerWidth - FAB_W - 16)
  pos.top = 140
  nameInput.value = aiConfig.name
  avatarInput.value = aiConfig.avatar
  messages.value = [{
    role: 'ai',
    content: `您好！我是${aiConfig.name}，港口船舶智能助手。有问题尽管问，我会记住您的偏好慢慢成长。`
  }]
  refreshStatus()
  refreshTools()
})

onUnmounted(() => {
  savePos()
  if (typeof mqMobile.removeEventListener === 'function') mqMobile.removeEventListener('change', onMqChange)
})
</script>

<style scoped>
.ai-float { position: fixed; z-index: 10001; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif; }
/* 几何量（边框/内边距/gap/图标盒/标签字号/徽章）刻意和 FloatingBall 的
   .cascade-fab 保持一致：两个球上下叠在一起，谁差一档都会显得参差。
   徽章用 min-width + 圆角矩形而不是固定 20px 圆 —— 「121」这种三位数才不会被挤扁。 */
.ai-fab {
    position: relative; display: flex; align-items: center; gap: 6px;
    background: rgba(15, 26, 46, 0.88);
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    border: 2px solid rgba(26,115,232,0.65); color: #fff;
    padding: 6px 14px 6px 10px; border-radius: 999px; cursor: grab;
    font-size: 14px; user-select: none; touch-action: none;
    box-shadow: 0 8px 26px rgba(10,20,45,0.55);
    transition: transform 0.2s ease, box-shadow 0.2s ease;
}
.ai-fab:hover { transform: translateY(-2px) scale(1.04); box-shadow: 0 12px 30px rgba(26,115,232,0.45); }
.ai-fab:active { cursor: grabbing; transform: scale(1.02); }
.fab-ico { font-size: 18px; line-height: 1; width: 18px; height: 18px; flex-shrink: 0; }
.fab-ico img { width: 100%; height: 100%; border-radius: 50%; object-fit: cover; display: block; }
.fab-label { font-weight: 600; letter-spacing: 0.5px; font-size: 13px; }
.fab-badge {
    position: absolute; top: -6px; right: -6px;
    background: linear-gradient(135deg, #1a73e8, #7c3aed);
    color: #fff; min-width: 20px; height: 20px; border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    padding: 0 5px; font-size: 11px; font-weight: 700; border: 2px solid #fff;
}
.fab-exp {
    position: absolute; left: 50%; transform: translateX(-50%);
    top: -27px; background: rgba(26,115,232,0.95); color: #fff;
    padding: 2px 9px; border-radius: 10px; font-size: 10px; font-weight: 700;
    box-shadow: 0 4px 12px rgba(0,0,0,0.35);
    animation: floatUp 1.6s ease forwards; white-space: nowrap;
}
.fab-exp small { font-weight: 500; opacity: 0.8; }
@keyframes floatUp {
    0% { opacity: 0; transform: translate(-50%, 8px); }
    15% { opacity: 1; }
    70% { opacity: 1; }
    100% { opacity: 0; transform: translate(-50%, -18px); }
}

/* 毛玻璃窗口 */
.ai-window {
    position: absolute; min-width: 360px; min-height: 420px;
    display: flex; flex-direction: column; overflow: hidden;
    border-radius: 14px;
}
.glass {
    background: rgba(15, 26, 46, 0.86);
    backdrop-filter: blur(18px) saturate(1.4);
    -webkit-backdrop-filter: blur(18px) saturate(1.4);
    border: 1px solid rgba(255,255,255,0.12);
    box-shadow: 0 20px 60px rgba(0,0,0,0.55);
    color: #e8f0fe;
}
.ai-window-header {
    /* 必须换行：标题和 4 个 tab 挤不进同一行（见下面 .hdr-tabs 的说明） */
    display: flex; flex-wrap: wrap; align-items: center;
    gap: 6px 10px; padding: 9px 12px;
    background: rgba(26,115,232,0.16);
    border-bottom: 1px solid rgba(255,255,255,0.1);
    cursor: grab; user-select: none; touch-action: none;
}
.ai-window-header:active { cursor: grabbing; }
.ai-avatar {
    width: 30px; height: 30px; border-radius: 50%;
    background: rgba(255,255,255,0.1);
    display: flex; align-items: center; justify-content: center;
    font-size: 15px; flex-shrink: 0;
    overflow: hidden;
}
.ai-avatar img { width: 100%; height: 100%; object-fit: cover; }
.ai-avatar:not(.sm) { border: 1px solid rgba(26,115,232,0.5); }
.ai-avatar.sm { width: 26px; height: 26px; font-size: 13px; }
.ai-avatar.you { background: rgba(124,58,237,0.35); }
/* 标题吃掉第 1 行的剩余宽度。tab 移到第 2 行后这里有 ~297px，
   两行都能完整显示；行高收紧是为了让整个头部不至于撑太高。 */
.ai-window-title { flex: 1; min-width: 0; }
.ai-window-title strong { font-size: 13px; line-height: 1.35; display: block; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
/* small 也加溢出保护：它原来只有 nowrap，父盒一旦变窄就整条溢出去压到 tab 上 */
.ai-window-title small { font-size: 11px; line-height: 1.35; opacity: 0.55; color: #b9c6dd; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: block; }

/* tab 独占一行。原来它和标题同行：4 个 tab 常驻 237px，而标题的基准宽度是 0
   （.ai-window-title 写了 flex: 1，即 flex-basis: 0），400px 的窗口里只剩 50px
   —— strong 被截成「智能…」，small 那 194px 更是整条溢出、压到 tab 上。
   移动端布局本来就是这么分的（见文末媒体查询），这里提到基础样式。 */
.hdr-tabs {
    order: 5; width: 100%; display: flex; gap: 2px;
    border-top: 1px solid rgba(255,255,255,0.1);
    margin-top: 1px; padding-top: 6px;
}
.hdr-tab {
    background: none; border: none; color: rgba(255,255,255,0.6);
    padding: 4px 8px; border-radius: 7px; font-size: 12px; cursor: pointer;
    transition: all 0.15s; white-space: nowrap;
}
.hdr-tab:hover { background: rgba(255,255,255,0.08); color: #fff; }
.hdr-tab.active { background: rgba(26,115,232,0.55); color: #fff; font-weight: 600; }

.ai-min { background: none; border: none; color: rgba(255,255,255,0.55); cursor: pointer; font-size: 17px; padding: 0 5px; line-height: 1; }
.ai-min:hover { color: #fff; }

.ai-body { flex: 1; overflow-y: auto; display: flex; flex-direction: column; min-height: 0; padding: 12px; gap: 12px; }
.ai-body::-webkit-scrollbar { width: 6px; }
.ai-body::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.18); border-radius: 3px; }
.ai-body::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.3); }

/* ── 对话 ── */
.ai-chat-body { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; padding: 2px 2px 6px; }
.ai-chat-body::-webkit-scrollbar { width: 6px; }
.ai-chat-body::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.18); border-radius: 3px; }
.ai-msg { display: flex; gap: 7px; max-width: 100%; }
.ai-msg.user { flex-direction: row-reverse; }
.ai-bubble {
    background: rgba(255,255,255,0.09);
    border: 1px solid rgba(255,255,255,0.08);
    padding: 8px 11px; border-radius: 11px;
    font-size: 13px; line-height: 1.7; color: #e8f0fe;
    max-width: 82%; word-break: break-word;
}
.ai-msg.user .ai-bubble { background: rgba(26,115,232,0.42); border-color: rgba(26,115,232,0.6); }
.ai-msg.meta { justify-content: center; }
.ai-msg.meta .ai-bubble {
    background: rgba(124,58,237,0.18); border-color: rgba(124,58,237,0.4);
    font-size: 12px; padding: 5px 12px; color: #c4b5fd; max-width: 100%;
}
.bubble-exp { display: inline-flex; align-items: center; gap: 4px; margin-left: 6px; font-size: 11px; color: #7ee787; font-weight: 700; }
/* 造插件回复里的「查看草稿」—— 用主色描边引出，与工具调用记录的灰色块区分开 */
.forge-link {
  display: flex; align-items: center; gap: 6px; width: 100%; margin-top: 9px;
  padding: 8px 12px; border-radius: 8px; cursor: pointer;
  font-size: 12.5px; font-weight: 600; text-align: left;
  color: #7cc4ff; background: rgba(26, 115, 232, 0.16);
  border: 1px solid rgba(26, 115, 232, 0.45); transition: all 0.15s;
}
.forge-link:hover { background: rgba(26, 115, 232, 0.3); color: #a9d6ff; }

/* ── 对话中的工具调用卡片 ── */
.tool-calls { display: flex; flex-direction: column; gap: 5px; margin-top: 8px; border-top: 1px dashed rgba(255,255,255,0.14); padding-top: 7px; }
.tool-call {
    display: flex; align-items: flex-start; gap: 7px;
    background: rgba(26,115,232,0.10); border: 1px solid rgba(26,115,232,0.28);
    border-radius: 8px; padding: 6px 9px;
}
.tool-icon { font-size: 14px; line-height: 1.4; flex-shrink: 0; }
.tool-meta { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.tool-name { font-size: 12px; font-weight: 700; color: #cfe3ff; }
.tool-desc { font-size: 11px; color: rgba(185,198,221,0.75); line-height: 1.45; }
.tool-status { font-size: 10px; font-weight: 600; flex-shrink: 0; padding-top: 1px; }
.tool-status.ok { color: #7ee787; }
.tool-status.locked { color: #f5a524; }

/* ── 工具 tab ── */
.tools-body { overflow-y: auto; }
.tools-intro { font-size: 11px; color: rgba(185,198,221,0.7); line-height: 1.6; margin: 0 0 10px; }
.tool-row {
    display: flex; gap: 9px; padding: 8px 4px;
    border-bottom: 1px dashed rgba(255,255,255,0.08); align-items: flex-start;
}
.tool-row:last-child { border-bottom: none; }
.tool-row.locked { opacity: 0.45; }
.tool-row-icon { font-size: 16px; line-height: 1.5; flex-shrink: 0; }
.tool-row-meta { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.tool-row-name { font-size: 12px; font-weight: 700; color: #e8f0fe; }
.tool-row-name small { font-weight: 500; color: rgba(157,180,217,0.65); margin-left: 4px; }
.tool-row-desc { font-size: 11px; color: rgba(185,198,221,0.75); line-height: 1.5; }
.typing .dot { display: inline-block; width: 5px; height: 5px; margin-right: 4px; border-radius: 50%; background: #9db4d9; animation: blink 1.2s infinite; }
.typing .dot:nth-child(2) { animation-delay: 0.2s; }
.typing .dot:nth-child(3) { animation-delay: 0.4s; }
@keyframes blink { 0%, 80%, 100% { opacity: 0.25; } 40% { opacity: 1; } }

.ai-chat-input { flex-shrink: 0; }
.chat-quick-actions { display: flex; gap: 6px; margin-bottom: 8px; flex-wrap: wrap; }
.quick-btn {
    padding: 4px 10px; border: 1px solid rgba(255,255,255,0.14); border-radius: 14px;
    background: rgba(255,255,255,0.05); color: #b9c6dd; font-size: 11px; cursor: pointer;
    transition: all 0.15s; white-space: nowrap;
}
.quick-btn:hover { background: rgba(26,115,232,0.35); border-color: rgba(26,115,232,0.7); color: #fff; }
.chat-input-row { display: flex; gap: 8px; }
.chat-input {
    flex: 1; padding: 8px 13px; border: 1px solid rgba(255,255,255,0.16);
    border-radius: 18px; font-size: 13px; outline: none;
    background: rgba(255,255,255,0.08); color: #e8f0fe;
    transition: border-color 0.15s;
}
.chat-input::placeholder { color: rgba(185,198,221,0.55); }
.chat-input:focus { border-color: rgba(26,115,232,0.85); background: rgba(255,255,255,0.1); }
.chat-send {
    width: 36px; height: 36px; border-radius: 50%; border: none; cursor: pointer;
    background: linear-gradient(135deg, #1a73e8, #7c3aed); color: #fff;
    font-size: 14px; transition: transform 0.15s, opacity 0.15s; flex-shrink: 0;
}
.chat-send:hover { transform: scale(1.06); }
.chat-send:disabled { opacity: 0.45; cursor: not-allowed; }

/* ── 养成 ── */
.grow-body { overflow-y: auto; }
.grow-section { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.09); border-radius: 10px; padding: 12px; }
.grow-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 9px; }
.grow-title { font-size: 12px; font-weight: 700; color: #fff; letter-spacing: 0.5px; }
.grow-badge {
    background: linear-gradient(135deg, #1a73e8, #7c3aed); color: #fff;
    padding: 2px 10px; border-radius: 10px; font-size: 12px; font-weight: 700;
}
.exp-bar { height: 8px; border-radius: 4px; background: rgba(255,255,255,0.1); overflow: hidden; }
.exp-fill {
    height: 100%; border-radius: 4px;
    background: linear-gradient(90deg, #1a73e8, #7ee787);
    transition: width 0.5s ease;
}
.exp-meta { display: flex; justify-content: space-between; font-size: 11px; color: #9db4d9; margin-top: 6px; }

.trait-table { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.09); border-radius: 10px; padding: 8px 12px; }
.trait-row { display: flex; align-items: center; gap: 8px; padding: 6px 0; }
.trait-name { width: 42px; font-size: 12px; color: #b9c6dd; flex-shrink: 0; }
.trait-track { flex: 1; height: 7px; border-radius: 4px; background: rgba(255,255,255,0.1); overflow: hidden; }
.trait-fill { height: 100%; border-radius: 4px; background: linear-gradient(90deg, #1a73e8, #7c3aed); transition: width 0.25s ease; }
.trait-val { width: 26px; text-align: right; font-size: 12px; color: #fff; font-weight: 600; }
.trait-btns { display: flex; gap: 4px; }
.trait-btn {
    width: 22px; height: 22px; border-radius: 50%; border: 1px solid rgba(255,255,255,0.2);
    background: rgba(255,255,255,0.07); color: #e8f0fe; cursor: pointer; font-size: 13px; line-height: 1;
    transition: all 0.15s;
}
.trait-btn:hover { background: rgba(26,115,232,0.5); border-color: rgba(26,115,232,0.8); }

.grow-actions { display: flex; align-items: center; gap: 10px; }
.grow-save {
    padding: 7px 18px; border: none; border-radius: 8px; cursor: pointer;
    background: linear-gradient(135deg, #1a73e8, #7c3aed); color: #fff;
    font-size: 12px; font-weight: 600; transition: transform 0.15s, opacity 0.15s;
}
.grow-save:hover { transform: translateY(-1px); }
.grow-save.full { width: 100%; }
.grow-tip { font-size: 11px; color: rgba(185,198,221,0.6); }

.grow-log { display: flex; flex-direction: column; gap: 4px; max-height: 180px; overflow-y: auto; }
.grow-log::-webkit-scrollbar { width: 5px; }
.grow-log::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.15); border-radius: 3px; }
.log-line {
    display: flex; align-items: baseline; gap: 6px;
    font-size: 12px; color: #b9c6dd; padding: 2px 0;
    border-bottom: 1px dashed rgba(255,255,255,0.05);
}
.log-line:last-child { border-bottom: none; }
.log-time { color: rgba(157,180,217,0.55); font-size: 11px; flex-shrink: 0; font-family: Consolas, Monaco, monospace; }
.log-emoji { flex-shrink: 0; font-size: 11px; }
.log-text { word-break: break-all; }
.log-line.learn .log-text { color: #7ee787; }
.log-line.milestone .log-text { color: #d2a8ff; font-weight: 600; }

/* ── 记忆设置 ── */
.mem-body { overflow-y: auto; }
.mem-section { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.09); border-radius: 10px; padding: 12px; }
.auto-badge {
    background: rgba(64,160,255,0.18); color: #8ab4ff; font-size: 10px;
    padding: 2px 8px; border-radius: 8px; font-weight: 600;
}
.form-row { display: flex; gap: 8px; margin-bottom: 9px; }
.form-input {
    flex: 1; padding: 7px 10px; border: 1px solid rgba(255,255,255,0.16);
    border-radius: 8px; background: rgba(255,255,255,0.08); color: #e8f0fe;
    font-size: 13px; outline: none;
}
.form-input:focus, .form-textarea:focus { border-color: rgba(26,115,232,0.85); }
.form-group { margin-bottom: 10px; }
.form-group label { display: block; font-size: 12px; color: #b9c6dd; margin-bottom: 6px; }
.form-group .hint { font-size: 10px; color: rgba(157,180,217,0.55); }
.chips { display: flex; gap: 6px; }
.chip {
    flex: 1; padding: 6px 0; border: 1px solid rgba(255,255,255,0.16);
    border-radius: 8px; background: rgba(255,255,255,0.06); color: #b9c6dd;
    font-size: 12px; cursor: pointer; transition: all 0.15s; text-align: center;
}
.chip:hover { border-color: rgba(26,115,232,0.7); color: #fff; }
.chip.active { background: linear-gradient(135deg, #1a73e8, #7c3aed); color: #fff; font-weight: 600; border-color: transparent; }
.form-textarea {
    width: 100%; padding: 7px 10px; border: 1px solid rgba(255,255,255,0.16);
    border-radius: 8px; background: rgba(255,255,255,0.08); color: #e8f0fe;
    font-size: 12px; outline: none; resize: vertical; font-family: inherit; line-height: 1.6;
}
.stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; text-align: center; }
.stat-item { background: rgba(255,255,255,0.06); border-radius: 8px; padding: 8px 4px; }
.stat-item b { display: block; font-size: 15px; color: #fff; }
.stat-item span { display: block; font-size: 10px; color: rgba(157,180,217,0.7); margin-top: 2px; }
.mem-tip { font-size: 11px; color: rgba(157,180,217,0.6); margin-top: 8px; }

/* ── 状态栏 ── */
.ai-status {
    display: flex; align-items: center; gap: 7px;
    padding: 5px 13px; border-top: 1px solid rgba(255,255,255,0.1);
    background: rgba(9,17,32,0.6); font-size: 11px; color: #8ea3c4;
    flex-shrink: 0;
}
.status-dot { width: 7px; height: 7px; border-radius: 50%; }
.status-dot.ok { background: #4ade80; box-shadow: 0 0 6px #4ade80; animation: dotBlink 2.2s ease-in-out infinite; }
.status-dot.bad { background: #f87171; box-shadow: 0 0 6px #f87171; }
@keyframes dotBlink { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
.flex-sep { flex: 1; }

/* 出现/关闭动画 */
.pop-enter-active, .pop-leave-active { transition: all 0.22s ease; }
.pop-enter-from, .pop-leave-to { opacity: 0; transform: translateY(14px) scale(0.97); }

/* ── 移动端：FAB 钉在标签栏上方，窗口变全宽底部弹层 ── */
@media (max-width: 768px) {
    .ai-float.is-mobile {
        left: auto !important; top: auto !important;
        right: 14px !important;
        /* +76px 让位给上方的级联日志 FAB */
        bottom: calc(var(--tabbar-height) + var(--tabbar-safe-bottom) + 76px) !important;
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
    /* 换行、tab 独占一行已提升为基础样式，移动端只多一个居中 */
    .ai-float.is-mobile .hdr-tabs { justify-content: center; }
}
</style>