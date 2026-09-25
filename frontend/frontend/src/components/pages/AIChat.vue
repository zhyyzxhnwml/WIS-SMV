<template>
  <div class="page">
    <PageHeader :icon="aiConfig.avatar" title="智能对话"
                :subtitle="aiConfig.name + ' · 对话由后端 Python 处理，当前固定返回「测试中」'" />

    <div class="ai-layout">
      <div class="card ai-chat-card">
        <!-- 会话头 -->
        <div class="ai-chat-head">
          <div class="ai-head-avatar">
            <img v-if="avatarIsImage" :src="aiConfig.avatar" alt="avatar" />
            <AppIcon v-else :name="aiConfig.avatar" :size="20" />
          </div>
          <div class="ai-head-info">
            <strong>{{ aiConfig.name }}</strong>
            <small><span class="ai-dot"></span> 测试模式 · 由后端 Python 处理</small>
          </div>
          <span class="ai-head-tag">智能体</span>
        </div>

        <!-- 消息区 -->
        <div class="ai-chat-body" ref="chatBody">
          <div v-for="(m, i) in messages" :key="i" :class="['ai-msg', m.role]">
            <span v-if="m.role === 'ai'" class="ai-avatar sm">
              <img v-if="avatarIsImage" :src="aiConfig.avatar" alt="avatar" />
              <AppIcon v-else :name="aiConfig.avatar" :size="15" />
            </span>
            <div class="ai-bubble">
              <div>{{ m.content }}</div>
              <!-- 结构化证据卡片（违规证据链技能）：判定 + 轨迹图 + 事件表格 -->
              <EvidenceCard v-if="m.structured" :data="m.structured" />
              <!-- 渲染型工具产出（画图/写表格/写文档）：按 kind 内联渲染 -->
              <RenderBlocks v-if="m.blocks && m.blocks.length" :blocks="m.blocks" />
              <div v-if="m.tools && m.tools.length" class="tool-calls">
                <div v-for="(t, ti) in m.tools" :key="ti" class="tool-call">
                  <span class="tool-icon"><AppIcon :name="t.icon || 'tool'" :size="16" /></span>
                  <div class="tool-meta">
                    <span class="tool-name">{{ t.label || t.name }}</span>
                    <span class="tool-desc">{{ t.description }}</span>
                  </div>
                  <span class="tool-status" :class="t.status === 'locked' ? 'locked' : 'ok'">
                    <AppIcon :name="t.status === 'locked' ? 'lock' : 'check'" inline /> {{ t.status === 'locked' ? '未解锁' : '已调用' }}
                  </span>
                </div>
              </div>
            </div>
          </div>
          <div v-if="sending" class="ai-msg ai">
            <span class="ai-avatar sm">
              <img v-if="avatarIsImage" :src="aiConfig.avatar" alt="avatar" />
              <AppIcon v-else :name="aiConfig.avatar" :size="15" />
            </span>
            <div class="ai-bubble typing"><span></span><span></span><span></span></div>
          </div>
        </div>

        <!-- 输入区 -->
        <div class="ai-chat-input">
          <div class="chat-quick-actions">
            <button
              v-for="q in quickQuestions"
              :key="q"
              class="quick-btn"
              @click="sendQuick(q)"
            >{{ q }}</button>
          </div>
          <div class="chat-input-row">
            <input
              type="text"
              class="chat-input"
              v-model="text"
              placeholder="输入您的问题..."
              @keydown.enter="send"
            >
            <button class="btn btn-primary chat-send" @click="send" :disabled="sending">发送</button>
          </div>
        </div>
      </div>

      <div class="ai-side">
        <div class="card">
          <div class="card-header"><h3>智能体设置</h3></div>
          <div class="card-body">
            <div class="form-group">
              <label>智能体名称</label>
              <input type="text" class="form-control" v-model="nameInput" placeholder="输入新名称">
            </div>
            <div class="form-group">
              <label>智能体头像</label>
              <input type="text" class="form-control" v-model="avatarInput" placeholder="头像图片地址或表情，如 /avatar.jpg 或 🤖">
            </div>
            <div class="form-actions">
              <button class="btn btn-primary" @click="saveProfile"><AppIcon name="save" inline /> 保存</button>
              <button class="btn btn-secondary" @click="doGenerateLogo"><AppIcon name="palette" inline /> AI 生成 Logo</button>
            </div>
            <div class="logo-preview-box">
              <img v-if="aiConfig.logoUrl" :src="aiConfig.logoUrl" class="logo-preview" alt="AI Logo">
              <span v-else class="logo-empty"><AppIcon name="anchor" inline /> 暂无 Logo</span>
            </div>
            <p class="form-tip">AI Logo 由后端 Python 生成，接口 POST /ai/logo 已写入 API 文档。</p>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, nextTick } from 'vue'
