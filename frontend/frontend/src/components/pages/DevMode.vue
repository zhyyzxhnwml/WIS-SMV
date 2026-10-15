<template>
  <div class="page">
    <PageHeader icon="flask" title="开发者模式" subtitle="模型提供商 + 插件，改动即时生效" />

    <!-- 分区切换：提供商（模型与密钥）/ 插件（可执行判定插件 + 轻量 skill.md） -->
    <div class="dev-tabs">
      <button :class="['dev-tab', tab === 'providers' && 'active']" @click="tab = 'providers'"><AppIcon name="settings" inline /> 提供商</button>
      <button :class="['dev-tab', tab === 'plugins' && 'active']" @click="tab = 'plugins'"><AppIcon name="puzzle" inline /> 插件</button>
    </div>

    <template v-if="tab === 'providers'">
    <!-- 全局模型：整个系统只有一个模型 -->
    <div class="card cfg-card">
      <div class="card-head">
        <h3>全局模型</h3>
        <div class="head-actions">
          <span v-if="!canConfig" class="badge badge-readonly">只读（需要模型配置权限）</span>
          <button class="btn-mini btn-ghost" :disabled="resetting || !canConfig" @click="reset">
            <AppIcon name="refresh-cw" inline /> {{ resetting ? '恢复中…' : '恢复默认' }}
          </button>
        </div>
      </div>
      <p class="hint">
        整个系统用的是同一个模型：智能体对话、级联判定的 LLM 复核、AI 起草插件都走它。
        选好提供商与模型，保存即生效。
      </p>

      <div v-if="loading" class="dim">加载中…</div>

      <template v-else>
        <div class="global-row">
          <label class="field-group">
            <span>提供商</span>
            <select v-model="global.provider_id" class="field" :disabled="!canConfig">
              <option value="">自动（第一个可用的提供商）</option>
              <option v-for="p in usableProviders" :key="p.id" :value="p.id">{{ p.name }}</option>
            </select>
          </label>
          <label class="field-group">
            <span>模型</span>
            <input v-model.trim="global.model" class="field" list="global-models"
              :readonly="!canConfig" placeholder="留空 = 用该提供商的第一个模型" />
            <datalist id="global-models">
              <option v-for="m in modelsOf(global.provider_id)" :key="m" :value="m" />
            </datalist>
          </label>
        </div>

        <div class="global-meta">
          <span class="now">
            当前生效：<b>{{ effective.chat.provider_name }} · {{ effective.chat.model || '（未指定模型）' }}</b>
          </span>
          <span class="badge" :class="effective.chat.explicit ? 'tag-pinned' : 'tag-auto'">
            {{ effective.chat.explicit ? '已指定' : '自动回落' }}
          </span>
          <span v-if="globalNeedsKey" class="badge badge-warn">这个提供商还没有密钥</span>
        </div>

        <p v-if="diverged" class="warn-line">
          <AppIcon name="info" inline />
          检测到对话 / 判定 / 起草三处当前指向的模型不一致（历史配置）。保存后会统一成上面这一个。
        </p>
        <p v-if="!usableProviders.length" class="warn-line">
          <AppIcon name="info" inline />
          还没有可用的提供商。到下面「模型提供商」里展开一家（DeepSeek、智谱、通义… 或本地 Ollama），
          粘上 API 密钥保存即可。
        </p>

        <div class="btn-row">
          <button v-if="canConfig" class="btn-primary" :disabled="savingGlobal" @click="saveGlobal">
            <AppIcon name="save" inline /> {{ savingGlobal ? '保存中…' : '保存全局模型' }}
          </button>
          <button class="btn-mini btn-ok" :disabled="!global.provider_id || testingGlobal" @click="testGlobal">
            <AppIcon name="plug" inline /> {{ testingGlobal ? '测试中…' : '测试连接' }}
          </button>
          <button class="btn-mini btn-ghost" :disabled="!global.provider_id || probingGlobal" @click="probeGlobalModels">
            <AppIcon name="refresh-cw" inline /> {{ probingGlobal ? '拉取中…' : '拉取模型' }}
          </button>
        </div>

        <div v-if="globalTest" class="test-result" :class="globalTest.ok ? 'test-ok' : 'test-err'">
          <template v-if="globalTest.ok">
            <p><b><AppIcon name="check-circle" inline /> 连接正常</b>，耗时 {{ globalTest.elapsed_ms }} ms
              （模型 {{ globalTest.model }}）</p>
            <p class="test-reply">{{ globalTest.reply }}</p>
          </template>
          <p v-else class="test-reply"><b><AppIcon name="x-circle" inline /> 测试失败</b>：{{ globalTest.error }}</p>
        </div>
      </template>
    </div>

    <!-- 模型提供商：整个厂商目录平铺在这里，就地填密钥即可，没有「先添加再配置」两步 -->
    <div class="card cfg-card">
      <div class="card-head">
        <h3>模型提供商</h3>
        <div class="head-actions">
          <span class="dim">{{ enabledCount }} / {{ providers.length }} 已启用</span>
        </div>
      </div>
      <p class="hint">
        下面就是系统认识的<strong>全部</strong>厂商，不需要先「添加」：展开任意一家、粘上 API 密钥、保存即可。
        密钥加密后保存，接口永不回传原文；也可以用环境变量提供（例如 <code>DEEPSEEK_API_KEY</code>），
        环境变量优先。embedding 向量模型固定，不在这里暴露。
      </p>

      <div v-if="loading" class="dim">加载中…</div>

      <template v-else>
        <div v-for="g in providerGroups" :key="g.key" class="prov-group">
          <div class="prov-group-head">
            <span class="prov-group-label">{{ g.label }}</span>
            <span class="dim">{{ g.providers.length }} 家</span>
          </div>

          <div v-for="p in g.providers" :key="p.id" class="prov" :class="{ 'prov-off': !p.enabled }">
        <!-- 标题行：一眼看清「这是什么、能不能用、密钥有没有」 -->
        <div class="prov-head" @click="toggleExpand(p.id)">
          <div class="prov-title">
            <span class="prov-name">{{ p.name }}</span>
            <span class="prov-kind">{{ p.kind_label }}</span>
            <span v-if="!p.implemented" class="badge badge-warn">未实装</span>
            <span v-if="p.key_from_env" class="badge badge-env">密钥来自环境变量</span>
            <span v-else-if="p.key_error" class="badge badge-warn">密钥需重填</span>
            <span v-else-if="!p.has_key" class="badge badge-todo">未配置密钥</span>
          </div>
          <div class="prov-meta">
            <span class="prov-url">{{ p.effective_base_url || '地址待填' }}</span>
            <span class="prov-models">{{ p.models.length }} 个模型</span>
            <span class="badge" :class="p.enabled ? 'badge-online' : 'badge-offline'">
              {{ p.enabled ? '● 启用' : '○ 停用' }}
            </span>
            <AppIcon :name="expanded === p.id ? 'x' : 'edit'" inline class="prov-caret" />
          </div>
        </div>

        <!-- 展开：编辑表单。无权限时字段只读、且不显示密钥输入框 -->
        <div v-if="expanded === p.id" class="prov-body">
          <!-- 这家去哪拿密钥、有什么坑。平铺之后没有「选预设」那一步了，
               这两句话得挂在卡片自己身上，否则等于丢了。 -->
          <div v-if="p.doc_url || p.preset_note" class="preset-tip">
            <b>{{ p.name }}</b>
            <a v-if="p.doc_url" :href="p.doc_url" target="_blank" rel="noopener">
              去哪拿密钥 ↗
            </a>
            <p v-if="p.preset_note" class="footnote">{{ p.preset_note }}</p>
          </div>

          <div class="cfg-grid">
            <label class="field-group">
              <span>名称 <b class="req">*</b></span>
              <input v-model.trim="form.name" class="field" :readonly="!canConfig" placeholder="DeepSeek" />
            </label>
            <label class="field-group">
              <span>类型 <b class="req">*</b></span>
              <select v-model="form.kind" class="field" :disabled="!canConfig">
                <option v-for="k in kinds" :key="k.value" :value="k.value">{{ k.label }}</option>
              </select>
            </label>
            <label class="field-group field-full">
              <span>服务地址 <b class="req">*</b></span>
              <input v-model.trim="form.base_url" class="field" :readonly="!canConfig"
                placeholder="https://api.deepseek.com/v1 或 http://localhost:11434" />
              <span class="hint">
                按厂商文档原样填。地址没有路径时会自动补 /v1；自带版本段的（智谱 /api/paas/v4、
                千帆 /v2、火山方舟 /api/v3）原样使用，别手动加 /v1。
              </span>
            </label>
            <label class="field-group">
              <span>启用</span>
              <label class="switch-line">
                <input type="checkbox" v-model="form.enabled" :disabled="!canConfig" />
                <span>{{ form.enabled ? '启用（可被选为全局模型）' : '停用（不参与回落）' }}</span>
              </label>
            </label>

            <!-- 密钥输入框整体隐藏给无权限的人：连「有没有这个入口」都不暴露 -->
            <label v-if="canConfig" class="field-group field-full">
              <span>
                API 密钥
                <template v-if="form.kind === 'ollama'">（本地 Ollama 不需要密钥，留空即可）</template>
                <template v-else-if="p.has_key">（已保存 {{ p.key_hint }}；留空保持，填新值则覆盖）</template>
                <template v-else>（填上密钥保存即可启用这家）</template>
              </span>
              <input v-model.trim="form.api_key" class="field" type="password" autocomplete="new-password"
                :placeholder="p.has_key ? '不修改请留空' : '粘贴 API 密钥'" />
              <span v-if="p.key_from_env" class="hint">
                环境变量 {{ envHint(p) }} 已提供密钥，它的优先级高于这里保存的值。
              </span>
              <span v-if="p.key_error" class="hint hint-warn">{{ p.key_error }}</span>
            </label>

            <label class="field-group field-full">
              <span>模型列表（逗号分隔；「拉取模型」可自动填充）</span>
              <input v-model.trim="form.modelsText" class="field" :readonly="!canConfig"
                placeholder="deepseek-chat, deepseek-reasoner" />
            </label>

            <label class="field-group">
              <span>temperature（0~2，步进 0.1）</span>
              <input v-model.number="form.temperature" class="field" type="number" min="0" max="2" step="0.1"
                :readonly="!canConfig" />
            </label>
            <label class="field-group">
              <span>num_predict（最大生成 token）</span>
              <input v-model.number="form.num_predict" class="field" type="number" min="1" step="1"
                :readonly="!canConfig" />
            </label>
            <label class="field-group">
              <span>top_p（留空=不设置）</span>
              <input v-model.number="form.top_p" class="field" type="number" min="0" max="1" step="0.05"
                :readonly="!canConfig" />
            </label>
            <label class="field-group">
              <span>top_k（留空=不设置）</span>
              <input v-model.number="form.top_k" class="field" type="number" min="1" step="1"
                :readonly="!canConfig" />
            </label>
            <label class="field-group field-full">
              <span>系统提示词（留空=使用内置人格）</span>
              <textarea v-model="form.system_prompt" class="field textarea" rows="2" :readonly="!canConfig"
                placeholder="可在此追加全局指令，会前置到小慧等内置人格之前" />
            </label>
          </div>

          <div class="btn-row">
            <template v-if="canConfig">
              <button class="btn-primary" :disabled="saving" @click="saveProvider(p)">
                <AppIcon name="save" inline /> {{ saving ? '保存中…' : '保存' }}
              </button>
            </template>
            <button class="btn-mini btn-ok" :disabled="testing === p.id" @click="testProviderById(p)">
              <AppIcon name="plug" inline /> {{ testing === p.id ? '测试中…' : '测试连接' }}
            </button>
            <button class="btn-mini btn-ghost" :disabled="probing === p.id" @click="probeModels(p)">
              <AppIcon name="refresh-cw" inline /> {{ probing === p.id ? '拉取中…' : '拉取模型' }}
            </button>
            <button v-if="canConfig" class="btn-mini btn-danger" @click="removeProvider(p)">
              <AppIcon name="trash" inline /> 删除
            </button>
            <button class="btn-mini btn-ghost" @click="expanded = ''">收起</button>
          </div>

          <!-- 该提供商的测试结果 -->
          <div v-if="testResult && testResult.pid === p.id" class="test-result"
            :class="testResult.ok ? 'test-ok' : 'test-err'">
            <template v-if="testResult.ok">
              <p><b><AppIcon name="check-circle" inline /> 连接正常</b>，耗时 {{ testResult.elapsed_ms }} ms
                （模型 {{ testResult.model }}）</p>
              <p class="test-reply">{{ testResult.reply }}</p>
            </template>
            <p v-else class="test-reply"><b><AppIcon name="x-circle" inline /> 测试失败</b>：{{ testResult.error }}</p>
          </div>
            </div>
          </div>
        </div>
      </template>
    </div>

    <transition name="fade">
      <div v-if="msg.text" class="msg" :class="'msg-' + msg.type">{{ msg.text }}</div>
    </transition>
    </template>

    <!-- 插件分区。skill.md「自定义技能」那套已整体下线 —— 插件是唯一的一条路：
         它能真跑代码、产出落在判定链上，而 skill.md 只是往提示词里塞一段文字。 -->
    <DevPlugins v-if="tab === 'plugins'" :focus-plugin="focusPlugin" />
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import PageHeader from '../PageHeader.vue'
import DevPlugins from './DevPlugins.vue'
import AppIcon from '../AppIcon.vue'
import { can } from '../../store/user.js'
import { useRoute } from 'vue-router'
import {
  fetchProviders, updateProvider, deleteProvider,
  fetchProviderModels, testProvider, saveBindings, resetLlmConfig
} from '../../api/dev.js'

