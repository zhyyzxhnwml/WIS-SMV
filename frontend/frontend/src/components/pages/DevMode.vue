<template>
  <div class="page">
    <PageHeader icon="flask" title="开发者模式" subtitle="模型参数配置 + 自定义技能（skill.md），改动即时生效" />

    <!-- 页签切换 -->
    <div class="dev-tabs">
      <button :class="['dev-tab', tab === 'cfg' && 'active']" @click="tab = 'cfg'"><AppIcon name="settings" inline /> 模型配置</button>
      <button :class="['dev-tab', tab === 'skills' && 'active']" @click="tab = 'skills'"><AppIcon name="puzzle" inline /> 自定义技能</button>
    </div>

    <template v-if="tab === 'cfg'">
    <!-- Ollama 状态卡片 -->
    <div class="card status-card">
      <div class="card-head">
        <h3>Ollama 状态</h3>
        <div class="head-actions">
          <span class="badge" :class="online ? 'badge-online' : 'badge-offline'">
            {{ online ? '● 在线' : '○ 离线' }}
          </span>
          <button class="btn-mini btn-ghost" :disabled="loadingModels" @click="loadModels">
            {{ loadingModels ? '刷新中…' : '刷新模型' }}
          </button>
        </div>
      </div>
      <p v-if="onlineError" class="dim status-error">{{ onlineError }}</p>
      <p v-if="!online && !onlineError" class="dim">无法连接 Ollama（{{ cfg.base_url }}），请确认服务已启动。</p>
      <div v-if="online" class="model-list">
        <div v-for="m in models" :key="m.name" class="model-chip" @click="pickModel(m.name)">
          <span class="model-name">{{ m.name }}</span>
          <span class="model-size">{{ fmtSize(m.size) }}</span>
        </div>
        <p v-if="models.length === 0" class="dim">Ollama 在线，但未安装任何模型。</p>
      </div>
      <p v-if="online" class="footnote"><AppIcon name="lightbulb" inline /> 点击某个模型即可填入上方「模型名称」</p>
    </div>

    <!-- 模型与参数卡片 -->
    <div class="card cfg-card">
      <div class="card-head">
        <h3>模型与参数</h3>
      </div>

      <div class="cfg-grid">
        <label class="field-group">
          <span>Ollama 地址 <b class="req">*</b></span>
          <input v-model.trim="cfg.base_url" class="field" placeholder="http://localhost:11434" />
        </label>

        <label class="field-group">
          <span>模型名称 <b class="req">*</b></span>
          <input v-model.trim="cfg.model" class="field" list="model-list" placeholder="qwen2.5:7b-instruct-q4_K_M" />
          <datalist id="model-list">
            <option v-for="m in models" :key="m.name" :value="m.name" />
          </datalist>
        </label>

        <label class="field-group">
          <span>temperature（0~2，步进 0.1）</span>
          <input v-model.number="cfg.temperature" class="field" type="number" min="0" max="2" step="0.1" />
        </label>

        <label class="field-group">
          <span>num_predict（最大生成 token）</span>
          <input v-model.number="cfg.num_predict" class="field" type="number" min="1" step="1" />
        </label>

        <label class="field-group">
          <span>top_p（留空=不设置）</span>
          <input v-model.number="cfg.top_p" class="field" type="number" min="0" max="1" step="0.05" />
        </label>

        <label class="field-group">
          <span>top_k（留空=不设置）</span>
          <input v-model.number="cfg.top_k" class="field" type="number" min="1" step="1" />
        </label>

        <label class="field-group field-full">
          <span>系统提示词（留空=使用内置人格）</span>
          <textarea v-model="cfg.system_prompt" class="field textarea" rows="3"
            placeholder="可在此追加全局指令，会前置到小慧等内置人格之前" />
        </label>
      </div>

      <div class="btn-row">
        <button class="btn-primary" :disabled="saving" @click="save">
          <AppIcon name="save" inline /> {{ saving ? '保存中…' : '保存配置' }}
        </button>
        <button class="btn-mini btn-ok" :disabled="testing" @click="test">
          <AppIcon name="plug" inline /> {{ testing ? '测试中…' : '测试连接' }}
        </button>
        <button class="btn-mini btn-danger" :disabled="resetting" @click="reset">
          <AppIcon name="refresh-cw" inline /> {{ resetting ? '恢复中…' : '恢复默认' }}
        </button>
      </div>

      <!-- 测试结果 -->
      <div v-if="testResult" class="test-result" :class="testOk ? 'test-ok' : 'test-err'">
        <template v-if="testOk">
          <p><b><AppIcon name="check-circle" inline /> 连接正常</b>，耗时 {{ testResult.elapsed_ms }} ms</p>
          <p class="test-reply">{{ testResult.reply }}</p>
        </template>
        <p v-else class="test-reply"><b><AppIcon name="x-circle" inline /> 测试失败</b>：{{ testResult.error }}</p>
      </div>

      <transition name="fade">
        <div v-if="msg.text" class="msg" :class="'msg-' + msg.type">{{ msg.text }}</div>
      </transition>

      <p class="footnote"><AppIcon name="info" inline /> 仅影响对话/判定模型；embedding 向量模型固定（向量库维度原因），不可修改。</p>
    </div>
    </template>

    <!-- 自定义技能（用户提交 skill.md，智能体直接可用） -->
    <DevSkills v-if="tab === 'skills'" />
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import PageHeader from '../PageHeader.vue'
import DevSkills from './DevSkills.vue'
import AppIcon from '../AppIcon.vue'
import { fetchLlmConfig, saveLlmConfig, testLlmConfig, fetchModels, resetLlmConfig } from '../../api/dev.js'

