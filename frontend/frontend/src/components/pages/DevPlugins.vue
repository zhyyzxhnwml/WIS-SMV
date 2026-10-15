<template>
  <div class="dev-plugins">
    <!-- 顶部说明：这里最需要说清的是「它和 skill.md 有什么不一样」以及「风险在哪」 -->
    <div class="tip-card">
      <p>
        <AppIcon name="zap" inline />
        可执行插件 = <b>manifest</b>（元信息）+ <b>Python 判定代码</b>，一个文件。
        每批数据导入时，已启用的插件会对每艘船跑一遍，产出**附加判定**。
        它<b>不改变</b>系统的基线结论 —— 违规判定与风险等级仍由 L1 实测证据和确定性引擎给出。
      </p>
      <p class="tip-sub">
        插件跑在独立子进程里（没有数据库连接、没有密钥），输入是只读的 facts。
        想让它判什么，可以在悬浮的「级联监管者」里直接说
        「给我做个 XX 判定插件」，由 AI 起草后再到这里试跑、启用。
      </p>
    </div>

    <!-- 编辑区 -->
    <div v-if="editorOpen" class="card editor-card">
      <div class="card-head">
        <h3>
          <template v-if="editing"><AppIcon name="edit" inline /> 编辑插件：{{ editing.label }}</template>
          <template v-else><AppIcon name="plus" inline /> 新建插件</template>
        </h3>
        <button class="btn-mini btn-ghost" @click="closeEditor"><AppIcon name="x" inline /> 收起</button>
      </div>

      <div class="split">
        <label class="field-group">
          <span>manifest（<code>---</code> 包住的元信息）</span>
          <textarea v-model="manifestYaml" class="code-editor short" spellcheck="false"
            placeholder="name: my_plugin&#10;label: 我的判定插件&#10;..." />
        </label>
        <div class="manifest-note">
          <p v-if="parsedMeta" class="dim">
            解析：<b>{{ parsedMeta.label }}</b>
            <code>{{ parsedMeta.name }}</code>
            <span class="badge">hooks: {{ parsedMeta.hooks || '—' }}</span>
            <span class="badge">超时 {{ parsedMeta.timeout_ms || '—' }} ms</span>
          </p>
          <p v-else class="dim">manifest 还没填或格式不对。</p>
          <p class="dim">
            触发词决定「对话里问到什么时会用上它」；
            <b>timeout_ms</b> 上限 3000，<b>max_findings</b> 上限 50 —— 写超了会被直接拒。
          </p>
        </div>
      </div>

      <label class="field-group">
        <span>Python 判定代码（<code>def apply(ctx)</code> + 注册 handler）</span>
        <textarea v-model="sourcePy" class="code-editor" spellcheck="false"
          placeholder="PLUGIN = {&quot;name&quot;: &quot;my_plugin&quot;, &quot;entry_tool&quot;: &quot;adjudicate&quot;}&#10;&#10;def apply(ctx):&#10;    ctx.tools.register(name=&quot;adjudicate&quot;, handler=_judge)&#10;" />
      </label>

      <div class="btn-row">
        <button class="btn-primary" :disabled="saving" @click="doSave">
          <AppIcon name="save" inline /> {{ saving ? '保存中…' : editing ? '保存为新版本' : '创建插件' }}
        </button>
        <button class="btn-mini btn-ok" :disabled="validating" @click="doValidate">
          <AppIcon name="search" inline /> {{ validating ? '校验中…' : '静态校验' }}
        </button>
        <button class="btn-mini btn-ok" :disabled="trialing" @click="doTrial">
          <AppIcon name="flask" inline /> {{ trialing ? '试跑中…' : '试跑' }}
        </button>
        <button class="btn-mini btn-ghost" @click="fillTemplate">
          <AppIcon name="refresh-cw" inline /> 填入模板
        </button>
      </div>

      <p class="dim footnote">
        「静态校验」只看代码（导入白名单、禁用名字、双下划线属性、有无副作用）；
        「试跑」是拿 3 份内置样本在子进程里真跑一遍，其中一份<b>明确不该命中</b>
        —— 任何输入都返回违规的「恒真插件」会在这一步被挡下。
      </p>

      <div v-if="report" class="test-result" :class="report.ok ? 'test-ok' : 'test-err'">
        <pre>{{ report.text }}</pre>
      </div>
    </div>

    <!-- 插件列表 -->
    <div class="card list-card">
      <div class="card-head">
        <h3>插件（{{ plugins.length }}）</h3>
        <div class="head-actions">
          <button class="btn-mini btn-ghost" :disabled="loading" @click="loadAll">
            <AppIcon name="refresh-cw" inline /> {{ loading ? '刷新中…' : '刷新' }}
          </button>
          <button class="btn-mini btn-ok" @click="openCreate"><AppIcon name="plus" inline /> 新建插件</button>
        </div>
      </div>

      <p v-if="!plugins.length && !loading" class="dim empty-tip">
        还没有插件。点「＋ 新建插件」手写一个，或者到悬浮的「级联监管者」里说
        「给我做个 XX 判定插件」，让 AI 起草一个草稿。
      </p>

      <!-- 从悬浮对话跳过来的落点提示。草稿本身没有「运行记录」，
           不说一句用户会以为插件没生效。 -->
      <div v-if="focusedPlugin && focusedPlugin.status !== 'enabled'" class="focus-banner">
        <AppIcon name="zap" inline />
        <span>
          这是刚起草的「<b>{{ focusedPlugin.label }}</b>」（下面高亮那条），现在是
          <b>草稿</b>、还没参与判定。点它那一行的<b>启用</b>即开始生效；
          启用前建议先「试跑」看一眼它对你那批数据判得对不对。
        </span>
      </div>

      <div class="plug-list">
        <div v-for="p in plugins" :key="p.id" class="plug-row"
          :data-plug-id="p.id"
          :class="{ 'plug-suspended': p.status === 'suspended', 'plug-focus': focusedId === p.id }">
          <span class="plug-icon">{{ p.icon || '🧩' }}</span>
          <div class="plug-main">
            <div class="plug-title">
              <b>{{ p.label }}</b>
              <code class="plug-name">{{ p.name }}</code>
              <span class="badge" :class="statusClass(p.status)">{{ statusLabel(p.status) }}</span>
              <span class="badge" :class="originClass(p.origin)">{{ originLabel(p.origin) }}</span>
              <span class="badge">v{{ p.current_version }}</span>
              <span v-if="p.can_raise_risk" class="badge badge-risk">可抬升风险</span>
            </div>
            <div class="plug-desc">{{ p.description }}</div>
            <div class="plug-kws">
              触发词：
              <span v-for="(k, i) in splitKws(p.trigger_keywords)" :key="i" class="kw">{{ k }}</span>
              <span v-if="!splitKws(p.trigger_keywords).length" class="dim">（未设，对话中不会被选中）</span>
            </div>
            <div v-if="p.last_error" class="plug-err">
              <AppIcon name="alert-triangle" inline /> 最近错误：{{ p.last_error }}
              <span v-if="p.error_streak">（连续失败 {{ p.error_streak }} 次）</span>
            </div>
          </div>
          <div class="plug-actions">
            <button class="btn-mini btn-ghost" @click="openEdit(p)">编辑</button>
            <button class="btn-mini btn-ghost" @click="openFindings(p)">
              {{ findingsOf === p.id ? '收起记录' : '运行记录' }}
            </button>
            <button v-if="canPublish" class="btn-mini btn-ghost" @click="openVersions(p)">版本</button>

            <!-- 启用/停用只用 plugin.publish（仅超管）。没权限就不显示按钮 ——
                 显示一个点了必 403 的按钮比不显示更糟。 -->
            <template v-if="canPublish">
              <button v-if="p.status !== 'enabled'" class="btn-mini btn-ok" @click="doEnable(p)">启用</button>
              <button v-else class="btn-mini btn-danger" @click="doDisable(p)">停用</button>
              <button class="btn-mini btn-danger" @click="doDelete(p)">删除</button>
            </template>
          </div>

          <!-- 运行记录：失败行单独标出来，和正常判定混在一起看会误判 -->
          <div v-if="findingsOf === p.id" class="plug-panel">
            <div class="panel-head">
              <b>运行记录</b>
              <span class="dim">最近 {{ findings.length }} 条</span>
            </div>
            <p v-if="!findings.length" class="dim">还没有运行记录（启用后每批数据都会留下痕迹）。</p>
            <table v-else class="find-table">
              <thead>
                <tr><th>时间</th><th>结果</th><th>MMSI</th><th>判定 / 错误</th><th>把握</th><th>耗时</th></tr>
              </thead>
              <tbody>
                <tr v-for="f in findings" :key="f.id" :class="{ 'row-fail': f.result !== 'ok' }">
                  <td class="td-ts">{{ f.analysis_time || f.created_at }}</td>
                  <td><span class="badge" :class="f.result === 'ok' ? 'badge-online' : 'badge-offline'">{{ f.result }}</span></td>
                  <td>{{ f.mmsi }}</td>
                  <td class="td-main">
                    <template v-if="f.result === 'ok'">
                      <b>{{ f.label || f.code }}</b>
                      <div class="td-ev">{{ f.evidence }}</div>
                    </template>
                    <span v-else class="dim">{{ f.error_text }}</span>
                  </td>
                  <td>{{ f.result === 'ok' ? Math.round((f.confidence || 0) * 100) + '%' : '—' }}</td>
                  <td>{{ f.elapsed_ms }} ms</td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- 版本历史：回滚只改指针，不删任何一版 -->
          <div v-if="versionsOf === p.id" class="plug-panel">
            <div class="panel-head">
              <b>版本历史</b>
              <span class="dim">当前 v{{ p.current_version }}；回滚只改指针，不删版本</span>
            </div>
            <div v-for="v in versions" :key="v.id" class="ver-row">
              <span class="badge" :class="v.version === p.current_version ? 'badge-online' : ''">
                v{{ v.version }}{{ v.version === p.current_version ? ' · 当前' : '' }}
              </span>
              <span class="dim">{{ v.created_at }}</span>
              <span class="dim">{{ v.created_by || '—' }}</span>
              <span class="dim">{{ v.source_bytes }} 字节</span>
              <button v-if="canPublish && v.version !== p.current_version"
                class="btn-mini btn-ghost" @click="doRollback(p, v.version)">
                回滚到这一版
              </button>
            </div>
          </div>
        </div>
      </div>

      <transition name="fade">
        <div v-if="msg.text" class="msg" :class="'msg-' + msg.type">{{ msg.text }}</div>
      </transition>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, nextTick } from 'vue'
