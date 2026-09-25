<template>
  <Teleport to="body"><!-- 挂到 body，避免被 .topnav 的 backdrop-filter 变成固定定位的 containing block -->
  <div class="profile-overlay" @click.self="close">
    <div class="profile-card" role="dialog" aria-label="个人信息">
      <button class="profile-close" title="关闭" @click="close"><AppIcon name="x" :size="16" /></button>

      <div class="profile-head">
        <div class="profile-avatar">
          <img v-if="avatarIsImg" :src="user.avatar" alt="avatar" />
          <AppIcon v-else :name="user.avatar" :size="30" />
        </div>
        <div class="profile-id">
          <strong class="profile-name">{{ user.name || user.username || '未登录' }}</strong>
          <span class="profile-role" :class="roleClass">
            {{ user.role || '用户' }}
          </span>
        </div>
      </div>

      <div class="profile-meta">
        <div class="meta-row"><span>账号 ID</span><b class="mono">{{ user.id || '—' }}</b></div>
        <div class="meta-row"><span>登录名</span><b class="mono">{{ user.username || '—' }}</b></div>
        <div class="meta-row"><span>注册时间</span><b>{{ user.createdAt || '—' }}</b></div>
      </div>

      <!-- 开发者模式入口：所有成员可见 -->
      <div class="profile-links">
        <a class="link-btn" @click="go('/dev')"><AppIcon name="flask" inline /> 开发者模式</a>
      </div>

      <!-- 管理入口：超级管理员→用户管理；管理员/超级管理员→组织管理 -->
      <div v-if="isAdmin()" class="profile-links">
        <a v-if="isSuper()" class="link-btn" @click="go('/admin/users')"><AppIcon name="shield" inline /> 用户管理</a>
        <a class="link-btn" @click="go('/admin/orgs')"><AppIcon name="building" inline /> 组织管理</a>
      </div>

      <!-- 账户编辑 -->
      <div class="account-editor">
        <div class="edit-block">
          <div class="edit-head" @click="openSections.nickname = !openSections.nickname">
            <span>显示昵称</span><span class="edit-arrow">{{ openSections.nickname ? '▴' : '▾' }}</span>
          </div>
          <div v-if="openSections.nickname" class="edit-body">
            <input v-model="nameInput" class="field" placeholder="输入新的显示昵称" maxlength="32" @keyup.enter="saveName" />
            <button class="btn" :disabled="saving.name" @click="saveName">{{ saving.name ? '保存中…' : '保存' }}</button>
          </div>
        </div>

        <div class="edit-block">
          <div class="edit-head" @click="openSections.avatar = !openSections.avatar">
            <span>修改头像</span><span class="edit-arrow">{{ openSections.avatar ? '▴' : '▾' }}</span>
          </div>
          <div v-if="openSections.avatar" class="edit-body">
            <div class="avatar-row">
              <img v-if="avatarIsImg" :src="user.avatar" class="avatar-preview" alt="avatar" />
              <div v-else class="avatar-preview avatar-emoji"><AppIcon :name="user.avatar" :size="28" /></div>
              <div class="avatar-actions">
                <input ref="fileInput" type="file" accept="image/png,image/jpeg,image/gif,image/webp" style="display:none" @change="onFile" />
                <button class="btn" :disabled="saving.avatar" @click="fileInput.click()">{{ saving.avatar ? '上传中…' : '选择图片' }}</button>
                <span class="hint">支持 png/jpg/gif/webp，≤2MB</span>
              </div>
            </div>
          </div>
        </div>

        <div class="edit-block">
          <div class="edit-head" @click="openSections.password = !openSections.password">
            <span>修改密码</span><span class="edit-arrow">{{ openSections.password ? '▴' : '▾' }}</span>
          </div>
          <div v-if="openSections.password" class="edit-body pwd-body">
            <input v-model="pwd.old" type="password" class="field" placeholder="旧密码" @keyup.enter="savePassword" />
            <input v-model="pwd.new1" type="password" class="field" placeholder="新密码（至少 6 位）" @keyup.enter="savePassword" />
            <input v-model="pwd.new2" type="password" class="field" placeholder="确认新密码" @keyup.enter="savePassword" />
            <button class="btn" :disabled="saving.password" @click="savePassword">{{ saving.password ? '提交中…' : '确认修改' }}</button>
          </div>
        </div>
      </div>

      <transition name="fade">
        <div v-if="msg.text" class="msg" :class="'msg-' + msg.type">{{ msg.text }}</div>
      </transition>

      <div class="profile-stats">
        <div class="stat"><b>{{ stats.ships }}</b><span>船舶数</span></div>
        <div class="stat"><b>{{ stats.violations }}</b><span>违规数</span></div>
        <div class="stat"><b>{{ fmtNum(stats.aisPoints) }}</b><span>AIS 点</span></div>
      </div>

      <button class="logout-btn" @click="doLogout">退出登录</button>
    </div>
  </div>
  </Teleport>