// 分区 key：providers / plugins（用户点的名就是「提供商」「插件」）。
// 允许 ?tab=plugins&plugin=<id> 直达 —— 悬浮对话里「查看草稿」按钮要跳到那个插件上，
// 光跳到插件列表等于让用户自己在几十条里找，那这个按钮就白做了。
const route = useRoute()
const tab = ref(route.query.tab === 'plugins' ? 'plugins' : 'providers')
const focusPlugin = computed(() => {
  const v = Number(route.query.plugin)
  return Number.isFinite(v) && v > 0 ? v : null
})

// 写操作要有 model.config。后端 @perm_required 才是真门禁，
// 这里只是别让人点了才发现 403。
const canConfig = computed(() => can('model.config'))

const providers = ref([])
const kinds = ref([])
const effectiveList = ref([])         // 三个用途各自「实际生效」的提供商与模型

// 「能用」= 启用了、且密钥那关过得去。没密钥的云端厂商虽然也在列表里（要平铺给人看），
// 但不能进全局模型下拉 —— 选了它就是一说话就报未配置密钥。
const usableProviders = computed(() => providers.value.filter(p => p.enabled && !p.needs_key))
const enabledCount = computed(() => providers.value.filter(p => p.enabled).length)

// 按目录分组平铺。组序以档案里第一次出现的顺序为准（后端顺序 = 目录顺序），
// 不在这里写死一份组名，免得目录加了新组前端看不见。
const providerGroups = computed(() => {
  const groups = []
  const index = new Map()
  for (const p of providers.value) {
    const key = p.group || 'other'
    if (!index.has(key)) {
      const g = { key, label: p.group_label || '其他', providers: [] }
      index.set(key, g)
      groups.push(g)
    }
    index.get(key).providers.push(p)
  }
  return groups
})