import AppIcon from '../AppIcon.vue'
import { can } from '../../store/user.js'
import {
  fetchPlugins, fetchPlugin, fetchPluginTemplate, createPlugin, updatePlugin,
  validatePlugin, trialPlugin, enablePlugin, disablePlugin, rollbackPlugin, deletePlugin,
  fetchPluginFindings, fetchPluginVersions,
} from '../../api/dev.js'

// 后端 @perm_required 才是真门禁；这里只是别让人点了才发现 403。
// 从悬浮对话的「查看草稿」跳过来时带上插件 id：落在这一页还不够，
// 得把它滚到眼前并高亮 —— 列表里几十条，让用户自己找等于这个按钮白做。
const props = defineProps({
  focusPlugin: { type: Number, default: null },
})
const focusedId = ref(null)
const focusedPlugin = computed(() => plugins.value.find(p => p.id === focusedId.value) || null)

const canPublish = computed(() => can('plugin.publish'))

const plugins = ref([])
const loading = ref(false)
const editorOpen = ref(false)
const editing = ref(null)          // 正在编辑的插件（null=新建）
const manifestYaml = ref('')
const sourcePy = ref('')
const saving = ref(false)
const validating = ref(false)
const trialing = ref(false)
const report = ref(null)           // { ok, text }
const msg = ref({ type: '', text: '' })

