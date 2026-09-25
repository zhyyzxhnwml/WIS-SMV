<template>
  <div v-if="isLoginPage" class="fullscreen-page">
    <router-view v-slot="{ Component }">
      <transition name="page-fade" mode="out-in">
        <component :is="Component" />
      </transition>
    </router-view>
  </div>
  <div v-else class="app-container">
    <TopNav />
    <main class="main-content">
      <div class="content-area">
        <router-view v-slot="{ Component }">
          <transition name="page-fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </div>
    </main>
    <FloatingBall />
    <FloatingAIChat />
    <MobileTabBar />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { onMounted } from 'vue'
import TopNav from './components/TopNav.vue'
import FloatingBall from './components/FloatingBall.vue'
import FloatingAIChat from './components/FloatingAIChat.vue'
import MobileTabBar from './components/MobileTabBar.vue'
import { loadAiProfile } from './store/ai.js'
import { loadUser } from './store/user.js'

const route = useRoute()
const router = useRouter()
const isLoginPage = computed(() => route.path === '/login')

onMounted(async () => {
  loadAiProfile()
  await loadUser()
  // 检查登录状态，未登录跳转到登录页
  try {
    const resp = await fetch('/api/v1/auth/status')
    const json = await resp.json()
    if (!json?.data?.id && route.path !== '/login') {
      router.push('/login')
    }
  } catch (e) {
    if (route.path !== '/login') router.push('/login')
  }
})
</script>

<style>
.fullscreen-page {
  height: 100vh;
  height: 100dvh;
  width: 100vw;
  overflow: hidden;
}

/* 页面切换过渡 */
.page-fade-enter-active,
.page-fade-leave-active {
  transition: opacity 0.28s ease, transform 0.28s ease;
}
.page-fade-enter-from {
  opacity: 0;
  transform: translateY(10px);
}
.page-fade-leave-to {
  opacity: 0;
  transform: translateY(-6px);
}
</style>
