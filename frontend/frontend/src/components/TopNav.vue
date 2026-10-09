<template>
  <nav class="topnav">
    <div class="nav-left" @click="$router.push('/dashboard')">
      <img v-if="aiConfig.logoUrl" :src="aiConfig.logoUrl" class="nav-logo-img" alt="logo">
      <span v-else class="nav-logo-icon"><AppIcon name="anchor" :size="24" /></span>
      <div class="nav-logo-text">
        <span class="nav-logo-title">智海巡航</span>
        <span class="nav-logo-subtitle">港口船舶违规行为智能识别平台</span>
      </div>
    </div>

    <div class="nav-menu">
      <router-link
        v-for="item in navItems"
        :key="item.path"
        :to="item.path"
        class="nav-item"
        :class="{ active: isActive(item.path) }"
      >
        <span class="nav-icon"><AppIcon :name="item.icon" :size="16" /></span>
        <span class="nav-label">{{ item.label }}</span>
      </router-link>

      <div
        v-for="group in navGroups"
        :key="group.label"
        class="nav-group"
      >
        <span class="nav-item nav-parent" :class="{ active: isGroupActive(group) }">
          <span class="nav-icon"><AppIcon :name="group.icon" :size="16" /></span>
          <span class="nav-label">{{ group.label }}</span>
          <span class="nav-arrow">▾</span>
        </span>
        <div class="nav-dropdown">
          <div class="nav-dropdown-menu">
            <router-link
              v-for="child in group.children"
              :key="child.path"
              :to="child.path"
              class="dropdown-item"
              :class="{ active: route.path === child.path }"
            >
              <span class="nav-icon"><AppIcon :name="child.icon" :size="16" /></span>
              <span>{{ child.label }}</span>
            </router-link>
          </div>
        </div>
      </div>
    </div>

    <div class="nav-right">
      <span class="nav-time">{{ currentTime }}</span>
      <template v-if="isLoggedIn">
        <div class="user-chip" @click="showProfile = true" title="查看个人信息">
          <span class="user-avatar">
            <img v-if="avatarIsImg" :src="user.avatar" alt="avatar" />
            <AppIcon v-else :name="user.avatar" :size="18" />
          </span>
          <span class="user-name">{{ user.name }}</span>
        </div>
        <button class="logout-btn" @click="logout">退出</button>
      </template>
      <template v-else>
        <button class="logout-btn" style="color:#fff;" @click="$router.push('/login')">登录</button>
      </template>
    </div>

    <!-- 移动端 slim 条：头像 + 退出（仅 ≤768px 显示） -->
    <div class="nav-mobile-actions">
      <button class="nav-mobile-avatar" title="个人信息" @click="showProfile = true">
        <img v-if="avatarIsImg" :src="user.avatar" alt="avatar" />
        <AppIcon v-else :name="user.avatar" :size="16" />
      </button>
      <button class="logout-btn nav-mobile-logout" title="退出登录" @click="logout"><AppIcon name="power" :size="15" /></button>
    </div>

    <UserProfileModal v-if="showProfile" @close="showProfile = false" />
  </nav>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { aiConfig, isImageUrl } from '../store/ai.js'
import { user, loadUser, resetUser, can } from '../store/user.js'
import UserProfileModal from './UserProfileModal.vue'
import AppIcon from './AppIcon.vue'

const route = useRoute()
const currentTime = ref('')
const showProfile = ref(false)
const avatarIsImg = computed(() => isImageUrl(user.avatar))
const isLoggedIn = computed(() => !!user.id)
let timer = null

// 基础导航（登录后追加开发者入口）
const navItems = computed(() => {
  const items = [
    { path: '/dashboard', icon: 'bar-chart', label: '数据分析' },
    { path: '/monitor', icon: 'radar', label: '船舶监控' },
    { path: '/data', icon: 'upload', label: '数据上传' }
  ]
  if (isLoggedIn.value) {
    items.push({ path: '/dev', icon: 'flask', label: '开发者' })
  }
  return items
})