const findingsOf = ref(null)
const findings = ref([])
const versionsOf = ref(null)
const versions = ref([])

let msgTimer = null
function flash(type, text) {
  msg.value = { type, text }
  if (msgTimer) clearTimeout(msgTimer)
  msgTimer = setTimeout(() => (msg.value = { type: '', text: '' }), 4000)
}

function splitKws(raw) {
  return String(raw || '').split(',').map(s => s.trim()).filter(Boolean)
}

// manifest 只做「够用就好」的本地预览，真解析在后端 —— 前端不引 YAML 库，
// 也不该出现两套解析规则（那正是「页面显示 1500ms、实际跑 1000ms」的来源）。
const parsedMeta = computed(() => {
  const out = {}
  for (const line of String(manifestYaml.value || '').split('\n')) {
    const t = line.trim()
    if (!t || t.startsWith('#') || !t.includes(':')) continue
    const i = t.indexOf(':')
    out[t.slice(0, i).trim().toLowerCase()] = t.slice(i + 1).trim()
  }
  return out.name ? out : null
})

const STATUS_LABELS = {
  draft: '草稿', enabled: '● 生效中', disabled: '○ 已停用',
  rejected: '生成失败', suspended: '⚠ 已熔断',
}
const statusLabel = s => STATUS_LABELS[s] || s
function statusClass(s) {
  if (s === 'enabled') return 'badge-online'
  if (s === 'suspended') return 'badge-suspended'
  if (s === 'rejected') return 'badge-offline'
  return 'badge-draft'
}