const loading = ref(false)
const saving = ref(false)
const savingGlobal = ref(false)
const resetting = ref(false)
const testing = ref('')     // 正在测试的 provider id
const probing = ref('')     // 正在拉取模型的 provider id
const testingGlobal = ref(false)
const probingGlobal = ref(false)
const expanded = ref('')
const msg = ref({ type: '', text: '' })
const testResult = ref(null)
const globalTest = ref(null)

// 全局模型草稿：一个提供商 + 一个模型，保存时写成三个用途的同一份绑定。
const global = reactive({ provider_id: '', model: '' })

// 展开表单的副本。密钥单独放，且**默认空串**：
// 空串 = 「不动原密钥」，只有用户真填了新值才带上去（后端三种语义见 api/dev.js 注释）。
const form = reactive({
  name: '', kind: 'ollama', base_url: '', enabled: true,
  api_key: '', modelsText: '', temperature: 0.7, num_predict: 600,
  top_p: null, top_k: null, system_prompt: ''
})

let msgTimer = null
function flash(type, text) {
  msg.value = { type, text }
  if (msgTimer) clearTimeout(msgTimer)
  msgTimer = setTimeout(() => (msg.value = { type: '', text: '' }), 3500)
}

function envHint(p) {
  const slug = String(p.id || '').toUpperCase().replace(/[^A-Z0-9]+/g, '_').replace(/^_|_$/g, '')
  return `${slug}_API_KEY`
}