// 违规监管 + （有权限者）系统管理 分组，按权限点动态组装。
// 权限点来自 /auth/status（后端 permissions.py 现算），前端只负责显隐 ——
// 真正的门禁在路由上的 @role_required / @perm_required，这里隐藏只是不让用户
// 点了才发现 403。系统总览等到对应阶段有页面了再加进来。
// 注：「系统参数」曾在这里，已按需求移除（级联判定阈值不作为运行时旋钮暴露）。
const navGroups = computed(() => {
  const groups = [
    {
      icon: 'alert-triangle', label: '违规监管',
      children: [
        { path: '/report', icon: 'clipboard', label: '违规监控' }
      ]
    }
  ]
  const adminChildren = [
    ...(can('user.manage') ? [{ path: '/admin/users', icon: 'shield', label: '用户管理' }] : []),
    ...(can('org.manage') ? [{ path: '/admin/orgs', icon: 'building', label: '组织管理' }] : []),
    ...(can('audit.view') ? [{ path: '/admin/audit', icon: 'file-text', label: '审计日志' }] : [])
  ]
  if (adminChildren.length) {
    groups.push({ icon: 'settings', label: '系统管理', children: adminChildren })
  }
  return groups
})

function isActive(path) {
  return route.path === path
}

function isGroupActive(group) {
  return group.children.some(c => isActive(c.path))
}

function updateTime() {
  currentTime.value = new Date().toLocaleString('zh-CN', { hour12: false })
}

onMounted(async () => {
  updateTime()
  timer = setInterval(updateTime, 1000)
  await loadUser()
})