const ORIGIN_LABELS = { ai: 'AI 起草', manual: '手写', system: '系统预置' }
const originLabel = o => ORIGIN_LABELS[o] || o
function originClass(o) {
  return o === 'ai' ? 'badge-ai' : (o === 'system' ? 'badge-sys' : 'badge-manual')
}

async function loadAll() {
  loading.value = true
  try {
    plugins.value = await fetchPlugins() || []
  } catch (e) {
    flash('error', e.message || '加载失败')
  } finally {
    loading.value = false
  }
}

async function fillTemplate() {
  try {
    const t = await fetchPluginTemplate()
    const text = t.template || ''
    // 模板是「完整插件文件」（--- 元信息 --- + 正文），切开分别填进两个框
    const parts = text.split('---')
    if (parts.length >= 3) {
      manifestYaml.value = parts[1].trim()
      sourcePy.value = parts.slice(2).join('---').trim()
    } else {
      sourcePy.value = text
    }
    report.value = null
  } catch (e) {
    flash('error', e.message || '获取模板失败')
  }
}

function openCreate() {
  editing.value = null
  editorOpen.value = true
  manifestYaml.value = ''
  sourcePy.value = ''
  report.value = null
  fillTemplate()
}

async function openEdit(p) {
  editing.value = p
  editorOpen.value = true
  report.value = null
  try {
    const full = await fetchPlugin(p.id)
    manifestYaml.value = full.manifest_yaml || ''
    sourcePy.value = full.source_py || ''
    // 源码里含 frontmatter，切成两框显示；不切的话用户会在正文框里看到重复的元信息
    const parts = String(full.source_py || '').split('---')
    if (parts.length >= 3 && parts[0].trim() === '') {
      manifestYaml.value = parts[1].trim()
      sourcePy.value = parts.slice(2).join('---').trim()
    }
  } catch (e) {
    flash('error', e.message || '加载插件详情失败')
  }
}

function closeEditor() {
  editorOpen.value = false
  editing.value = null
}

function payload() {
  return { manifest_yaml: manifestYaml.value, source_py: sourcePy.value }
}

function describeValidation(v) {
  return [
    `名称：${v.name}（${v.label}）`,
    `hooks：${(v.hooks || []).join(', ')}`,
    `超时：${v.timeout_ms} ms　最多：${v.max_findings} 条`,
    `注册的工具：${(v.registered_tools || []).join(', ') || '—'}`,
    `AST 节点：${v.ast_nodes}　体积：${v.bytes} 字节`,
    `触发词：${(v.trigger_keywords || []).join('、') || '（未设）'}`,
  ].join('\n')
}

function describeTrial(t) {
  const lines = ['动态试跑（子进程真跑）：']
  for (const r of t.results || []) {
    const mark = r.ok ? '✓' : '✗'
    lines.push(`  ${mark} ${r.expect === 'hit' ? '该命中' : '不该命中'}　`
      + `命中 ${r.n} 条${r.codes?.length ? '（' + r.codes.join(', ') + '）' : ''}`
      + (r.note ? `　${r.note}` : ''))
  }
  return lines.join('\n')
}

async function doValidate() {
  if (!manifestYaml.value.trim() || !sourcePy.value.trim()) {
    return flash('error', 'manifest 与 Python 代码都要填（校验要一起看）')
  }
  validating.value = true
  report.value = null
  try {
    // 新建时还没有 id，用 0 走「只校验请求体」的分支（后端会优先用请求里的内容）
    const v = await validatePlugin(editing.value ? editing.value.id : 0, payload())
    report.value = { ok: true, text: '静态校验通过\n' + describeValidation(v) }
  } catch (e) {
    report.value = { ok: false, text: e.message || '校验失败' }
  } finally {
    validating.value = false
  }
}