function modelsOf(pid) {
  const p = providers.value.find(x => x.id === pid)
  return p ? p.models : []
}

const effective = computed(() => {
  const m = {}
  for (const b of effectiveList.value) m[b.purpose] = b
  return m
})

// 选了「自动」时看解析结果里那个真正的提供商，才能判断它有没有密钥
const globalProvider = computed(() => {
  const pid = global.provider_id || effective.value.chat?.provider_id
  return providers.value.find(p => p.id === pid) || null
})
const globalNeedsKey = computed(() => !!globalProvider.value?.needs_key)

// 历史配置可能让三个用途指向不同模型；保存后会统一，所以先提示一声。
const diverged = computed(() => {
  const c = effective.value.chat
  if (!c) return false
  return ['judge', 'draft'].some(k => {
    const b = effective.value[k]
    return b && (b.provider_id !== c.provider_id || (b.model || '') !== (c.model || ''))
  })
})

function toggleExpand(pid) {
  if (expanded.value === pid) {
    expanded.value = ''
    return
  }
  const p = providers.value.find(x => x.id === pid)
  if (!p) return
  expanded.value = pid
  applyToForm(p)
}

function applyToForm(p) {
  form.name = p.name
  form.kind = p.kind
  form.base_url = p.base_url
  // 还没配过密钥的：展开时就把「启用」勾上 —— 平铺之后用户的动线就是
  // 「展开 → 粘密钥 → 保存」，如果保存完还得再回来勾一次启用，那一步没人会想到。
  // 勾是可见可改的，用户不想启用自己取消即可。
  form.enabled = p.enabled || !!p.needs_key
  form.api_key = ''            // 永不回填密钥
  form.modelsText = (p.models || []).join(', ')
  form.temperature = p.params?.temperature ?? 0.7
  form.num_predict = p.params?.num_predict ?? 600
  form.top_p = p.params?.top_p ?? null
  form.top_k = p.params?.top_k ?? null
  form.system_prompt = p.params?.system_prompt || ''
}

