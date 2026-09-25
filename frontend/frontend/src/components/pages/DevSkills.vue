<template>
  <div class="dev-skills">
    <!-- 顶部说明 -->
    <div class="tip-card">
      <p><AppIcon name="lightbulb" inline /> 自定义技能 = 一份 <b>skill.md</b> 操作手册。提交后智能体「小慧」在对话中命中触发词就会自动使用它，严格按你的手册操作（无需写代码，纯 LLM 驱动）。</p>
    </div>

    <!-- 编辑区 -->
    <div class="card editor-card" v-if="editorOpen">
      <div class="card-head">
        <h3>
          <template v-if="editing"><AppIcon name="edit" inline /> 编辑技能：{{ editing.name }}</template>
          <template v-else><AppIcon name="puzzle" inline /> 新建技能</template>
        </h3>
        <button class="btn-mini btn-ghost" @click="closeEditor"><AppIcon name="x" inline /> 收起</button>
      </div>
      <p class="dim">按模板编写 <code>---</code> 元信息（name/label/icon/min_level/trigger_keywords）+ 正文操作手册。</p>
      <textarea v-model="md" class="md-editor" spellcheck="false" placeholder="粘贴或编写 skill.md…"></textarea>
      <div class="btn-row">
        <button class="btn-primary" :disabled="saving" @click="doSave">
          <AppIcon name="save" inline /> {{ saving ? '保存中…' : editing ? '保存修改' : '创建技能' }}
        </button>
        <button class="btn-mini btn-ok" :disabled="validating" @click="doValidate">
          <AppIcon name="search" inline /> {{ validating ? '校验中…' : '校验' }}
        </button>
        <button class="btn-mini btn-ghost" @click="fillTemplate"><AppIcon name="refresh-cw" inline /> 填入模板</button>
      </div>
      <div v-if="validation" class="test-result" :class="validation.ok ? 'test-ok' : 'test-err'">
        <pre>{{ validation.text }}</pre>
      </div>
    </div>

    <!-- 技能列表 -->
    <div class="card list-card">
      <div class="card-head">
        <h3>我的自定义技能（{{ skills.length }}）</h3>
        <div class="head-actions">
          <button class="btn-mini btn-ghost" :disabled="loading" @click="loadAll">
            <AppIcon name="refresh-cw" inline /> {{ loading ? '刷新中…' : '刷新' }}
          </button>
          <button class="btn-mini btn-ok" @click="openCreate"><AppIcon name="plus" inline /> 新建技能</button>
        </div>
      </div>

      <p v-if="skills.length === 0 && !loading" class="dim empty-tip">
        还没有自定义技能。点击「＋ 新建技能」，按模板写一份 skill.md 就能让智能体用上。
      </p>

      <div class="skill-list">
        <div v-for="s in skills" :key="s.id" class="skill-row">
          <span class="skill-icon"><AppIcon :name="s.icon || 'puzzle'" :size="26" /></span>
          <div class="skill-main">
            <div class="skill-title">
              <b>{{ s.label }}</b>
              <code class="skill-name">{{ s.name }}</code>
              <span class="lv-badge">Lv{{ s.min_level }}</span>
              <span class="badge" :class="s.status === 'active' ? 'badge-online' : 'badge-offline'">
                {{ s.status === 'active' ? '● 生效中' : '○ 已停用' }}
              </span>
            </div>
            <div class="skill-desc">{{ s.description }}</div>
            <div class="skill-kws">触发词：<span v-for="(k, i) in splitKws(s.trigger_keywords)" :key="i" class="kw">{{ k }}</span></div>
          </div>
          <div class="skill-actions">
            <button class="btn-mini btn-ghost" @click="openEdit(s)">编辑</button>
            <button class="btn-mini" :class="s.status === 'active' ? 'btn-danger' : 'btn-ok'" @click="toggle(s)">
              {{ s.status === 'active' ? '停用' : '启用' }}
            </button>
            <button class="btn-mini btn-danger" @click="remove(s)">删除</button>
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
import { ref, onMounted } from 'vue'
import AppIcon from '../AppIcon.vue'
import { fetchSkillTemplate, fetchSkills, fetchSkill, createSkill, updateSkill, deleteSkill, setSkillStatus, validateSkill } from '../../api/dev.js'