async function doTrial() {
  if (!manifestYaml.value.trim() || !sourcePy.value.trim()) {
    return flash('error', '试跑要带上 manifest —— 插件名、hooks、超时都在里面')
  }
  trialing.value = true
  report.value = null
  try {
    const t = await trialPlugin(editing.value ? editing.value.id : 0, payload())
    report.value = { ok: true, text: describeTrial(t) }
  } catch (e) {
    // 试跑失败时后端把完整结果放在 data 里，尽量展示出来，别只说「失败了」
    const d = e.data || e.response?.data?.data
    report.value = {
      ok: false,
      text: (e.message || '试跑失败') + (d ? '\n\n' + describeTrial(d) : ''),
    }
  } finally {
    trialing.value = false
  }
}

async function doSave() {
  if (!manifestYaml.value.trim() || !sourcePy.value.trim()) {
    return flash('error', 'manifest 与 Python 代码都要填')
  }
  saving.value = true
  try {
    if (editing.value) {
      const r = await updatePlugin(editing.value.id, payload())
      flash('ok', `已保存为 v${r.version}（旧版本都还在，可随时回滚）`)
    } else {
      await createPlugin(payload())
      flash('ok', '插件草稿已创建。它还没有生效 —— 需要超级管理员启用。')
    }
    editorOpen.value = false
    editing.value = null
    await loadAll()
  } catch (e) {
    flash('error', e.message || '保存失败')
  } finally {
    saving.value = false
  }
}

async function doEnable(p) {
  if (!confirm(`启用「${p.label}」？\n\n启用后它会对**所有用户**的每批数据生效，`
    + `跑的是真代码。启用前会自动再试跑一次，不过就启用不了。`)) return
  try {
    await enablePlugin(p.id)
    flash('ok', `「${p.label}」已启用，下一批数据就会跑`)
    await loadAll()
  } catch (e) {
    flash('error', e.message || '启用失败')
  }
}

async function doDisable(p) {
  try {
    await disablePlugin(p.id)
    flash('ok', `已停用「${p.label}」`)
    await loadAll()
  } catch (e) {
    flash('error', e.message || '停用失败')
  }
}

async function doDelete(p) {
  if (!confirm(`确定删除插件「${p.label}」？\n\n版本历史和运行记录会一并删除，无法恢复。`
    + `只是想让它不起作用的话，用「停用」更好。`)) return
  try {
    await deletePlugin(p.id)
    plugins.value = plugins.value.filter(x => x.id !== p.id)
    flash('ok', `已删除「${p.label}」`)
  } catch (e) {
    flash('error', e.message || '删除失败')
  }
}

async function openFindings(p) {
  if (findingsOf.value === p.id) { findingsOf.value = null; return }
  findingsOf.value = p.id
  versionsOf.value = null
  try {
    findings.value = await fetchPluginFindings(p.id) || []
  } catch (e) {
    flash('error', e.message || '加载运行记录失败')
  }
}

async function openVersions(p) {
  if (versionsOf.value === p.id) { versionsOf.value = null; return }
  versionsOf.value = p.id
  findingsOf.value = null
  try {
    versions.value = await fetchPluginVersions(p.id) || []
  } catch (e) {
    flash('error', e.message || '加载版本失败')
  }
}

async function doRollback(p, version) {
  if (!confirm(`回滚「${p.label}」到 v${version}？\n\n只改指向，不删任何版本，随时可以再滚回来。`)) return
  try {
    await rollbackPlugin(p.id, version)
    flash('ok', `已回滚到 v${version}`)
    await loadAll()
    versions.value = await fetchPluginVersions(p.id) || []
  } catch (e) {
    flash('error', e.message || '回滚失败')
  }
}

onMounted(async () => {
  await loadAll()
  if (!props.focusPlugin) return
  const p = plugins.value.find(x => x.id === props.focusPlugin)
  if (!p) {
    flash('error', '没找到那个插件（可能已被删除）')
    return
  }
  focusedId.value = p.id
  await nextTick()
  document.querySelector(`[data-plug-id="${p.id}"]`)
    ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
})
</script>

<style scoped>
.tip-card {
  padding: 14px 18px; border-radius: 12px; margin-bottom: 20px;
  background: rgba(12, 140, 233, 0.07); border: 1px solid rgba(12, 140, 233, 0.25);
  font-size: 13px; color: var(--text); line-height: 1.75;
}
.tip-card p { margin: 0; }
.tip-card .tip-sub { margin-top: 8px; color: var(--text-secondary); font-size: 12.5px; }
.tip-card code { background: rgba(12, 140, 233, 0.12); padding: 1px 6px; border-radius: 4px; }