import PageHeader from '../../components/PageHeader.vue'
import { aiConfig, updateAiProfile, generateLogo, isImageUrl } from '../../store/ai.js'
import { sendChat } from '../../api/ai.js'
import AppIcon from '../../components/AppIcon.vue'
import EvidenceCard from '../../components/EvidenceCard.vue'
import RenderBlocks from '../../components/RenderBlocks.vue'

const avatarIsImage = computed(() => isImageUrl(aiConfig.avatar))

const chatBody = ref(null)
const text = ref('')
const nameInput = ref('')
const avatarInput = ref('')
const messages = ref([])
const sending = ref(false)

const quickQuestions = [
  '413570530 的违规证据链',
  '查询闽海之星最近航行轨迹',
  '福州港主航道限速规定',
  '今日辖区违规事件统计',
  '船舶在港区锚泊有什么规定？'
]

onMounted(() => {
  nameInput.value = aiConfig.name
  avatarInput.value = aiConfig.avatar
  messages.value = [{
    role: 'ai',
    content: `您好！我是${aiConfig.name}，港口船舶违规行为智能识别助手。当前对话由后端 Python 处理，处于「测试中」阶段。`
  }]
})

async function sendQuick(q) {
  text.value = q
  await send()
}

async function send() {
  const t = text.value.trim()
  if (!t || sending.value) return
  text.value = ''
  messages.value.push({ role: 'user', content: t })
  scrollToBottom()
  sending.value = true
  try {
    const r = await sendChat({ message: t })
    messages.value.push({ role: 'ai', content: r.reply || '测试中', tools: r.tool_calls || [], structured: r.structured || null, blocks: r.blocks || null })
  } catch (e) {
    messages.value.push({ role: 'ai', content: '请求失败，请检查后端服务是否启动' })
  } finally {
    sending.value = false
    scrollToBottom()
  }
}

async function saveProfile() {
  try {
    await updateAiProfile({
      name: nameInput.value.trim() || aiConfig.name,
      avatar: avatarInput.value.trim() || aiConfig.avatar
    })
    alert(`智能体已更名为「${aiConfig.name}」`)
  } catch (e) {
    alert('保存失败')
  }
}

async function doGenerateLogo() {
  try {
    await generateLogo()
    alert('Logo 生成成功，导航栏 Logo 已更新')
  } catch (e) {
    alert('生成失败，请检查后端服务')
  }
}

async function scrollToBottom() {
  await nextTick()
  if (chatBody.value) {
    chatBody.value.scrollTop = chatBody.value.scrollHeight
  }
}
</script>

<style scoped>
.ai-layout { display: grid; grid-template-columns: 1fr 320px; gap: 20px; align-items: start; }
.ai-chat-card {
    display: flex; flex-direction: column;
    height: calc(100vh - 230px); min-height: 480px; margin-bottom: 0;
}

/* ── 会话头 ── */
.ai-chat-head {
    display: flex; align-items: center; gap: 12px;
    padding: 12px 20px; border-bottom: 1px solid var(--border);
    background: linear-gradient(90deg, rgba(227, 242, 253, 0.6), rgba(214, 245, 239, 0.35) 60%, transparent);
}
.ai-head-avatar {
    width: 40px; height: 40px; border-radius: 50%;
    background: var(--gradient-primary);
    box-shadow: 0 3px 10px rgba(12, 140, 233, 0.35);
    display: flex; align-items: center; justify-content: center;
    font-size: 20px; color: #fff; flex-shrink: 0;
    overflow: hidden;
}
.ai-head-avatar img { width: 100%; height: 100%; object-fit: cover; }
.ai-head-info { flex: 1; min-width: 0; }
.ai-head-info strong { font-size: 15px; display: block; color: var(--text); }
.ai-head-info small { font-size: 12px; color: var(--text-secondary); display: flex; align-items: center; gap: 6px; }
.ai-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--warning); box-shadow: 0 0 6px rgba(245, 165, 36, 0.6); }
.ai-head-tag {
    font-size: 12px; font-weight: 600; color: var(--primary);
    background: var(--primary-light); padding: 3px 10px; border-radius: 999px; flex-shrink: 0;
}