function buildProviderPayload() {
  const payload = {
    name: form.name,
    kind: form.kind,
    base_url: form.base_url,
    enabled: form.enabled,
    models: form.modelsText.split(',').map(s => s.trim()).filter(Boolean),
    params: {
      temperature: form.temperature,
      num_predict: form.num_predict,
      top_p: form.top_p === '' || form.top_p === null ? null : form.top_p,
      top_k: form.top_k === '' || form.top_k === null ? null : form.top_k,
      system_prompt: form.system_prompt
    }
  }
  // 密钥只在用户真填了东西时才带上 —— 字段缺席即「保持原密钥」，留空不动它。
  if (form.api_key) payload.api_key = form.api_key
  // preset 不带：后端会沿用档案里原来的那个（见 upsert_provider 的 setdefault），
  // 带上来反而容易被一个手滑的空值把分组依据抹掉。
  return payload
}

async function load() {
  loading.value = true
  try {
    const data = await fetchProviders()
    providers.value = data.providers || []
    kinds.value = data.kinds || []
    rebuildEffective(data.bindings || {})
  } catch (e) {
    flash('error', e.message || '加载提供商失败')
  } finally {
    loading.value = false
  }
}

// 后端 get_bindings 返回的是**解析后**的结果（含 explicit 标记）。
// 全局模型草稿以 chat 为准：解析后的 provider_id 才是「现在真正在用的那个」，
// 所以即使还没显式绑定过，下拉框也预选它 —— 保存一次即固化成显式绑定。
function rebuildEffective(bindings) {
  const list = []
  for (const purpose of ['chat', 'judge', 'draft']) {
    const b = bindings[purpose]
    if (b) list.push(b)
  }
  effectiveList.value = list
  const chat = bindings.chat
  if (chat) {
    global.provider_id = chat.provider_id || ''
    global.model = chat.model || ''
  }
}

async function saveGlobal() {
  savingGlobal.value = true
  try {
    // 三个用途写同一份绑定：只写 chat 的话，judge/draft 会继续按能力回落，
    // 一旦以后加了第二个启用提供商，就可能悄悄跑到别家去 —— 那不是「全局模型」。
    const payload = {}
    for (const purpose of ['chat', 'judge', 'draft']) {
      payload[purpose] = global.provider_id
        ? { provider_id: global.provider_id, model: global.model || '' }
        : null
    }
    await saveBindings(payload)
    globalTest.value = null
    flash('ok', '全局模型已保存，立即生效')
    await load()
  } catch (e) {
    flash('error', e.message || '保存失败')
  } finally {
    savingGlobal.value = false
  }
}