.editor-card, .list-card { padding: 22px 26px; margin-bottom: 24px; }
.card-head {
  display: flex; justify-content: space-between; align-items: center; gap: 14px; flex-wrap: wrap;
  margin-bottom: 16px; padding-bottom: 14px; border-bottom: 1px solid rgba(169, 205, 226, 0.18);
}
.card-head h3 { margin: 0; font-size: 16px; }
.head-actions { display: flex; align-items: center; gap: 10px; }
.dim { color: var(--text-secondary); }
.empty-tip { padding: 10px 0; line-height: 1.6; }

.split { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 0.9fr); gap: 20px; }
.field-group { display: flex; flex-direction: column; gap: 8px; margin-bottom: 16px; }
.field-group > span { font-size: 13px; color: var(--text-secondary); }
.field-group code { background: rgba(10, 42, 66, 0.07); padding: 1px 5px; border-radius: 4px; }
.manifest-note { font-size: 12px; line-height: 1.7; }
.manifest-note p { margin: 0 0 8px; }

.code-editor {
  width: 100%; box-sizing: border-box; min-height: 380px;
  padding: 14px 16px; border: 1px solid var(--border); border-radius: 10px;
  font-family: 'Consolas', 'Courier New', monospace; font-size: 13px; line-height: 1.6;
  background: #0d1b2a; color: #dce6f2; resize: vertical; outline: none;
}
.code-editor.short { min-height: 190px; }
.code-editor:focus { border-color: var(--primary); }