const tab = ref('cfg')

const cfg = reactive({
  base_url: '',
  model: '',
  temperature: 0.7,
  num_predict: 600,
  top_p: null,
  top_k: null,
  system_prompt: ''
})

const models = ref([])
const online = ref(false)
const onlineError = ref('')
const loadingModels = ref(false)
const saving = ref(false)
const testing = ref(false)
const resetting = ref(false)
const msg = ref({ type: '', text: '' })
const testResult = ref(null)
const testOk = ref(true)

let msgTimer = null
function flash(type, text) {
  msg.value = { type, text }
  if (msgTimer) clearTimeout(msgTimer)
  msgTimer = setTimeout(() => (msg.value = { type: '', text: '' }), 3500)
}

function fmtSize(bytes) {
  const v = Number(bytes) || 0
  if (v >= 1e9) return (v / 1e9).toFixed(1) + ' GB'
  if (v >= 1e6) return (v / 1e6).toFixed(1) + ' MB'
  return v + ' B'
}

function applyConfig(c) {
  cfg.base_url = c.base_url || ''
  cfg.model = c.model || ''
  cfg.temperature = c.temperature ?? 0.7
  cfg.num_predict = c.num_predict ?? 600
  cfg.top_p = c.top_p ?? null
  cfg.top_k = c.top_k ?? null
  cfg.system_prompt = c.system_prompt || ''
}

function buildPayload() {
  return {
    base_url: cfg.base_url,
    model: cfg.model,
    temperature: cfg.temperature,
    num_predict: cfg.num_predict,
    top_p: cfg.top_p === '' || cfg.top_p === null ? null : cfg.top_p,
    top_k: cfg.top_k === '' || cfg.top_k === null ? null : cfg.top_k,
    system_prompt: cfg.system_prompt
  }
}

async function load() {
  try {
    const c = await fetchLlmConfig()
    applyConfig(c)
  } catch (e) {
    flash('error', e.message || '加载配置失败')
  }
}

async function loadModels() {
  loadingModels.value = true
  try {
    const data = await fetchModels()
    online.value = !!data.online
    models.value = data.models || []
    onlineError.value = data.error || ''
  } catch (e) {
    online.value = false
    onlineError.value = e.message || '获取模型列表失败'
  } finally {
    loadingModels.value = false
  }
}

