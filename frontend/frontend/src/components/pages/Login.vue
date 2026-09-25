<template>
  <div class="login-page">
    <!-- 海洋动效背景 -->
    <div class="ocean-bg">
      <div class="ocean-glow ocean-glow-1"></div>
      <div class="ocean-glow ocean-glow-2"></div>
      <div class="wave wave-1"></div>
      <div class="wave wave-2"></div>
      <div class="wave wave-3"></div>
      <div class="bubbles">
        <span v-for="i in 12" :key="i" class="bubble" :style="bubbleStyle(i)"></span>
      </div>
    </div>

    <div class="login-card">
      <div class="login-header">
        <div class="login-icon-wrap">
          <span class="login-icon"><AppIcon name="anchor" :size="34" style="color:#fff;" /></span>
        </div>
        <h2>智海巡航</h2>
        <p>港口船舶违规行为智能识别平台</p>
      </div>

      <!-- 登录表单 -->
      <form v-if="!isRegister" @submit.prevent="doLogin" class="login-form">
        <div class="form-group">
          <label>用户名</label>
          <div class="input-wrap">
            <span class="input-icon"><AppIcon name="user" inline /></span>
            <input type="text" class="form-control" v-model="username" placeholder="输入用户名" required autocomplete="username">
          </div>
        </div>
        <div class="form-group">
          <label>密码</label>
          <div class="input-wrap">
            <span class="input-icon"><AppIcon name="lock" inline /></span>
            <input type="password" class="form-control" v-model="password" placeholder="输入密码" required autocomplete="current-password">
          </div>
        </div>
        <button type="submit" class="btn btn-primary login-btn" :disabled="loading">
          <span v-if="loading" class="btn-spinner"></span>
          {{ loading ? '登录中...' : '登 录' }}
        </button>
        <p class="form-switch">
          没有账号？<a href="#" @click.prevent="isRegister = true">立即注册</a>
        </p>
        <p v-if="errorMsg" class="error-msg">{{ errorMsg }}</p>
      </form>

      <!-- 注册表单 -->
      <form v-else @submit.prevent="doRegister" class="login-form">
        <div class="form-group">
          <label>用户名</label>
          <div class="input-wrap">
            <span class="input-icon"><AppIcon name="user" inline /></span>
            <input type="text" class="form-control" v-model="username" placeholder="至少2位字符" required>
          </div>
        </div>
        <div class="form-group">
          <label>密码</label>
          <div class="input-wrap">
            <span class="input-icon"><AppIcon name="lock" inline /></span>
            <input type="password" class="form-control" v-model="password" placeholder="至少6位密码" required>
          </div>
        </div>
        <div class="form-group">
          <label>确认密码</label>
          <div class="input-wrap">
            <span class="input-icon"><AppIcon name="key" inline /></span>
            <input type="password" class="form-control" v-model="password2" placeholder="再次输入密码" required>
          </div>
        </div>
        <button type="submit" class="btn btn-primary login-btn" :disabled="loading">
          <span v-if="loading" class="btn-spinner"></span>
          {{ loading ? '注册中...' : '注 册' }}
        </button>
        <p class="form-switch">
          已有账号？<a href="#" @click.prevent="isRegister = false">返回登录</a>
        </p>
        <p v-if="errorMsg" class="error-msg">{{ errorMsg }}</p>
      </form>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { loadUser } from '../../store/user.js'
import AppIcon from '../AppIcon.vue'

const router = useRouter()
const username = ref('')
const password = ref('')
const password2 = ref('')
const isRegister = ref(false)
const loading = ref(false)
const errorMsg = ref('')

function bubbleStyle(i) {
  const size = 6 + ((i * 7) % 16)
  return {
    left: (i * 8.3) % 100 + '%',
    width: size + 'px',
    height: size + 'px',
    animationDelay: (i * 0.9) % 6 + 's',
    animationDuration: 4 + ((i * 13) % 5) + 's',
  }
}

async function doLogin() {
  loading.value = true
  errorMsg.value = ''
  try {
    const resp = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: username.value, password: password.value }),
    })
    const json = await resp.json()
    if (json.code === 200) {
      await loadUser()
      router.push('/dashboard')
    } else {
      errorMsg.value = json.message || '登录失败'
    }
  } catch (e) {
    errorMsg.value = '网络错误，请检查后端服务'
  } finally {
    loading.value = false
  }
}

async function doRegister() {
  loading.value = true
  errorMsg.value = ''
  if (password.value !== password2.value) {
    errorMsg.value = '两次密码输入不一致'
    loading.value = false
    return
  }
  try {
    const resp = await fetch('/api/v1/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: username.value, password: password.value }),
    })
    const json = await resp.json()
    if (json.code === 200) {
      alert('注册成功，请登录')
      isRegister.value = false
    } else {
      errorMsg.value = json.message || '注册失败'
    }
  } catch (e) {
    errorMsg.value = '网络错误，请检查后端服务'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  position: relative;
  height: 100vh;
  height: 100dvh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--gradient-ocean);
  overflow: hidden;
}

/* ===== 海洋动效背景 ===== */
.ocean-bg { position: absolute; inset: 0; overflow: hidden; }

.ocean-glow {
  position: absolute; border-radius: 50%; filter: blur(90px);
  opacity: 0.45; animation: glowFloat 12s ease-in-out infinite;
}
.ocean-glow-1 {
  width: 520px; height: 520px; top: -120px; right: -80px;
  background: radial-gradient(circle, rgba(24,184,166,0.55), transparent 70%);
}
.ocean-glow-2 {
  width: 460px; height: 460px; bottom: -140px; left: -100px;
  background: radial-gradient(circle, rgba(12,140,233,0.5), transparent 70%);
  animation-delay: -6s;
}
@keyframes glowFloat {
  0%, 100% { transform: translate(0, 0) scale(1); }
  50% { transform: translate(30px, -30px) scale(1.08); }
}

