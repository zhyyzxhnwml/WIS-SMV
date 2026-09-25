<template>
  <div class="page">
    <PageHeader icon="shield" title="用户管理" subtitle="超级管理员专属：查看所有用户并设置角色" />

    <!-- 无权限提示 -->
    <div v-if="!canView" class="card forbid-card">
      <span class="forbid-icon"><AppIcon name="ban" :size="40" style="color:var(--text-secondary);" /></span>
      <p>仅超级管理员可访问用户管理</p>
    </div>

    <template v-else>
      <!-- 统计 -->
      <div class="stats-grid">
        <div class="stat-card">
          <span class="stat-value">{{ users.length }}</span>
          <span class="stat-label">用户总数</span>
        </div>
        <div class="stat-card">
          <span class="stat-value">{{ countRole('超级管理员') }}</span>
          <span class="stat-label">超级管理员</span>
        </div>
        <div class="stat-card">
          <span class="stat-value">{{ countRole('管理员') }}</span>
          <span class="stat-label">管理员</span>
        </div>
        <div class="stat-card">
          <span class="stat-value">{{ countRole('用户') }}</span>
          <span class="stat-label">普通用户</span>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <h3>全部用户</h3>
          <button class="btn-refresh" @click="load">刷新</button>
        </div>
        <div class="table-wrap">
          <table class="tbl">
            <thead>
              <tr>
                <th>用户</th>
                <th>登录名</th>
                <th>角色</th>
                <th>所属组织</th>
                <th>注册时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="u in users" :key="u.id">
                <td>
                  <span class="cell-user">
                    <span class="cell-avatar">
                      <img v-if="isImg(u.avatar)" :src="u.avatar" alt="" />
                      <AppIcon v-else :name="u.avatar" :size="14" />
                    </span>
                    <span class="cell-name">{{ u.name }}</span>
                  </span>
                </td>
                <td class="mono">{{ u.username }}</td>
                <td><span class="role-tag" :class="'role-' + roleKey(u.role)">{{ u.role }}</span></td>
                <td>
                  <span v-if="u.orgs && u.orgs.length" class="org-chips">
                    <span v-for="o in u.orgs" :key="o.org_id" class="org-chip">{{ o.org_name }}</span>
                  </span>
                  <span v-else class="dim">—</span>
                </td>
                <td class="dim">{{ shortDate(u.created_at) }}</td>
                <td>
                  <template v-if="u.id === user.id || u.role === '超级管理员'">
                    <span class="dim">—</span>
                  </template>
                  <button
                    v-else-if="u.role === '用户'"
                    class="btn-mini btn-promote"
                    @click="setRole(u, '管理员')"
                  >设为管理员</button>
                  <button
                    v-else
                    class="btn-mini btn-demote"
                    @click="setRole(u, '用户')"
                  >设为用户</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import PageHeader from '../PageHeader.vue'
import { user, isSuper } from '../../store/user.js'
import { fetchAdminUsers, setUserRole } from '../../api/user.js'
import { isImageUrl } from '../../store/ai.js'
import AppIcon from '../AppIcon.vue'

const users = ref([])
const loading = ref(false)
const canView = computed(() => isSuper())

function roleKey(r) {
  if (r === '超级管理员') return 'super'
  if (r === '管理员') return 'admin'
  return 'user'
}

function countRole(r) {
  return users.value.filter(u => u.role === r).length
}

function isImg(v) {
  return isImageUrl(v)
}

function shortDate(s) {
  return (s || '').slice(0, 10)
}

async function load() {
  loading.value = true
  try {
    users.value = (await fetchAdminUsers()) || []
  } catch (e) {
    alert(e.message || '加载失败')
  } finally {
    loading.value = false
  }
}

