<template>
  <div class="page">
    <PageHeader icon="file-text" title="审计日志" subtitle="谁、在什么时候、做了什么">
      <button class="btn-ghost" :disabled="!canView" @click="exportCsv">
        <AppIcon name="file-text" :size="14" inline /> 导出 CSV
      </button>
      <button class="btn-ghost" :disabled="!canView" @click="search">
        <AppIcon name="refresh-cw" :size="14" inline /> 刷新
      </button>
    </PageHeader>

    <div v-if="!canView" class="card forbid-card">
      <span class="forbid-icon"><AppIcon name="ban" :size="40" style="color:var(--text-secondary);" /></span>
      <p>需要「审计日志查看」权限（管理员及以上）</p>
    </div>

    <template v-else>
      <div class="card">
        <div class="card-body">
          <div class="filters">
            <input v-model.trim="f.username" class="f-input f-search" placeholder="用户名（可模糊）" @keyup.enter="search" />
            <select v-model="f.action" class="f-input f-sel">
              <option value="">全部动作</option>
              <option v-for="a in actions" :key="a.action" :value="a.action">
                {{ a.label }}（{{ a.count }}）
              </option>
            </select>
            <select v-model="f.result" class="f-input f-sel">
              <option value="">全部结果</option>
              <option value="ok">成功</option>
              <option value="fail">失败</option>
              <option value="denied">被拒绝</option>
            </select>
            <span class="f-range">
              <input v-model="f.date_from" type="date" class="f-input f-date" />
              <span class="dim">至</span>
              <input v-model="f.date_to" type="date" class="f-input f-date" />
            </span>
            <button class="btn-go" @click="search"><AppIcon name="search" :size="14" inline /> 查询</button>
            <button class="btn-mini" @click="resetFilters">重置</button>
          </div>
          <p class="f-hint dim">导出为当前筛选条件的<b>前 200 条</b>（后端上限），不是全量。</p>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <h3>共 {{ total }} 条</h3>
          <span v-if="loading" class="card-subtitle">加载中…</span>
        </div>

        <div class="card-body" style="padding:0;">
          <div class="table-wrap">
            <table class="tbl">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>用户</th>
                  <th>动作</th>
                  <th>对象</th>
                  <th>结果</th>
                  <th>IP</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="r in rows" :key="r.id">
                  <td class="mono nowrap">{{ r.ts }}</td>
                  <td>
                    <span class="cell-user">{{ r.username || '—' }}</span>
                    <span v-if="!r.user_id" class="dim tag-anon">未认证</span>
                  </td>
                  <td>
                    <span class="act">{{ r.action_label }}</span>
                    <span class="act-key mono">{{ r.action }}</span>
                  </td>
                  <td>
                    <span v-if="r.target_id" class="mono">{{ r.target_id }}</span>
                    <span v-if="r.target_type" class="dim ttype">{{ r.target_type }}</span>
                    <span v-if="!r.target_id && !r.target_type" class="dim">—</span>
                  </td>
                  <td><span class="res" :class="'res-' + r.result">{{ r.result_label }}</span></td>
                  <td class="mono dim nowrap">{{ r.ip || '—' }}</td>
                  <td>
                    <button class="btn-mini" @click="openDetail(r)">详情</button>
                  </td>
                </tr>
                <tr v-if="!rows.length && !loading">
                  <td colspan="7" class="empty">没有符合条件的记录</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="pager">
          <button class="btn-mini" :disabled="page <= 1" @click="go(page - 1)">上一页</button>
          <span class="dim">第 {{ page }} / {{ totalPages }} 页</span>
          <button class="btn-mini" :disabled="page >= totalPages" @click="go(page + 1)">下一页</button>
        </div>
      </div>
    </template>

    <!-- 详情抽屉 -->
    <div v-if="detail" class="ovl" @click.self="detail = null">
      <div class="card dtl">
        <div class="card-header">
          <h3>{{ detail.action_label }}</h3>
          <button class="btn-mini" @click="detail = null"><AppIcon name="x" :size="13" inline /> 关闭</button>
        </div>
        <div class="card-body">
          <dl class="kv">
            <dt>时间</dt><dd class="mono">{{ detail.ts }}</dd>
            <dt>用户</dt><dd>{{ detail.username || '—' }}<span v-if="detail.user_id" class="dim"> （{{ detail.user_id }}）</span></dd>
            <dt>动作</dt><dd class="mono">{{ detail.action }}</dd>
            <dt>对象</dt><dd class="mono">{{ detail.target_type || '—' }} {{ detail.target_id || '' }}</dd>
            <dt>结果</dt><dd><span class="res" :class="'res-' + detail.result">{{ detail.result_label }}</span></dd>
            <dt>IP</dt><dd class="mono">{{ detail.ip || '—' }}</dd>
          </dl>
          <p class="dtl-label">详情（原始 JSON）</p>
          <pre class="json">{{ pretty(detail.detail) }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import PageHeader from '../PageHeader.vue'
import AppIcon from '../AppIcon.vue'
import { can } from '../../store/user.js'
import { fetchAuditLogs, fetchAuditActions, auditCsvUrl } from '../../api/admin.js'

const canView = computed(() => can('audit.view'))

const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = 50
const loading = ref(false)
const actions = ref([])
const detail = ref(null)

const f = reactive({ username: '', action: '', result: '', date_from: '', date_to: '' })

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

function params(p = page.value) {
  return { ...f, page: p, page_size: pageSize }
}

async function search() {
  loading.value = true
  page.value = 1
  try {
    const res = (await fetchAuditLogs(params(1))) || {}
    rows.value = res.items || []
    total.value = res.total || 0
    if (res.error) alert(`查询出错：${res.error}`)
  } catch (e) {
    alert(e.message || '查询失败')
  } finally {
    loading.value = false
  }
}

async function go(p) {
  if (p < 1 || p > totalPages.value) return
  loading.value = true
  try {
    const res = (await fetchAuditLogs(params(p))) || {}
    rows.value = res.items || []
    total.value = res.total || 0
    page.value = p
  } catch (e) {
    alert(e.message || '翻页失败')
  } finally {
    loading.value = false
  }
}

function resetFilters() {
  f.username = ''
  f.action = ''
  f.result = ''
  f.date_from = ''
  f.date_to = ''
  search()
}

function openDetail(r) {
  detail.value = r
}

function pretty(v) {
  try {
    return JSON.stringify(v || {}, null, 2)
  } catch (e) {
    return String(v)
  }
}

// 用 <a download> 直链下载：同源请求会带上会话 cookie。
// 会话过期时浏览器会打开一个 JSON 页面而不是下载 —— 这是可接受的失败方式，
// 比在前端拼 CSV 更不容易和后端导出逻辑走偏。
function exportCsv() {
  const a = document.createElement('a')
  a.href = auditCsvUrl({ ...f, page: 1, page_size: 200 })
  a.download = 'audit_logs.csv'
  document.body.appendChild(a)
  a.click()
  a.remove()
}

onMounted(async () => {
  if (!canView.value) return
  try {
    actions.value = (await fetchAuditActions()) || []
  } catch (e) {
    // 筛选项拿不到不该挡住日志本身
  }
  search()
})
</script>

<style scoped>
.filters { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 12px; }
.f-input {
  padding: 7px 10px; border: 1px solid var(--border); border-radius: 8px; font-size: 13px;
  background: rgba(255, 255, 255, 0.75); color: inherit;
}
.f-input:focus { outline: none; border-color: var(--primary); }
/* 固定宽度：靠内容撑开会让整条筛选栏参差不齐 */
.f-search { width: 190px; }
.f-sel { width: 168px; }
.f-range { display: inline-flex; align-items: center; gap: 6px; }
.f-date { width: 148px; }
.f-hint { font-size: 11px; margin: 12px 0 0; }
.dim { color: var(--text-secondary); }
.mono { font-family: Consolas, Monaco, monospace; font-size: 12px; }
.nowrap { white-space: nowrap; }

/* 不覆盖 .card-header —— 用全局那套（16px 20px 内边距 + 青色竖条） */

.table-wrap { overflow-x: auto; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; min-width: 780px; }
.tbl th {
  text-align: left; padding: 11px 14px; font-size: 12px; color: var(--text-secondary);
  border-bottom: 1px solid var(--border); background: rgba(13, 30, 52, 0.4); white-space: nowrap;
}
.tbl td { padding: 11px 14px; border-bottom: 1px solid rgba(169, 205, 226, 0.12); vertical-align: middle; }
/* 首末列与卡片标题对齐（卡片内边距是 20px） */
.tbl th:first-child, .tbl td:first-child { padding-left: 20px; }
.tbl th:last-child, .tbl td:last-child { padding-right: 20px; }
.tbl tr:hover td { background: rgba(24, 184, 166, 0.05); }
.empty { text-align: center; color: var(--text-secondary); padding: 44px 0; }

.cell-user { font-weight: 600; }
.tag-anon { font-size: 10px; margin-left: 6px; }
.act { display: block; }
.act-key { color: var(--text-secondary); font-size: 10px; }
.ttype { font-size: 10px; margin-left: 6px; }

.res { display: inline-block; font-size: 11px; font-weight: 600; padding: 1px 10px; border-radius: 999px; }
.res-ok { background: rgba(24, 184, 166, 0.15); color: #2dd4bf; border: 1px solid rgba(24,184,166,0.4); }
.res-fail { background: rgba(244, 63, 94, 0.15); color: #fb7185; border: 1px solid rgba(244,63,94,0.4); }
.res-denied { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245,158,11,0.4); }

.pager {
  display: flex; align-items: center; gap: 12px; justify-content: center;
  padding: 14px 20px 18px; font-size: 12px;
}

.btn-go {
  display: inline-flex; align-items: center; gap: 6px;
  background: var(--gradient-primary); color: #fff; border: none; border-radius: 999px;
  padding: 6px 16px; font-size: 13px; cursor: pointer;
}
.btn-go:hover { opacity: 0.9; }
.btn-ghost {
  display: inline-flex; align-items: center; gap: 6px; background: transparent;
  border: 1px solid var(--border); color: inherit; border-radius: 999px;
  padding: 6px 14px; font-size: 13px; cursor: pointer; transition: all var(--transition);
}
.btn-ghost:hover:not(:disabled) { border-color: var(--primary); color: var(--primary); }
.btn-ghost:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-mini {
  padding: 4px 12px; border-radius: 999px; font-size: 11px; cursor: pointer;
  border: 1px solid rgba(169,205,226,0.5); background: transparent; color: var(--text-secondary);
  transition: all 0.15s;
}
.btn-mini:hover:not(:disabled) { border-color: var(--primary); color: var(--primary); }
.btn-mini:disabled { opacity: 0.4; cursor: not-allowed; }

.forbid-card { text-align: center; padding: 60px 20px; color: var(--text-secondary); }
.forbid-icon { display: block; margin-bottom: 10px; }

.ovl {
  position: fixed; inset: 0; background: rgba(4, 15, 31, 0.55); z-index: 2000;
  display: flex; align-items: center; justify-content: center; padding: 20px;
}
.dtl { max-width: 640px; width: 100%; max-height: 85vh; overflow-y: auto; }
.kv { display: grid; grid-template-columns: 64px 1fr; gap: 6px 14px; margin: 0 0 16px; font-size: 13px; }
.kv dt { color: var(--text-secondary); }
.kv dd { margin: 0; word-break: break-all; }
.dtl-label { font-size: 12px; color: var(--text-secondary); margin: 0 0 6px; }
.json {
  background: rgba(13, 30, 52, 0.06); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px; font-size: 12px; font-family: Consolas, Monaco, monospace;
  white-space: pre-wrap; word-break: break-all; margin: 0; max-height: 320px; overflow: auto;
}

@media (max-width: 768px) {
  /* 移动端把筛选控件拉满一行，避免一行挤三个控件 */
  .f-search, .f-sel { width: 100%; }
  .f-date { width: 132px; }
}
</style>