/* 波浪 */
.wave {
  position: absolute; left: -10%; width: 120%; height: 100%;
  background-repeat: repeat-x; background-position: 0 0;
  opacity: 0.16; pointer-events: none;
}
.wave-1 {
  top: 62%; height: 180px;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1200 180' preserveAspectRatio='none'%3E%3Cpath fill='%2318b8a6' d='M0,90 C180,20 360,20 540,90 C720,160 900,160 1080,90 C1140,58 1170,40 1200,30 L1200,180 L0,180 Z'/%3E%3C/svg%3E");
  background-size: 1200px 100%;
  animation: waveSlide 22s linear infinite;
}
.wave-2 {
  top: 70%; height: 160px;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1200 160' preserveAspectRatio='none'%3E%3Cpath fill='%230c8ce9' d='M0,80 C150,140 300,20 450,80 C600,140 750,20 900,80 C1050,140 1150,60 1200,50 L1200,160 L0,160 Z'/%3E%3C/svg%3E");
  background-size: 1200px 100%;
  animation: waveSlide 30s linear infinite reverse;
  opacity: 0.12;
}
.wave-3 {
  top: 78%; height: 120px;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1200 120' preserveAspectRatio='none'%3E%3Cpath fill='%2306409b' d='M0,60 C200,120 400,0 600,60 C800,120 1000,0 1200,60 L1200,120 L0,120 Z'/%3E%3C/svg%3E");
  background-size: 1200px 100%;
  animation: waveSlide 36s linear infinite;
  opacity: 0.5;
}
@keyframes waveSlide {
  from { background-position: 0 0; }
  to { background-position: 1200px 0; }
}

/* 气泡 */
.bubble {
  position: absolute; bottom: -20px;
  border-radius: 50%;
  background: radial-gradient(circle at 30% 30%, rgba(255,255,255,0.7), rgba(140,220,255,0.2));
  border: 1px solid rgba(180, 230, 255, 0.35);
  animation: bubbleRise 5s linear infinite;
  opacity: 0;
}
@keyframes bubbleRise {
  0% { transform: translateY(0); opacity: 0; }
  12% { opacity: 0.7; }
  85% { opacity: 0.5; }
  100% { transform: translateY(-85vh); opacity: 0; }
}

/* ===== 登录卡片 ===== */
.login-card {
  position: relative; z-index: 10;
  background: rgba(255, 255, 255, 0.88);
  backdrop-filter: blur(18px);
  -webkit-backdrop-filter: blur(18px);
  border: 1px solid rgba(255, 255, 255, 0.6);
  border-radius: 24px;
  padding: 44px 40px 36px;
  width: 400px;
  max-width: 90vw;
  box-shadow: 0 24px 70px rgba(2, 15, 34, 0.45);
  animation: cardIn 0.6s ease;
}
@keyframes cardIn {
  from { opacity: 0; transform: translateY(24px); }
  to { opacity: 1; transform: translateY(0); }
}

.login-header { text-align: center; margin-bottom: 32px; }

.login-icon-wrap {
  width: 72px; height: 72px; margin: 0 auto 14px;
  border-radius: 22px;
  background: var(--gradient-primary);
  box-shadow: 0 10px 28px rgba(12, 140, 233, 0.4);
  display: flex; align-items: center; justify-content: center;
  transform: rotate(-6deg);
  animation: float 4s ease-in-out infinite;
}
@keyframes float {
  0%, 100% { transform: rotate(-6deg) translateY(0); }
  50% { transform: rotate(-6deg) translateY(-6px); }
}
.login-icon { font-size: 34px; }

.login-header h2 {
  font-size: 24px; font-weight: 800; margin-bottom: 6px;
  background: var(--gradient-primary);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  letter-spacing: 2px;
}

.login-header p { font-size: 13px; color: var(--text-secondary); }

.login-form { display: flex; flex-direction: column; gap: 14px; }

.input-wrap { position: relative; }
.input-icon {
  position: absolute; left: 14px; top: 50%; transform: translateY(-50%);
  font-size: 14px; opacity: 0.65; z-index: 1;
}
.input-wrap .form-control { padding-left: 40px; }

.login-btn {
  width: 100%; padding: 12px !important;
  font-size: 15px !important; border-radius: 12px !important;
  margin-top: 6px; justify-content: center;
  letter-spacing: 6px; font-weight: 600;
}
.btn-spinner {
  width: 16px; height: 16px; margin-right: 6px;
  border: 2px solid rgba(255,255,255,0.4); border-top-color: #fff;
  border-radius: 50%; animation: spin 0.7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

.form-switch { text-align: center; font-size: 13px; color: var(--text-secondary); }
.form-switch a { color: var(--primary); cursor: pointer; font-weight: 600; }

.error-msg {
  color: var(--danger); font-size: 13px; text-align: center;
  background: #fde8e8; padding: 8px 12px; border-radius: 8px;
  animation: shake 0.3s ease;
}
@keyframes shake {
  0%, 100% { transform: translateX(0); }
  25% { transform: translateX(-4px); }
  75% { transform: translateX(4px); }
}

/* 小屏：登录卡片收紧 */
@media (max-width: 480px) {
    .login-card { padding: 28px 20px; }
}
</style>