async function setRole(u, role) {
  const tip = role === '管理员'
    ? `确定将「${u.name || u.username}」设为管理员？`
    : `确定将「${u.name || u.username}」降为普通用户？\n若其名下有组织，组织将交接给你，且本人保留为普通成员。`
  if (!confirm(tip)) return
  try {
    await setUserRole(u.id, role)
    u.role = role
  } catch (e) {
    alert(e.message || '操作失败')
  }
}

onMounted(load)
</script>

<style scoped>
.stats-grid {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 20px;
}
.stat-card {
  background: var(--gradient-card); border: 1px solid rgba(169, 205, 226, 0.4);
  border-radius: var(--radius); padding: 18px 20px; box-shadow: var(--shadow);
  display: flex; flex-direction: column; gap: 4px;
}
.stat-value { font-size: 26px; font-weight: 700; color: var(--primary); }
.stat-label { font-size: 12px; color: var(--text-secondary); }

.card-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;
}
.card-header h3 { margin: 0; font-size: 16px; }

.table-wrap { overflow-x: auto; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; min-width: 720px; }
.tbl th {
  text-align: left; padding: 10px 12px; font-size: 12px; color: var(--text-secondary);
  border-bottom: 1px solid var(--border); background: rgba(13, 30, 52, 0.4); white-space: nowrap;
}
.tbl td { padding: 10px 12px; border-bottom: 1px solid rgba(169, 205, 226, 0.12); vertical-align: middle; }
.tbl tr:hover td { background: rgba(24, 184, 166, 0.05); }

.cell-user { display: flex; align-items: center; gap: 10px; }
.cell-avatar {
  width: 30px; height: 30px; border-radius: 50%; flex-shrink: 0;
  background: var(--gradient-teal); display: flex; align-items: center; justify-content: center;
  font-size: 14px; overflow: hidden;
}
.cell-avatar img { width: 100%; height: 100%; object-fit: cover; }
.cell-name { font-weight: 600; white-space: nowrap; }
.mono { font-family: Consolas, Monaco, monospace; font-size: 12px; }
.dim { color: var(--text-secondary); }

.role-tag {
  display: inline-block; font-size: 11px; font-weight: 600; padding: 2px 10px; border-radius: 999px;
}
.role-super { background: rgba(244, 63, 94, 0.15); color: #fb7185; border: 1px solid rgba(244,63,94,0.4); }
.role-admin { background: rgba(245, 158, 11, 0.14); color: #fbbf24; border: 1px solid rgba(245,158,11,0.35); }
.role-user { background: rgba(26, 115, 232, 0.13); color: #8ab4ff; border: 1px solid rgba(26,115,232,0.35); }

.org-chips { display: flex; flex-wrap: wrap; gap: 4px; }
.org-chip {
  font-size: 11px; padding: 1px 8px; border-radius: 999px;
  background: rgba(12, 140, 233, 0.12); color: #6db6ff; border: 1px solid rgba(12,140,233,0.3);
}

.btn-mini {
  padding: 4px 12px; border: none; border-radius: 999px; font-size: 12px; cursor: pointer;
  transition: all 0.15s;
}
.btn-promote { background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245,158,11,0.4); }
.btn-promote:hover { background: rgba(245, 158, 11, 0.35); }
.btn-demote { background: rgba(244, 63, 94, 0.15); color: #fb7185; border: 1px solid rgba(244,63,94,0.4); }
.btn-demote:hover { background: rgba(244, 63, 94, 0.3); }

.btn-refresh {
  background: var(--gradient-primary); color: #fff; border: none; border-radius: 999px;
  padding: 5px 16px; font-size: 12px; cursor: pointer; transition: all var(--transition);
}
.btn-refresh:hover { opacity: 0.9; transform: translateY(-1px); }

.forbid-card {
  text-align: center; padding: 60px 20px; color: var(--text-secondary);
}
.forbid-icon { font-size: 40px; display: block; margin-bottom: 10px; }

@media (max-width: 768px) {
  .stats-grid { grid-template-columns: repeat(2, 1fr); gap: 10px; }
}
</style>
