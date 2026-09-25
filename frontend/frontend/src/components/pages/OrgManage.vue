<template>
  <div class="page">
    <PageHeader icon="building" title="组织管理" subtitle="管理员创建并维护组织，组织成员共享数据可见性" />

    <!-- 无权限提示 -->
    <div v-if="!canView" class="card forbid-card">
      <span class="forbid-icon"><AppIcon name="ban" :size="40" style="color:var(--text-secondary);" /></span>
      <p>仅管理员或超级管理员可访问组织管理</p>
    </div>

    <template v-else>
      <!-- 创建组织 -->
      <div class="card create-card">
        <div class="create-row">
          <input v-model="newName" class="create-input" placeholder="输入组织名称，回车或点击创建" maxlength="40" @keyup.enter="create" />
          <button class="btn-primary" :disabled="creating" @click="create"><AppIcon name="plus" inline /> {{ creating ? '创建中…' : '创建组织' }}</button>
        </div>
      </div>

      <!-- 组织列表 -->
      <div v-if="orgs.length === 0" class="card empty-card">
        <p>还没有组织。管理员可创建组织，并把普通用户加为成员。</p>
      </div>

      <div v-else class="org-list">
        <div v-for="org in orgs" :key="org.id" class="card org-card">
          <div class="org-head">
            <div class="org-info">
              <span class="org-name">{{ org.name }}</span>
              <span class="org-sub">
                负责人：{{ org.admin_name }}
                <span v-if="isSuper() && org.admin_id !== user.id" class="tag-super">超级管理</span>
              </span>
            </div>
            <div class="org-actions">
              <span class="org-count">{{ org.member_count }} 名成员</span>
              <button class="btn-mini btn-ghost" @click="toggleDetail(org)">
                {{ isOpen(org.id) ? '收起' : '成员管理' }}
              </button>
              <button class="btn-mini btn-danger" @click="remove(org)">删除</button>
            </div>
          </div>

          <div v-if="isOpen(org.id)" class="org-detail">
            <div class="add-member">
              <input
                v-model="addInputs[org.id]"
                class="create-input"
                placeholder="输入用户名添加普通成员"
                @keyup.enter="addMember(org)"
              />
              <button class="btn-mini btn-ok" :disabled="addingId === org.id" @click="addMember(org)">
                {{ addingId === org.id ? '添加中…' : '添加' }}
              </button>
            </div>

            <div v-if="membersMap[org.id] && membersMap[org.id].length" class="member-list">
              <div v-for="m in membersMap[org.id]" :key="m.id" class="member-row">
                <span class="cell-avatar">
                  <img v-if="isImg(m.avatar)" :src="m.avatar" alt="" />
                  <AppIcon v-else :name="m.avatar" :size="14" />
                </span>
                <span class="member-name">{{ m.name }}</span>
                <span class="member-user mono">{{ m.username }}</span>
                <span class="member-role dim">{{ m.role }}</span>
                <button class="btn-mini btn-danger" @click="removeMember(org, m)">移除</button>
              </div>
            </div>
            <p v-else class="dim empty-tip">暂无成员</p>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import PageHeader from '../PageHeader.vue'
import { user, isAdmin, isSuper } from '../../store/user.js'
import { fetchOrgs, createOrg, fetchOrgDetail, addOrgMember, removeOrgMember, deleteOrg } from '../../api/user.js'
import { isImageUrl } from '../../store/ai.js'
import AppIcon from '../AppIcon.vue'

const orgs = ref([])
const newName = ref('')
const creating = ref(false)
const addingId = ref(null)
const openIds = ref(new Set())
const membersMap = reactive({})
const addInputs = reactive({})

const canView = computed(() => isAdmin())

function isImg(v) {
  return isImageUrl(v)
}

function isOpen(id) {
  return openIds.value.has(id)
}

async function load() {
  try {
    orgs.value = (await fetchOrgs()) || []
  } catch (e) {
    alert(e.message || '加载失败')
  }
}

async function create() {
  const name = newName.value.trim()
  if (!name) return alert('请输入组织名称')
  creating.value = true
  try {
    await createOrg(name)
    newName.value = ''
    await load()
  } catch (e) {
    alert(e.message || '创建失败')
  } finally {
    creating.value = false
  }
}

async function toggleDetail(org) {
  if (openIds.value.has(org.id)) {
    openIds.value.delete(org.id)
    return
  }
  openIds.value.add(org.id)
  try {
    const detail = await fetchOrgDetail(org.id)
    membersMap[org.id] = detail?.members || []
    org.member_count = (detail?.members || []).length
  } catch (e) {
    alert(e.message || '加载成员失败')
  }
}

async function addMember(org) {
  const username = (addInputs[org.id] || '').trim()
  if (!username) return alert('请输入用户名')
  addingId.value = org.id
  try {
    await addOrgMember(org.id, username)
    addInputs[org.id] = ''
    const detail = await fetchOrgDetail(org.id)
    membersMap[org.id] = detail?.members || []
    org.member_count = (detail?.members || []).length
  } catch (e) {
    alert(e.message || '添加失败')
  } finally {
    addingId.value = null
  }
}