const skills = ref([])
const loading = ref(false)
const editorOpen = ref(false)
const editing = ref(null)          // 正在编辑的技能（null=新建）
const md = ref('')
const saving = ref(false)
const validating = ref(false)
const validation = ref(null)       // { ok, text }
const msg = ref({ type: '', text: '' })

let msgTimer = null
function flash(type, text) {
  msg.value = { type, text }
  if (msgTimer) clearTimeout(msgTimer)
  msgTimer = setTimeout(() => (msg.value = { type: '', text: '' }), 3500)
}

function splitKws(raw) {
  return String(raw || '').split(',').filter(Boolean)
}

async function loadAll() {
  loading.value = true
  try {
    skills.value = await fetchSkills() || []
  } catch (e) {
    flash('error', e.message || '加载失败')
  } finally {
    loading.value = false
  }
}

async function fillTemplate() {
  try {
    const t = await fetchSkillTemplate()
    md.value = t.template || ''
    validation.value = null
  } catch (e) {
    flash('error', e.message || '获取模板失败')
  }
}

function openCreate() {
  editing.value = null
  editorOpen.value = true
  md.value = ''
  validation.value = null
  fillTemplate()
}

async function openEdit(s) {
  editing.value = s
  editorOpen.value = true
  validation.value = null
  md.value = ''
  try {
    const full = await fetchSkill(s.id)
    md.value = full.manual_md || ''
  } catch (e) {
    flash('error', e.message || '加载技能详情失败')
  }
}

function closeEditor() {
  editorOpen.value = false
  editing.value = null
}

async function doValidate() {
  if (!md.value.trim()) return flash('error', '请先填写 skill.md')
  validating.value = true
  try {
    const p = await validateSkill(md.value)
    validation.value = { ok: true, text: '校验通过\n' + [
      `名称：${p.name}（${p.label} ${p.icon}）`,
      `解锁等级：Lv${p.min_level}`,
      `说明：${p.description}`,
      `触发词：${p.trigger_keywords.join('、')}`
    ].join('\n') }
  } catch (e) {
    validation.value = { ok: false, text: (e.message || '校验失败') }
  } finally {
    validating.value = false
  }
}