function pickModel(name) {
  cfg.model = name
}

async function save() {
  if (!cfg.base_url || !cfg.model) {
    return flash('error', '请填写 Ollama 地址与模型名称')
  }
  if (!confirm('保存后将全局生效，影响所有成员的 AI 对话与级联判定。确定继续？')) return
  saving.value = true
  try {
    const c = await saveLlmConfig(buildPayload())
    applyConfig(c)
    flash('ok', '配置已保存，全部 AI 链路已生效')
  } catch (e) {
    flash('error', e.message || '保存失败')
  } finally {
    saving.value = false
  }
}

async function test() {
  if (!cfg.base_url || !cfg.model) {
    return flash('error', '请先填写 Ollama 地址与模型名称')
  }
  testing.value = true
  testResult.value = null
  try {
    const r = await testLlmConfig()
    testOk.value = true
    testResult.value = r
  } catch (e) {
    testOk.value = false
    testResult.value = { error: e.message || '测试失败' }
  } finally {
    testing.value = false
  }
}

async function reset() {
  if (!confirm('确定恢复默认模型与参数配置？')) return
  resetting.value = true
  try {
    const c = await resetLlmConfig()
    applyConfig(c)
    testResult.value = null
    flash('ok', '已恢复默认配置')
  } catch (e) {
    flash('error', e.message || '恢复失败')
  } finally {
    resetting.value = false
  }
}

onMounted(() => {
  load()
  loadModels()
})
</script>

<style scoped>
/* 页签切换 */
.dev-tabs {
  display: flex; gap: 8px; margin-bottom: 20px;
  padding: 5px; border-radius: 12px;
  background: rgba(10, 42, 66, 0.06); border: 1px solid rgba(169, 205, 226, 0.25);
  width: fit-content;
}
.dev-tab {
  padding: 9px 22px; border: none; border-radius: 9px; cursor: pointer;
  font-size: 14px; color: var(--text-secondary);
  background: transparent; transition: all 0.15s;
}
.dev-tab:hover { color: var(--text); }
.dev-tab.active {
  background: var(--gradient-primary); color: #fff; font-weight: 600;
  box-shadow: 0 3px 10px rgba(12, 140, 233, 0.3);
}