async function removeMember(org, m) {
  if (!confirm(`确定将「${m.name || m.username}」移出组织「${org.name}」？`)) return
  try {
    await removeOrgMember(org.id, m.id)
    membersMap[org.id] = membersMap[org.id].filter(x => x.id !== m.id)
    org.member_count = membersMap[org.id].length
  } catch (e) {
    alert(e.message || '移除失败')
  }
}

async function remove(org) {
  if (!confirm(`确定删除组织「${org.name}」？其成员关系将一并删除。`)) return
  try {
    await deleteOrg(org.id)
    openIds.value.delete(org.id)
    delete membersMap[org.id]
    orgs.value = orgs.value.filter(o => o.id !== org.id)
  } catch (e) {
    alert(e.message || '删除失败')
  }
}

onMounted(load)
</script>

<style scoped>
.create-card { margin-bottom: 24px; padding: 20px 24px; }
.create-row { display: flex; gap: 12px; }
.create-input {
  flex: 1; padding: 11px 14px; border-radius: 9px;
  border: 1px solid var(--border); background: #fff; color: var(--text);
  font-size: 14px; outline: none; transition: border 0.15s;
}
.create-input:focus { border-color: var(--primary); }
.btn-primary {
  background: var(--gradient-primary); color: #fff; border: none; border-radius: 9px;
  padding: 11px 24px; font-size: 13px; font-weight: 600; cursor: pointer;
  transition: all var(--transition); white-space: nowrap;
}
.btn-primary:hover:not(:disabled) { transform: translateY(-1px); opacity: 0.92; }
.btn-primary:disabled { opacity: 0.6; cursor: not-allowed; }

.org-list { display: flex; flex-direction: column; gap: 16px; }
.org-card { margin-bottom: 0; padding: 20px 24px; }
.org-head {
  display: flex; justify-content: space-between; align-items: center; gap: 14px; flex-wrap: wrap;
}
.org-info { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.org-name { font-size: 16px; font-weight: 700; }
.org-sub { font-size: 12px; color: var(--text-secondary); display: flex; align-items: center; gap: 10px; }
.tag-super {
  font-size: 10px; padding: 2px 10px; border-radius: 999px;
  background: rgba(244, 63, 94, 0.15); color: #fb7185; border: 1px solid rgba(244,63,94,0.4);
}
.org-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.org-count { font-size: 12px; color: var(--text-secondary); white-space: nowrap; }

.btn-mini {
  padding: 7px 14px; border: none; border-radius: 999px; font-size: 12px; cursor: pointer;
  transition: all 0.15s;
}
.btn-ghost { background: rgba(12, 140, 233, 0.12); color: #5caef0; border: 1px solid rgba(12,140,233,0.35); }
.btn-ghost:hover { background: rgba(12, 140, 233, 0.25); }
.btn-danger { background: rgba(244, 63, 94, 0.12); color: #fb7185; border: 1px solid rgba(244,63,94,0.35); }
.btn-danger:hover { background: rgba(244, 63, 94, 0.25); }
.btn-ok { background: rgba(24, 184, 166, 0.15); color: #2ec4a0; border: 1px solid rgba(24,184,166,0.4); }
.btn-ok:hover { background: rgba(24, 184, 166, 0.3); }

.org-detail {
  margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--border);
}
.add-member { display: flex; gap: 12px; margin-bottom: 14px; }
.add-member .create-input { padding: 10px 14px; font-size: 13px; }
.add-member .btn-mini { padding: 10px 20px; }

.member-list { display: flex; flex-direction: column; gap: 8px; }
.member-row {
  display: flex; align-items: center; gap: 12px;
  padding: 10px 14px; border: 1px solid rgba(169, 205, 226, 0.15);
  border-radius: 10px; background: rgba(255, 255, 255, 0.02);
}
.cell-avatar {
  width: 32px; height: 32px; border-radius: 50%; flex-shrink: 0;
  background: var(--gradient-teal); display: flex; align-items: center; justify-content: center;
  font-size: 14px; overflow: hidden;
}
.cell-avatar img { width: 100%; height: 100%; object-fit: cover; }
.member-name { font-weight: 600; font-size: 14px; }
.member-user { font-size: 12px; color: var(--text-secondary); }
.member-role { font-size: 12px; margin-left: auto; }
.member-row .btn-mini { margin-left: 8px; }
.empty-tip { font-size: 13px; }
.dim { color: var(--text-secondary); }
.mono { font-family: Consolas, Monaco, monospace; }

.empty-card { text-align: center; padding: 40px 20px; color: var(--text-secondary); }
.forbid-card { text-align: center; padding: 60px 20px; color: var(--text-secondary); }
.forbid-icon { font-size: 40px; display: block; margin-bottom: 10px; }

@media (max-width: 768px) {
  .create-card, .org-card { padding: 16px 16px; }
  .create-row { flex-direction: column; }
  .btn-primary { width: 100%; }
  .org-actions { flex-wrap: wrap; }
  .add-member { flex-direction: column; }
  .add-member .btn-mini { width: 100%; }
}
</style>