async function testGlobal() {
  testingGlobal.value = true
  globalTest.value = null
  try {
    const r = await testProvider(global.provider_id, global.model || '')
    globalTest.value = { ok: true, ...r }
  } catch (e) {
    globalTest.value = { ok: false, error: e.message || '测试失败' }
  } finally {
    testingGlobal.value = false
  }
}

async function probeGlobalModels() {
  probingGlobal.value = true
  try {
    const data = await fetchProviderModels(global.provider_id)
    const names = (data.models || []).map(m => m.name)
    if (data.error) flash('error', data.error)
    if (names.length) {
      const p = providers.value.find(x => x.id === global.provider_id)
      if (p) p.models = names          // 让下拉框立刻拿到真实列表
      if (!global.model) global.model = names[0]
      flash('ok', `拉取到 ${names.length} 个模型`)
    } else if (!data.error) {
      flash('error', '没拉到模型；确认地址与密钥是否正确')
    }
  } catch (e) {
    flash('error', e.message || '拉取模型失败')
  } finally {
    probingGlobal.value = false
  }
}

async function saveProvider(p) {
  if (!form.name || !form.base_url) {
    return flash('error', '请填写名称与服务地址')
  }
  saving.value = true
  try {
    await updateProvider(p.id, buildProviderPayload())
    // 提示里点明「启用没启用」：平铺之后卡片多了，保存完最容易糊涂的就是这一条
    flash('ok', form.enabled ? `「${form.name}」已保存并启用` : `「${form.name}」已保存（未启用）`)
    await load()
  } catch (e) {
    flash('error', e.message || '保存失败')
  } finally {
    saving.value = false
  }
}

async function removeProvider(p) {
  if (!confirm(`确定删除提供商「${p.name}」？正在被全局模型使用的删不掉，请先把全局模型切到别家。`)) return
  try {
    await deleteProvider(p.id)
    if (expanded.value === p.id) expanded.value = ''
    flash('ok', `已删除「${p.name}」`)
    await load()
  } catch (e) {
    flash('error', e.message || '删除失败')
  }
}

async function testProviderById(p) {
  testing.value = p.id
  testResult.value = null
  try {
    // 拿「该提供商被 chat 绑定时用的那个模型」去测，测的就是真正会跑的那条路；
    // 没绑/没填就让后端用该提供商的第一个模型。
    const bound = global.provider_id === p.id ? global.model : ''
    const r = await testProvider(p.id, bound || '')
    testResult.value = { pid: p.id, ok: true, ...r }
  } catch (e) {
    testResult.value = { pid: p.id, ok: false, error: e.message || '测试失败' }
  } finally {
    testing.value = ''
  }
}

async function probeModels(p) {
  probing.value = p.id
  try {
    const data = await fetchProviderModels(p.id)
    const names = (data.models || []).map(m => m.name)
    if (data.error) flash('error', data.error)
    if (names.length) {
      form.modelsText = names.join(', ')
      flash('ok', `拉取到 ${names.length} 个模型，已填入表单（记得保存）`)
      // 让列表卡片也立刻反映新模型，避免「表单有、卡片没有」的错觉
      p.models = names
    } else if (!data.error) {
      flash('error', '没拉到模型；确认地址与密钥是否正确')
    }
  } catch (e) {
    flash('error', e.message || '拉取模型失败')
  } finally {
    probing.value = ''
  }
}

async function reset() {
  if (!confirm('确定恢复默认？会把当前全局模型指向的那条档案改回「本地 Ollama」的地址与默认参数。\n'
    + '自定义的提供商不会被删除，但它的服务地址会被改写。')) return
  resetting.value = true
  try {
    await resetLlmConfig()
    testResult.value = null
    globalTest.value = null
    flash('ok', '已恢复默认')
    await load()
  } catch (e) {
    flash('error', e.message || '恢复失败')
  } finally {
    resetting.value = false
  }
}

onMounted(load)
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

.cfg-card { padding: 24px 26px; margin-bottom: 24px; }
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