async function logout() {
  try {
    await fetch('/api/v1/auth/logout', { method: 'POST' })
  } catch (e) {}
  resetUser()
  window.location.href = '/'
}

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.topnav {
    height: 64px; color: #fff;
    background: linear-gradient(90deg, #040f1f 0%, #08243d 55%, #0a3d62 100%);
    border-bottom: 1px solid rgba(118, 200, 255, 0.18);
    box-shadow: 0 4px 24px rgba(4, 15, 31, 0.35);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    display: flex; align-items: center; padding: 0 24px; gap: 24px;
    flex-shrink: 0; position: relative; z-index: 1000;
}
.topnav::after {
    content: ''; position: absolute; bottom: 0; left: 0; right: 0; height: 2px;
    background: linear-gradient(90deg, transparent, rgba(24,184,166,0.7), rgba(12,140,233,0.9), rgba(127,106,240,0.6), transparent);
    opacity: 0.5;
}
.nav-left { display: flex; align-items: center; gap: 10px; cursor: pointer; }
.nav-logo-img { height: 44px; border-radius: 8px; }
.nav-logo-icon {
    font-size: 24px; color: #fff;
    width: 42px; height: 42px;
    display: flex; align-items: center; justify-content: center;
    border-radius: 12px;
    background: var(--gradient-primary);
    box-shadow: 0 4px 14px rgba(12, 140, 233, 0.4);
    transform: rotate(-6deg);
}
.nav-logo-text { display: flex; flex-direction: column; }
.nav-logo-title { font-size: 17px; font-weight: 700; letter-spacing: 1px; line-height: 1.2; }
.nav-logo-subtitle { font-size: 10px; color: rgba(255,255,255,0.5); }
.nav-menu { flex: 1; display: flex; align-items: center; gap: 4px; margin-left: 24px; }
.nav-item {
    display: flex; align-items: center; gap: 8px; padding: 8px 16px;
    border-radius: var(--radius-sm); cursor: pointer; color: rgba(255,255,255,0.65);
    text-decoration: none; font-size: 14px; transition: all var(--transition);
    white-space: nowrap;
}
.nav-item:hover { background: rgba(24, 184, 166, 0.14); color: #7ff0e2; }
.nav-item.active {
    background: var(--gradient-primary);
    color: #fff;
    box-shadow: 0 4px 12px rgba(12, 140, 233, 0.4);
}
.nav-icon { font-size: 16px; }
.nav-group { position: relative; }
.nav-arrow { font-size: 10px; opacity: 0.7; margin-left: 2px; }
.nav-dropdown {
    display: none;
    position: absolute; top: 100%; left: 0;
    padding-top: 8px;
}
.nav-group:hover .nav-dropdown,
.nav-group:focus-within .nav-dropdown { display: block; }
.nav-dropdown-menu {
    background: rgba(13, 30, 52, 0.97);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    border: 1px solid rgba(118, 200, 255, 0.2);
    border-radius: var(--radius-sm); box-shadow: 0 12px 32px rgba(2, 15, 34, 0.5);
    padding: 6px; display: flex; flex-direction: column; gap: 2px; min-width: 180px;
}
.dropdown-item {
    display: flex; align-items: center; gap: 10px; padding: 10px 14px;
    border-radius: 6px; color: rgba(255,255,255,0.78); text-decoration: none; font-size: 13px;
    transition: all var(--transition);
}
.dropdown-item:hover { background: var(--sidebar-hover); color: #fff; }
.dropdown-item.active {
    background: linear-gradient(90deg, rgba(12,140,233,0.25), rgba(24,184,166,0.15));
    color: #5caef0; font-weight: 600;
}
.nav-right { display: flex; align-items: center; gap: 16px; }
.nav-time { font-size: 13px; color: rgba(255,255,255,0.6); }
.user-chip { display: flex; align-items: center; gap: 8px; cursor: pointer; padding: 4px 8px; border-radius: 999px; transition: background 0.2s; }
.user-chip:hover { background: rgba(255,255,255,0.1); }
.user-avatar {
    width: 34px; height: 34px; border-radius: 50%;
    background: var(--gradient-teal);
    box-shadow: 0 2px 8px rgba(13, 148, 136, 0.4);
    display: flex; align-items: center; justify-content: center; font-weight: 600; font-size: 13px;
    overflow: hidden;
}
.user-avatar img { width: 100%; height: 100%; object-fit: cover; }
.user-name { font-size: 12px; color: rgba(255,255,255,0.7); }
.logout-btn {
    background: none; border: 1px solid rgba(255,255,255,0.3);
    color: rgba(255,255,255,0.7); padding: 4px 12px; border-radius: 999px;
    cursor: pointer; font-size: 12px; transition: all 0.2s;
}
.logout-btn:hover { border-color: rgba(24,184,166,0.8); color: #7ff0e2; background: rgba(24,184,166,0.12); }
.nav-mobile-actions { display: none; margin-left: auto; align-items: center; gap: 8px; }
.nav-mobile-avatar {
    display: none; width: 32px; height: 32px; border-radius: 50%;
    border: 1px solid rgba(255,255,255,0.35);
    background: var(--gradient-teal);
    align-items: center; justify-content: center;
    font-size: 15px; cursor: pointer; padding: 0; overflow: hidden;
}
.nav-mobile-avatar img { width: 100%; height: 100%; object-fit: cover; }
.nav-mobile-logout { display: none; font-size: 15px; line-height: 1; }

/* 移动端：收缩为 slim logo 条，导航交给底部标签栏 */
@media (max-width: 768px) {
    .topnav { height: 52px; padding: 0 16px; gap: 0; }
    .nav-menu { display: none; }
    .nav-right { display: none; }
    .nav-logo-subtitle { display: none; }
    .nav-logo-title { font-size: 15px; }
    .nav-logo-icon { width: 34px; height: 34px; font-size: 19px; }
    .nav-mobile-actions { display: flex; }
    .nav-mobile-avatar { display: inline-flex; }
    .nav-mobile-logout { display: inline-flex; }
}
</style>
