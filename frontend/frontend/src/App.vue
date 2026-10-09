<template>
  <div v-if="isLoginPage" class="fullscreen-page">
    <router-view v-slot="{ Component }">
      <transition name="page-fade" mode="out-in">
        <component :is="Component" />
      </transition>
    </router-view>
  </div>
  <!--
    未确认登录状态前不渲染应用外壳。
    否则从深链接进来且未登录时（如直接开 /#/report），isLoginPage 为 false，
    TopNav / FloatingBall / 页面组件会**先挂载**并各自打一遍需要登录的接口 ——
    全部 401；FloatingBall 还带 3 秒轮询，会一直 401 下去。

    两个条件缺一不可，别合并成一个：
      user.loaded —— 认证检查**是否已回来**（store 里 resetUser() 也会把它置 true，
                     所以它不代表"已登录"，只代表"已经知道答案了"）
      user.id     —— 答案是不是"有登录用户"
    只看 loaded 会在未登录时放行外壳（resetUser 已把 loaded 置 true 了）；
    只看 id 会在检查还没回来时闪一下空壳。
  -->
  <div v-else-if="!user.loaded || !user.id" class="boot-splash" aria-busy="true"></div>
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
import { computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { onMounted } from 'vue'
import TopNav from './components/TopNav.vue'
import FloatingBall from './components/FloatingBall.vue'
import FloatingAIChat from './components/FloatingAIChat.vue'
import MobileTabBar from './components/MobileTabBar.vue'
import { loadAiProfile } from './store/ai.js'
import { loadUser, user } from './store/user.js'

const route = useRoute()
const router = useRouter()
const isLoginPage = computed(() => route.path === '/login')

// AI 配置是登录后才能取的（/ai/profile 需要身份）。放在这里而不是 onMounted 里
// 直接调，是为了兼顾两种情况：进页面时已登录、以及刚在登录页登录成功
// （Login.vue 会先 loadUser() 再 router.push，所以 user.id 翻转先于路由变化）。
// 盯 user.id 而不是 user.loaded —— 后者在未登录时也会翻转（resetUser），
// 那会给一个没登录的请求打 /ai/profile，又是一个 401。
watch(() => user.id, (id) => { if (id) loadAiProfile() }, { immediate: true })

onMounted(async () => {
  // loadUser() 内部就是打 /auth/status —— 不要再单独 fetch 一次同一个接口
  await loadUser()
  // 判据是 user.id 不是 user.loaded：resetUser() 会把 loaded 置 true，
  // 用它做判据会导致未登录时永远不跳登录页（这次的实际故障）。
  if (!user.id && route.path !== '/login') {
    router.push('/login')
  }
})
</script>

<style>
.boot-splash {
  height: 100vh;
  height: 100dvh;
  width: 100vw;
  background: #f4f9fd;
}

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