/* ── 全局模型 ── */
.global-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 20px 26px; }
.global-meta {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  margin-top: 18px; font-size: 13px; color: var(--text-secondary);
}
.global-meta .now { word-break: break-all; }
.global-meta .now b { color: var(--text); }
.tag-pinned { background: rgba(12, 140, 233, 0.16); color: #5caef0; border: 1px solid rgba(12, 140, 233, 0.4); }
.tag-auto { background: rgba(148, 163, 184, 0.16); color: var(--text-secondary); border: 1px solid rgba(148, 163, 184, 0.4); }
.warn-line {
  display: flex; align-items: flex-start; gap: 7px;
  margin: 14px 0 0; font-size: 12px; color: #fbbf24; line-height: 1.6;
}

/* ── 提供商分组（厂商目录平铺，按 本地/国内/聚合/海外 分区） ── */
.prov-group { margin-bottom: 22px; }
.prov-group:last-child { margin-bottom: 0; }
.prov-group-head {
  display: flex; align-items: baseline; gap: 10px;
  margin-bottom: 10px; padding-bottom: 6px;
  border-bottom: 1px dashed rgba(169, 205, 226, 0.35);
}
.prov-group-label { font-size: 13px; font-weight: 600; }
.preset-tip {
  margin-bottom: 16px; padding: 12px 14px; border-radius: 10px;
  background: rgba(24, 184, 166, 0.08); border: 1px solid rgba(24, 184, 166, 0.3);
  font-size: 13px;
}
.preset-tip b { margin-right: 10px; }
.preset-tip a { color: #5caef0; }

/* ── 提供商列表 ── */
.prov {
  border: 1px solid rgba(169, 205, 226, 0.3); border-radius: 12px;
  margin-bottom: 12px; overflow: hidden; transition: border 0.15s;
}
.prov:hover { border-color: rgba(12, 140, 233, 0.45); }
.prov-off { opacity: 0.66; }
.prov-head {
  display: flex; justify-content: space-between; align-items: center; gap: 14px;
  flex-wrap: wrap; padding: 13px 16px; cursor: pointer;
  background: rgba(10, 42, 66, 0.03);
}
.prov-title { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; min-width: 0; }
.prov-name { font-size: 14px; font-weight: 600; }
.prov-kind { font-size: 12px; color: var(--text-secondary); }
.prov-meta { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.prov-url { font-size: 12px; color: var(--text-secondary); word-break: break-all; }
.prov-models { font-size: 12px; color: var(--text-secondary); white-space: nowrap; }
.prov-caret { color: var(--text-secondary); flex-shrink: 0; }
.prov-body { padding: 18px 16px 20px; border-top: 1px solid rgba(169, 205, 226, 0.22); }

.badge-readonly { background: rgba(148, 163, 184, 0.16); color: var(--text-secondary); border: 1px solid rgba(148, 163, 184, 0.4); }
.badge-warn { background: rgba(245, 158, 11, 0.14); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); }
.badge-env { background: rgba(12, 140, 233, 0.14); color: #5caef0; border: 1px solid rgba(12, 140, 233, 0.4); }
/* 「未配置密钥」是常态而非错误（目录里绝大多数厂商用户都不会配），故用中性灰不用琥珀 */
.badge-todo { background: rgba(148, 163, 184, 0.14); color: var(--text-secondary); border: 1px solid rgba(148, 163, 184, 0.4); }

.switch-line { display: flex; align-items: center; gap: 9px; font-size: 13px; padding: 11px 0; }
.switch-line input { width: 16px; height: 16px; accent-color: var(--primary); }

.hint { font-size: 12px; color: var(--text-secondary); line-height: 1.6; }
.hint-warn { color: #fbbf24; }
.hint code { background: rgba(10, 42, 66, 0.07); padding: 1px 5px; border-radius: 4px; }

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

.footnote { margin: 12px 0 0; font-size: 12px; color: var(--text-secondary); line-height: 1.6; }

.dim { color: var(--text-secondary); }

@media (max-width: 768px) {
  .dev-tabs { width: 100%; }
  .dev-tab { flex: 1; padding: 9px 12px; }
  .cfg-card { padding: 18px 16px; margin-bottom: 16px; }
  .cfg-grid { grid-template-columns: 1fr; gap: 16px; }
  .field-full { grid-column: auto; }
  .btn-row { flex-direction: column; }
  .btn-primary, .btn-mini { width: 100%; }
  /* 窄屏一律单列：两列/多列的网格会横向溢出 */
  .global-row { grid-template-columns: 1fr; gap: 16px; }
  .prov-head { flex-direction: column; align-items: flex-start; gap: 8px; }
}
</style>