.btn-row { display: flex; gap: 12px; margin-top: 16px; flex-wrap: wrap; }
.btn-primary {
  background: var(--gradient-primary); color: #fff; border: none; border-radius: 9px;
  padding: 10px 26px; font-size: 14px; font-weight: 600; cursor: pointer; transition: all var(--transition);
}
.btn-primary:hover:not(:disabled) { transform: translateY(-1px); opacity: 0.92; }
.btn-primary:disabled { opacity: 0.6; cursor: not-allowed; }
.btn-mini { padding: 9px 18px; border: none; border-radius: 999px; font-size: 13px; cursor: pointer; transition: all 0.15s; }
.btn-mini:disabled { opacity: 0.6; cursor: not-allowed; }
.btn-ghost { background: rgba(12, 140, 233, 0.12); color: #5caef0; border: 1px solid rgba(12, 140, 233, 0.35); }
.btn-ghost:hover:not(:disabled) { background: rgba(12, 140, 233, 0.25); }
.btn-ok { background: rgba(24, 184, 166, 0.15); color: #2ec4a0; border: 1px solid rgba(24, 184, 166, 0.4); }
.btn-ok:hover:not(:disabled) { background: rgba(24, 184, 166, 0.3); }
.btn-danger { background: rgba(244, 63, 94, 0.12); color: #fb7185; border: 1px solid rgba(244, 63, 94, 0.35); }
.btn-danger:hover:not(:disabled) { background: rgba(244, 63, 94, 0.25); }

.test-result { margin-top: 16px; padding: 12px 16px; border-radius: 10px; font-size: 13px; }
.test-ok { background: rgba(24, 184, 166, 0.1); border: 1px solid rgba(24, 184, 166, 0.35); color: #7ff0e2; }
.test-err { background: rgba(244, 63, 94, 0.1); border: 1px solid rgba(244, 63, 94, 0.4); color: #fb7185; }
.test-result pre { margin: 0; white-space: pre-wrap; font-family: inherit; line-height: 1.7; }

/* ── 列表 ── */
.plug-row {
  display: flex; gap: 14px; align-items: flex-start; flex-wrap: wrap;
  padding: 16px 0; border-bottom: 1px solid rgba(169, 205, 226, 0.16);
}
.plug-row:last-child { border-bottom: none; }
.plug-suspended { background: rgba(244, 63, 94, 0.05); }
/* 悬浮对话跳过来的那一条：给一圈会自己褪去的光晕，指一下就好，不做成常驻状态 */
.plug-focus {
  border-radius: 10px; padding-left: 12px; padding-right: 12px;
  background: rgba(12, 140, 233, 0.07);
  animation: plugFocusFade 3.6s ease-out 1;
}
@keyframes plugFocusFade {
  0%, 45% { box-shadow: 0 0 0 2px rgba(12, 140, 233, 0.55); }
  100% { box-shadow: 0 0 0 2px rgba(12, 140, 233, 0); }
}
.focus-banner {
  display: flex; align-items: flex-start; gap: 8px;
  margin: 4px 0 14px; padding: 12px 14px; border-radius: 10px;
  font-size: 13px; line-height: 1.65;
  color: #7cc4ff; background: rgba(26, 115, 232, 0.12);
  border: 1px solid rgba(26, 115, 232, 0.4);
}
.focus-banner b { color: #a9d6ff; }
.plug-icon { font-size: 26px; line-height: 1.2; flex-shrink: 0; }
.plug-main { flex: 1; min-width: 240px; }
.plug-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.plug-title b { font-size: 14.5px; }
.plug-name { font-size: 12px; color: var(--text-secondary); background: rgba(10, 42, 66, 0.07); padding: 1px 6px; border-radius: 4px; }
.plug-desc { margin-top: 5px; font-size: 13px; color: var(--text-secondary); line-height: 1.6; }
.plug-kws { margin-top: 6px; font-size: 12px; color: var(--text-secondary); }
.plug-kws .kw {
  display: inline-block; margin: 2px 4px 0 0; padding: 1px 8px; border-radius: 999px;
  background: rgba(12, 140, 233, 0.12); color: #5caef0;
}
.plug-err { margin-top: 7px; font-size: 12px; color: #fbbf24; line-height: 1.6; }
.plug-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: flex-start; }

.badge { font-size: 12px; font-weight: 600; padding: 3px 12px; border-radius: 999px; }
.badge-online { background: rgba(24, 184, 166, 0.15); color: #2ec4a0; border: 1px solid rgba(24, 184, 166, 0.4); }
.badge-offline { background: rgba(244, 63, 94, 0.12); color: #fb7185; border: 1px solid rgba(244, 63, 94, 0.4); }
.badge-draft { background: rgba(148, 163, 184, 0.16); color: var(--text-secondary); border: 1px solid rgba(148, 163, 184, 0.4); }
.badge-suspended { background: rgba(244, 63, 94, 0.18); color: #fb7185; border: 1px solid rgba(244, 63, 94, 0.55); }
.badge-ai { background: rgba(168, 85, 247, 0.15); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.4); }
.badge-sys { background: rgba(12, 140, 233, 0.14); color: #5caef0; border: 1px solid rgba(12, 140, 233, 0.4); }
.badge-manual { background: rgba(148, 163, 184, 0.16); color: var(--text-secondary); border: 1px solid rgba(148, 163, 184, 0.4); }
.badge-risk { background: rgba(245, 158, 11, 0.16); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.45); }

.plug-panel {
  flex-basis: 100%; margin-top: 14px; padding: 14px 16px; border-radius: 10px;
  background: rgba(10, 42, 66, 0.035); border: 1px solid rgba(169, 205, 226, 0.22);
}
.panel-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 10px; font-size: 13px; }
.find-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.find-table th {
  text-align: left; padding: 6px 8px; color: var(--text-secondary);
  border-bottom: 1px solid rgba(169, 205, 226, 0.25); font-weight: 600; white-space: nowrap;
}
.find-table td { padding: 7px 8px; border-bottom: 1px solid rgba(169, 205, 226, 0.12); vertical-align: top; }
.row-fail { background: rgba(244, 63, 94, 0.06); }
.td-ts { white-space: nowrap; color: var(--text-secondary); }
.td-main { min-width: 200px; }
.td-ev { color: var(--text-secondary); margin-top: 3px; line-height: 1.55; }
.ver-row { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; padding: 7px 0; font-size: 12.5px; }

.msg { margin-top: 16px; padding: 10px 14px; border-radius: 8px; font-size: 13px; }
.msg-ok { background: rgba(24, 184, 166, 0.16); color: #7ff0e2; border: 1px solid rgba(24, 184, 166, 0.4); }
.msg-error { background: rgba(244, 63, 94, 0.14); color: #fb7185; border: 1px solid rgba(244, 63, 94, 0.4); }
.fade-enter-active, .fade-leave-active { transition: opacity 0.2s; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

@media (max-width: 768px) {
  .editor-card, .list-card { padding: 18px 16px; }
  .split { grid-template-columns: 1fr; }
  .plug-actions { width: 100%; }
  .find-table { display: block; overflow-x: auto; }
}
</style>
