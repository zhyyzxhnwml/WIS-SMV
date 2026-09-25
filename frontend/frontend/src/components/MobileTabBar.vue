<template>
  <nav class="mobile-tabbar" aria-label="主导航">
    <router-link
      v-for="tab in tabs"
      :key="tab.path"
      :to="tab.path"
      class="tab-item"
      :class="{ active: isActive(tab.path) }"
    >
      <span class="tab-icon"><AppIcon :name="tab.icon" :size="20" /></span>
      <span class="tab-label">{{ tab.label }}</span>
    </router-link>
  </nav>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { user, isAdmin } from '../store/user.js'
import AppIcon from './AppIcon.vue'

const route = useRoute()

// 所有成员追加「开发」tab（开发者模式）；管理员/超级管理员再追加「管理」tab
const tabs = computed(() => {
  const base = [
    { path: '/dashboard', icon: 'bar-chart', label: '数据分析' },
    { path: '/monitor', icon: 'radar', label: '船舶监控' },
    { path: '/data', icon: 'upload', label: '数据上传' },
    { path: '/report', icon: 'alert-triangle', label: '违规监管' },
    { path: '/ai', icon: 'bot', label: '智能对话' },
  ]
  if (user.id) {
    base.push({ path: '/dev', icon: 'flask', label: '开发' })
  }
  if (isAdmin()) {
    base.push({ path: '/admin/orgs', icon: 'settings', label: '管理' })
  }
  return base
})

// 前缀匹配：/ai/colors 也高亮"智能对话"
function isActive(path) {
  return route.path === path || route.path.startsWith(path + '/')
}
</script>

<style scoped>
.mobile-tabbar {
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 9000; /* 低于悬浮 FAB(9999/10001)，高于页面内容 */
  display: none;
  height: calc(var(--tabbar-height) + var(--tabbar-safe-bottom));
  padding-bottom: var(--tabbar-safe-bottom);
  background: #fff; border-top: 1px solid var(--border);
  box-shadow: 0 -4px 20px rgba(6, 31, 56, 0.10);
  -webkit-tap-highlight-color: transparent;
}
.tab-item {
  flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 2px; text-decoration: none; color: var(--text-secondary);
  font-size: 10px; user-select: none;
}
.tab-icon { font-size: 20px; line-height: 1; }
.tab-item.active { color: var(--primary); font-weight: 600; }
.tab-item.active .tab-icon { transform: translateY(-1px); }

@media (max-width: 768px) {
  .mobile-tabbar { display: flex; }
}
</style>