</template>

<script setup>
import { ref, computed, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { user, loadUser, isAdmin, isSuper } from '../store/user.js'
import { fetchUserStats, fetchDashboardStats, updateUserProfile, changePassword, uploadAvatar } from '../api/user.js'
import { isImageUrl } from '../store/ai.js'
import AppIcon from './AppIcon.vue'

const router = useRouter()
const emit = defineEmits(['close'])
const stats = ref({ ships: 0, violations: 0, aisPoints: 0 })
const fileInput = ref(null)

const avatarIsImg = computed(() => isImageUrl(user.avatar))
const roleClass = computed(() =>
  user.role === '超级管理员' ? 'role-super' : user.role === '管理员' ? 'role-admin' : 'role-user'
)

// 编辑区折叠状态
const openSections = reactive({ nickname: false, avatar: false, password: false })
const saving = reactive({ name: false, avatar: false, password: false })

const nameInput = ref('')
const pwd = reactive({ old: '', new1: '', new2: '' })
const msg = ref({ type: '', text: '' })

let msgTimer = null
function flash(type, text) {
  msg.value = { type, text }
  if (msgTimer) clearTimeout(msgTimer)
  msgTimer = setTimeout(() => (msg.value = { type: '', text: '' }), 3000)
}

function fmtNum(n) {
  const v = Number(n) || 0
  if (v >= 10000) return (v / 10000).toFixed(1) + '万'
  return String(v)
}

async function loadStats() {
  try {
    const [us, ds] = await Promise.all([fetchUserStats(), fetchDashboardStats()])
    stats.value = {
      ships: us?.ships ?? 0,
      violations: ds?.violationShips ?? 0,
      aisPoints: us?.aisPoints ?? 0
    }
  } catch (e) {
    stats.value = { ships: 0, violations: 0, aisPoints: 0 }
  }
}

function close() {
  emit('close')
}

function go(path) {
  close()
  router.push(path)
}

async function saveName() {
  const name = nameInput.value.trim()
  if (!name) return flash('error', '昵称不能为空')
  saving.name = true
  try {
    await updateUserProfile(name)
    await loadUser()
    nameInput.value = ''
    flash('ok', '昵称已更新')
  } catch (e) {
    flash('error', e.message || '更新失败')
  } finally {
    saving.name = false
  }
}

function onFile(e) {
  const file = e.target.files && e.target.files[0]
  if (!file) return
  const okExt = /\.(png|jpe?g|gif|webp)$/i.test(file.name)
  if (!okExt) return flash('error', '仅支持 png/jpg/jpeg/gif/webp 图片')
  if (file.size > 2 * 1024 * 1024) return flash('error', '图片不能超过 2MB')
  saving.avatar = true
  uploadAvatar(file)
    .then(async data => {
      if (data?.avatar) {
        user.avatar = data.avatar
        await loadUser()
        flash('ok', '头像已更新')
      } else {
        flash('error', '上传失败')
      }
    })
    .catch(err => flash('error', err.message || '上传失败'))
    .finally(() => {
      saving.avatar = false
      if (fileInput.value) fileInput.value.value = ''
    })
}

async function savePassword() {
  const { old, new1, new2 } = pwd
  if (!old) return flash('error', '请输入旧密码')
  if (!new1 || new1.length < 6) return flash('error', '新密码至少 6 位')
  if (new1 !== new2) return flash('error', '两次新密码不一致')
  saving.password = true
  try {
    await changePassword(old, new1)
    pwd.old = ''
    pwd.new1 = ''
    pwd.new2 = ''
    flash('ok', '密码已修改，下次登录请使用新密码')
  } catch (e) {
    flash('error', e.message || '修改失败')
  } finally {
    saving.password = false
  }
}

async function doLogout() {
  try {
    await fetch('/api/v1/auth/logout', { method: 'POST' })
  } catch (e) {}
  window.location.href = '/'
}

onMounted(loadStats)
</script>

<style scoped>
.profile-overlay {
  position: fixed; inset: 0; z-index: 11000;
  background: rgba(4, 15, 31, 0.62);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
  display: flex; align-items: center; justify-content: center;
  padding: 20px;
}
.profile-card {
  position: relative;
  width: 360px; max-width: 92vw;
  box-sizing: border-box;
  max-height: calc(100vh - 40px); overflow-y: auto;
  background: linear-gradient(165deg, #0b2038 0%, #0a1830 100%);
  border: 1px solid rgba(118, 200, 255, 0.22);
  border-radius: 18px;
  box-shadow: 0 24px 70px rgba(2, 15, 34, 0.6);
  padding: 26px 24px 22px;
  color: #e8f0fe;
  animation: cardIn 0.28s ease;
}
.profile-card::-webkit-scrollbar { width: 4px; }
.profile-card::-webkit-scrollbar-thumb { background: rgba(118,200,255,0.3); border-radius: 2px; }
@keyframes cardIn {
  from { opacity: 0; transform: translateY(16px) scale(0.96); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}
.profile-close {
  position: absolute; top: 12px; right: 12px;
  width: 28px; height: 28px; border-radius: 50%;
  border: 1px solid rgba(255,255,255,0.18);
  background: rgba(255,255,255,0.06); color: rgba(255,255,255,0.7);
  font-size: 13px; cursor: pointer; line-height: 1;
  transition: all 0.15s;
}
.profile-close:hover { background: rgba(255,255,255,0.16); color: #fff; }

.profile-head { display: flex; align-items: center; gap: 14px; margin-bottom: 16px; }
.profile-avatar {
  width: 64px; height: 64px; border-radius: 50%;
  background: var(--gradient-teal);
  box-shadow: 0 4px 16px rgba(13, 148, 136, 0.45);
  display: flex; align-items: center; justify-content: center;
  font-size: 30px; color: #fff; overflow: hidden; flex-shrink: 0;
}
.profile-avatar img { width: 100%; height: 100%; object-fit: cover; }
.profile-id { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.profile-name { font-size: 18px; font-weight: 700; letter-spacing: 0.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.profile-role {
  align-self: flex-start;
  font-size: 11px; font-weight: 600; padding: 2px 10px; border-radius: 999px;
}
.role-super { background: rgba(225, 29, 72, 0.2); color: #fb7185; border: 1px solid rgba(244,63,94,0.5); }
.role-admin { background: rgba(245, 158, 11, 0.18); color: #fbbf24; border: 1px solid rgba(245,158,11,0.4); }
.role-user { background: rgba(26, 115, 232, 0.18); color: #8ab4ff; border: 1px solid rgba(26,115,232,0.4); }

.profile-meta { margin-bottom: 14px; display: flex; flex-direction: column; gap: 7px; }
.meta-row {
  display: flex; justify-content: space-between; gap: 12px;
  font-size: 12px; color: rgba(185,198,221,0.75);
  background: rgba(255,255,255,0.05);
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 9px; padding: 8px 12px;
}
.meta-row b { color: #e8f0fe; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.meta-row .mono { font-family: Consolas, Monaco, monospace; font-size: 11px; }

.profile-links {
  display: flex; gap: 8px; margin-bottom: 14px;
}
.link-btn {
  flex: 1; text-align: center; padding: 8px 0; border-radius: 9px;
  background: rgba(12, 140, 233, 0.14);
  border: 1px solid rgba(12, 140, 233, 0.4);
  color: #8ab4ff; font-size: 12px; cursor: pointer; text-decoration: none;
  transition: all 0.15s;
}
.link-btn:hover { background: rgba(12, 140, 233, 0.28); color: #fff; }

.account-editor {
  border: 1px solid rgba(255,255,255,0.09);
  border-radius: 12px; overflow: hidden; margin-bottom: 12px;
}
.edit-block + .edit-block { border-top: 1px solid rgba(255,255,255,0.09); }
.edit-head {
  display: flex; justify-content: space-between; align-items: center;
  padding: 11px 14px; font-size: 13px; color: rgba(232,240,254,0.9);
  cursor: pointer; transition: background 0.15s;
}
.edit-head:hover { background: rgba(255,255,255,0.05); }
.edit-arrow { font-size: 11px; color: rgba(185,198,221,0.6); }
.edit-body {
  padding: 12px 14px 14px; display: flex; flex-direction: column; gap: 9px;
  background: rgba(255,255,255,0.03);
}
.field {
  width: 100%; box-sizing: border-box;
  padding: 8px 12px; border-radius: 8px;
  border: 1px solid rgba(255,255,255,0.16);
  background: rgba(2, 15, 34, 0.6); color: #e8f0fe; font-size: 13px;
  outline: none; transition: border 0.15s;
}
.field:focus { border-color: rgba(24, 184, 166, 0.7); }
.btn {
  padding: 8px 0; border: none; border-radius: 8px;
  background: linear-gradient(135deg, #0c8ce9, #18b8a6); color: #fff;
  font-size: 13px; font-weight: 600; cursor: pointer; transition: opacity 0.15s, transform 0.15s;
}
.btn:disabled { opacity: 0.55; cursor: not-allowed; }
.btn:hover:not(:disabled) { transform: translateY(-1px); opacity: 0.92; }
.avatar-row { display: flex; align-items: center; gap: 12px; }
.avatar-preview {
  width: 52px; height: 52px; border-radius: 50%; flex-shrink: 0;
  background: var(--gradient-teal);
  display: flex; align-items: center; justify-content: center; overflow: hidden;
}
.avatar-preview img { width: 100%; height: 100%; object-fit: cover; }
.avatar-emoji { font-size: 24px; color: #fff; }
.avatar-actions { display: flex; flex-direction: column; gap: 5px; flex: 1; }
.hint { font-size: 11px; color: rgba(157,180,217,0.6); }

.msg {
  margin-bottom: 12px; padding: 8px 12px; border-radius: 8px; font-size: 12px;
}
.msg-ok { background: rgba(24, 184, 166, 0.16); color: #7ff0e2; border: 1px solid rgba(24,184,166,0.4); }
.msg-error { background: rgba(244, 63, 94, 0.14); color: #fb7185; border: 1px solid rgba(244,63,94,0.4); }
.fade-enter-active, .fade-leave-active { transition: opacity 0.2s; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

.profile-stats {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px;
  margin-bottom: 18px;
}
.stat {
  background: rgba(255,255,255,0.05);
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 10px; padding: 10px 4px; text-align: center;
}
.stat b { display: block; font-size: 18px; color: #7ff0e2; font-weight: 700; }
.stat span { display: block; font-size: 11px; color: rgba(157,180,217,0.7); margin-top: 3px; }

.logout-btn {
  width: 100%; padding: 10px 0; border: none; border-radius: 10px;
  background: linear-gradient(135deg, #e11d48, #f43f5e); color: #fff;
  font-size: 13px; font-weight: 600; letter-spacing: 2px; cursor: pointer;
  transition: transform 0.15s, opacity 0.15s;
}
.logout-btn:hover { transform: translateY(-1px); opacity: 0.92; }
</style>