/* ── 消息区 ── */
.ai-chat-body {
    flex: 1; overflow-y: auto; padding: 20px 24px;
    display: flex; flex-direction: column; gap: 14px;
}
.ai-msg { display: flex; gap: 10px; max-width: 82%; animation: msgIn 0.3s ease both; }
@keyframes msgIn {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
}
.ai-msg.user { align-self: flex-end; flex-direction: row-reverse; }
.ai-avatar {
    width: 34px; height: 34px; border-radius: 50%; flex-shrink: 0;
    background: linear-gradient(135deg, rgba(12,140,233,0.15), rgba(24,184,166,0.15));
    border: 2px solid rgba(12, 140, 233, 0.25);
    display: flex; align-items: center; justify-content: center; font-size: 18px;
    color: #fff; box-shadow: 0 2px 8px rgba(12, 140, 233, 0.18);
    overflow: hidden;
}
.ai-avatar img { width: 100%; height: 100%; object-fit: cover; }
.ai-avatar.sm { width: 30px; height: 30px; font-size: 15px; }
.ai-bubble {
    background: var(--card-bg); border: 1px solid rgba(169, 205, 226, 0.5);
    padding: 11px 15px; border-radius: 14px 14px 14px 4px;
    font-size: 14px; line-height: 1.7; color: var(--text);
    box-shadow: 0 2px 10px rgba(10, 42, 66, 0.06);
}
.ai-msg.user .ai-bubble {
    background: linear-gradient(135deg, #0c8ce9, #0a6fc4);
    color: #fff; border: none;
    border-radius: 14px 14px 4px 14px;
    box-shadow: 0 4px 14px rgba(12, 140, 233, 0.28);
}

/* 工具调用卡片 */
.tool-calls { display: flex; flex-direction: column; gap: 6px; margin-top: 10px; border-top: 1px dashed rgba(12, 140, 233, 0.25); padding-top: 9px; }
.tool-call {
    display: flex; align-items: flex-start; gap: 8px;
    background: rgba(12, 140, 233, 0.08); border: 1px solid rgba(12, 140, 233, 0.25);
    border-radius: 8px; padding: 7px 10px;
}
.tool-icon { font-size: 15px; line-height: 1.4; flex-shrink: 0; }
.tool-meta { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.tool-name { font-size: 13px; font-weight: 700; color: var(--text); }
.tool-desc { font-size: 12px; color: var(--text-secondary); line-height: 1.5; }
.tool-status { font-size: 11px; font-weight: 600; flex-shrink: 0; }
.tool-status.ok { color: #0b8a69; }
.tool-status.locked { color: #b45309; }

/* 打字指示器 */
.ai-bubble.typing { display: flex; gap: 5px; align-items: center; padding: 15px 16px; }
.ai-bubble.typing span {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--primary); opacity: 0.4;
    animation: typingDot 1.2s ease-in-out infinite;
}
.ai-bubble.typing span:nth-child(2) { animation-delay: 0.2s; }
.ai-bubble.typing span:nth-child(3) { animation-delay: 0.4s; }
@keyframes typingDot {
    0%, 60%, 100% { transform: translateY(0); opacity: 0.35; }
    30% { transform: translateY(-4px); opacity: 1; }
}

/* ── 输入区 ── */
.ai-chat-input {
    padding: 14px 20px; border-top: 1px solid var(--border);
    background: linear-gradient(180deg, #fff, rgba(244, 251, 255, 0.8));
    border-radius: 0 0 var(--radius) var(--radius);
}
.chat-quick-actions { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.quick-btn {
    padding: 6px 14px; border: 1px solid var(--border); border-radius: 16px;
    background: #fff; font-size: 12px; cursor: pointer; transition: all var(--transition);
    white-space: nowrap; color: var(--text-secondary);
}
.quick-btn:hover { background: var(--primary-light); border-color: var(--primary); color: var(--primary); }
.chat-input-row { display: flex; gap: 12px; }
.chat-input {
    flex: 1; padding: 11px 18px; border: 1px solid var(--border);
    border-radius: 24px; font-size: 14px; outline: none; transition: all var(--transition);
    background: #fff;
}
.chat-input:focus { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(12, 140, 233, 0.12); }
.chat-send { border-radius: 24px !important; padding: 10px 28px !important; }
.ai-chat-body::-webkit-scrollbar { width: 6px; }
.ai-chat-body::-webkit-scrollbar-thumb { background: #c1c7cd; border-radius: 3px; }
.logo-preview-box {
    margin-top: 16px; padding: 12px; border: 1px dashed var(--border);
    border-radius: var(--radius-sm); text-align: center;
}
.logo-preview { max-width: 100%; border-radius: var(--radius-sm); }
.logo-empty { font-size: 13px; color: var(--text-secondary); }
.form-tip { font-size: 12px; color: var(--text-secondary); margin-top: 12px; line-height: 1.7; }
@media (max-width: 1200px) {
    .ai-layout { grid-template-columns: 1fr; }
}
@media (max-width: 768px) {
    .ai-chat-card { height: calc(100dvh - 200px); min-height: 0; }
}
</style>