async function doSave() {
  if (!md.value.trim()) return flash('error', '请先填写 skill.md')
  saving.value = true
  try {
    if (editing.value) {
      await updateSkill(editing.value.id, md.value)
      flash('ok', `技能「${editing.value.name}」已更新，对话中立即生效`)
    } else {
      await createSkill(md.value)
      flash('ok', '技能已创建，对话中命中触发词即可使用')
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

async function toggle(s) {
  const next = s.status === 'active' ? 'disabled' : 'active'
  try {
    await setSkillStatus(s.id, next)
    s.status = next
    flash('ok', next === 'active' ? `已启用「${s.name}」` : `已停用「${s.name}」`)
  } catch (e) {
    flash('error', e.message || '操作失败')
  }
}

async function remove(s) {
  if (!confirm(`确定删除技能「${s.name}」？删除后智能体将不再使用它。`)) return
  try {
    await deleteSkill(s.id)
    skills.value = skills.value.filter(x => x.id !== s.id)
    flash('ok', `已删除「${s.name}」`)
  } catch (e) {
    flash('error', e.message || '删除失败')
  }
}

onMounted(loadAll)
</script>

<style scoped>
.tip-card {
  padding: 12px 18px; border-radius: 12px; margin-bottom: 20px;
  background: rgba(12, 140, 233, 0.07); border: 1px solid rgba(12, 140, 233, 0.25);
  font-size: 13px; color: var(--text);
}
.tip-card code { background: rgba(12, 140, 233, 0.12); padding: 1px 6px; border-radius: 4px; }

.editor-card, .list-card { padding: 22px 26px; margin-bottom: 24px; }
.card-head {
  display: flex; justify-content: space-between; align-items: center; gap: 14px; flex-wrap: wrap;
  margin-bottom: 16px; padding-bottom: 14px; border-bottom: 1px solid rgba(169, 205, 226, 0.18);
}
.card-head h3 { margin: 0; font-size: 16px; }
.head-actions { display: flex; align-items: center; gap: 10px; }
.dim { color: var(--text-secondary); }
.empty-tip { padding: 10px 0; }

.md-editor {
  width: 100%; box-sizing: border-box; min-height: 340px;
  padding: 14px 16px; border: 1px solid var(--border); border-radius: 10px;
  font-family: 'Consolas', 'Courier New', monospace; font-size: 13px; line-height: 1.6;
  background: #0d1b2a; color: #dce6f2; resize: vertical; outline: none;
}
.md-editor:focus { border-color: var(--primary); }

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
.test-result pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-family: inherit; }
.test-ok { background: rgba(24, 184, 166, 0.1); border: 1px solid rgba(24, 184, 166, 0.35); color: #7ff0e2; }
.test-err { background: rgba(244, 63, 94, 0.1); border: 1px solid rgba(244, 63, 94, 0.4); color: #fb7185; }

.skill-list { display: flex; flex-direction: column; gap: 12px; }
.skill-row {
  display: flex; gap: 14px; align-items: flex-start;
  padding: 14px 16px; border: 1px solid var(--border); border-radius: 12px;
  background: rgba(255, 255, 255, 0.02);
}
.skill-icon { font-size: 26px; line-height: 1; flex-shrink: 0; }
.skill-main { flex: 1; min-width: 0; }
.skill-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.skill-title b { font-size: 15px; }
.skill-name { font-size: 11px; color: var(--text-secondary); background: rgba(12, 140, 233, 0.1); padding: 1px 6px; border-radius: 4px; }
.lv-badge { font-size: 11px; font-weight: 700; color: #b45309; background: rgba(180, 83, 9, 0.12); padding: 1px 8px; border-radius: 999px; }
.badge { font-size: 11px; font-weight: 600; padding: 2px 10px; border-radius: 999px; }
.badge-online { background: rgba(24, 184, 166, 0.15); color: #2ec4a0; border: 1px solid rgba(24, 184, 166, 0.4); }
.badge-offline { background: rgba(244, 63, 94, 0.12); color: #fb7185; border: 1px solid rgba(244, 63, 94, 0.4); }
.skill-desc { font-size: 13px; color: var(--text-secondary); margin: 6px 0; }
.skill-kws { font-size: 12px; color: var(--text-secondary); display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.kw { background: rgba(12, 140, 233, 0.08); border: 1px solid rgba(12, 140, 233, 0.2); padding: 1px 8px; border-radius: 999px; }
.skill-actions { display: flex; gap: 8px; flex-shrink: 0; }

.msg { margin-top: 16px; padding: 10px 14px; border-radius: 8px; font-size: 13px; }
.msg-ok { background: rgba(24, 184, 166, 0.16); color: #7ff0e2; border: 1px solid rgba(24, 184, 166, 0.4); }
.msg-error { background: rgba(244, 63, 94, 0.14); color: #fb7185; border: 1px solid rgba(244, 63, 94, 0.4); }
.fade-enter-active, .fade-leave-active { transition: opacity 0.2s; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

@media (max-width: 768px) {
  .editor-card, .list-card { padding: 18px 16px; margin-bottom: 16px; }
  .skill-actions { flex-direction: column; }
}
</style>