.status-card, .cfg-card {
  padding: 24px 26px; margin-bottom: 24px;
}
.card-head {
  display: flex; justify-content: space-between; align-items: center;
  gap: 14px; flex-wrap: wrap; margin-bottom: 18px;
  padding-bottom: 14px; border-bottom: 1px solid rgba(169, 205, 226, 0.18);
}
.card-head h3 { margin: 0; font-size: 16px; }
.head-actions { display: flex; align-items: center; gap: 12px; }
.badge { font-size: 12px; font-weight: 600; padding: 4px 14px; border-radius: 999px; }
.badge-online { background: rgba(24, 184, 166, 0.15); color: #2ec4a0; border: 1px solid rgba(24, 184, 166, 0.4); }
.badge-offline { background: rgba(244, 63, 94, 0.12); color: #fb7185; border: 1px solid rgba(244, 63, 94, 0.4); }
.status-error { color: #fb7185; margin: 0 0 12px; }

.model-list {
  display: flex; flex-wrap: nowrap; gap: 12px; overflow-x: auto;
  padding-bottom: 6px; -webkit-overflow-scrolling: touch;
}
.model-chip {
  flex-shrink: 0; display: flex; flex-direction: column; gap: 4px;
  padding: 10px 16px; border: 1px solid rgba(12, 140, 233, 0.35); border-radius: 12px;
  background: rgba(12, 140, 233, 0.08); cursor: pointer; transition: all 0.15s;
}
.model-chip:hover { background: rgba(12, 140, 233, 0.2); }
.model-name { font-size: 13px; font-weight: 600; white-space: nowrap; }
.model-size { font-size: 11px; color: var(--text-secondary); white-space: nowrap; }

.cfg-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px 26px; }
.field-group { display: flex; flex-direction: column; gap: 8px; }
.field-group > span { font-size: 13px; color: var(--text-secondary); }
.field-group .req { color: #fb7185; }
.field-full { grid-column: 1 / -1; }
.field {
  width: 100%; box-sizing: border-box; padding: 11px 14px; border-radius: 9px;
  border: 1px solid var(--border); background: #fff; color: var(--text); font-size: 14px;
  outline: none; transition: border 0.15s;
}
.field:focus { border-color: var(--primary); }
.textarea { resize: vertical; font-family: inherit; }

.btn-row { display: flex; gap: 12px; margin-top: 26px; flex-wrap: wrap; }
.btn-primary {
  background: var(--gradient-primary); color: #fff; border: none; border-radius: 9px;
  padding: 11px 28px; font-size: 14px; font-weight: 600; cursor: pointer;
  transition: all var(--transition);
}
.btn-primary:hover:not(:disabled) { transform: translateY(-1px); opacity: 0.92; }
.btn-primary:disabled { opacity: 0.6; cursor: not-allowed; }
.btn-mini { padding: 10px 20px; border: none; border-radius: 999px; font-size: 13px; cursor: pointer; transition: all 0.15s; }
.btn-ghost { background: rgba(12, 140, 233, 0.12); color: #5caef0; border: 1px solid rgba(12, 140, 233, 0.35); }
.btn-ghost:hover:not(:disabled) { background: rgba(12, 140, 233, 0.25); }
.btn-ok { background: rgba(24, 184, 166, 0.15); color: #2ec4a0; border: 1px solid rgba(24, 184, 166, 0.4); }
.btn-ok:hover:not(:disabled) { background: rgba(24, 184, 166, 0.3); }
.btn-danger { background: rgba(244, 63, 94, 0.12); color: #fb7185; border: 1px solid rgba(244, 63, 94, 0.35); }
.btn-danger:hover:not(:disabled) { background: rgba(244, 63, 94, 0.25); }
.btn-mini:disabled { opacity: 0.6; cursor: not-allowed; }

.test-result {
  margin-top: 22px; padding: 14px 16px; border-radius: 10px; font-size: 13px;
  word-break: break-word;
}
.test-ok { background: rgba(24, 184, 166, 0.1); border: 1px solid rgba(24, 184, 166, 0.35); color: #7ff0e2; }
.test-err { background: rgba(244, 63, 94, 0.1); border: 1px solid rgba(244, 63, 94, 0.4); color: #fb7185; }
.test-result p { margin: 0; }
.test-reply { margin-top: 8px !important; opacity: 0.92; white-space: pre-wrap; }

.msg { margin-top: 16px; padding: 10px 14px; border-radius: 8px; font-size: 13px; }
.msg-ok { background: rgba(24, 184, 166, 0.16); color: #7ff0e2; border: 1px solid rgba(24, 184, 166, 0.4); }
.msg-error { background: rgba(244, 63, 94, 0.14); color: #fb7185; border: 1px solid rgba(244, 63, 94, 0.4); }
.fade-enter-active, .fade-leave-active { transition: opacity 0.2s; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

.footnote { margin: 14px 0 0; font-size: 12px; color: var(--text-secondary); }

.dim { color: var(--text-secondary); }

@media (max-width: 768px) {
  .dev-tabs { width: 100%; }
  .dev-tab { flex: 1; padding: 9px 12px; }
  .status-card, .cfg-card { padding: 18px 16px; margin-bottom: 16px; }
  .cfg-grid { grid-template-columns: 1fr; gap: 16px; }
  .field-full { grid-column: auto; }
  .btn-row { flex-direction: column; }
  .btn-primary, .btn-mini { width: 100%; }
  .model-list { overflow-x: auto; }
}
</style>
