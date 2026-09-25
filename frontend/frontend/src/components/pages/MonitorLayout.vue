<template>
  <div class="page">
    <!-- 按处理顺序逐步查看（原始 → 清洗 → 级联 → 最终），而不是平级并列 -->
    <nav class="stepper">
      <div class="stepper-track">
        <template v-for="(s, i) in steps" :key="s.to">
          <router-link :to="s.to" class="step" :class="{ active: i === stepIndex, done: i < stepIndex }">
            <span class="step-no">
              <AppIcon v-if="i < stepIndex" name="check" :size="13" />
              <template v-else>{{ i + 1 }}</template>
            </span>
            <span class="step-text">
              <span class="step-label">{{ s.label }}</span>
              <span class="step-hint">{{ s.hint }}</span>
            </span>
          </router-link>
          <span v-if="i < steps.length - 1" class="step-line" :class="{ done: i < stepIndex }"></span>
        </template>
      </div>
    </nav>

    <!-- 逐步导航：一步看完再看下一步 -->
    <div class="stepper-nav">
      <button class="btn btn-secondary" :disabled="stepIndex === 0" @click="go(stepIndex - 1)">← 上一步</button>
      <span class="nav-pos">
        <strong>{{ steps[stepIndex].label }}</strong>
        <span>第 {{ stepIndex + 1 }} / {{ steps.length }} 步</span>
      </span>
      <button class="btn btn-primary" :disabled="stepIndex === steps.length - 1" @click="go(stepIndex + 1)">下一步 →</button>
    </div>

    <router-view />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppIcon from '../../components/AppIcon.vue'

const steps = [
  { to: '/monitor/raw', label: '原始数据', hint: '清洗前轨迹 + 噪声点' },
  { to: '/monitor/cleaned', label: '清洗后', hint: '保留轨迹 + 质量标记' },
  { to: '/monitor/cascade', label: '级联判断后', hint: 'L1/L2/L3 判定结果' },
  { to: '/monitor/final', label: '最终结果', hint: '全部轨迹与违规着色' },
]

const route = useRoute()
const router = useRouter()

// 子路由只有这四步，stepIndex 一定落在 0..3
const stepIndex = computed(() => Math.max(0, steps.findIndex(s => s.to === route.path)))

function go(i) {
  if (i >= 0 && i < steps.length) router.push(steps[i].to)
}
</script>

<style scoped>
.stepper {
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
    background: var(--gradient-card);
    border: 1px solid rgba(169, 205, 226, 0.4);
    border-radius: var(--radius);
    padding: 12px 16px;
    box-shadow: var(--shadow);
}
.stepper-track { display: flex; align-items: center; flex: 1; flex-wrap: wrap; row-gap: 10px; }

.step {
    display: flex; align-items: center; gap: 9px;
    padding: 5px 10px;
    border-radius: 9px;
    text-decoration: none;
    border: 1px solid transparent;
    transition: all var(--transition);
}
.step-no {
    flex-shrink: 0;
    width: 26px; height: 26px;
    display: flex; align-items: center; justify-content: center;
    border-radius: 50%;
    font-size: 13px; font-weight: 700;
    background: rgba(120, 160, 190, 0.16);
    color: var(--text-secondary);
    border: 1px solid rgba(169, 205, 226, 0.5);
    transition: all var(--transition);
}
.step-text { display: flex; flex-direction: column; line-height: 1.25; }
.step-label { font-size: 14px; font-weight: 600; color: var(--text-secondary); }
.step-hint { font-size: 11px; color: var(--text-secondary); opacity: 0.7; }

.step:hover { background: rgba(12, 140, 233, 0.08); }
.step:hover .step-label { color: var(--primary, #0c8ce9); }

.step.active { background: rgba(12, 140, 233, 0.1); border-color: rgba(12, 140, 233, 0.35); }
.step.active .step-no {
    background: linear-gradient(135deg, #0c8ce9, #18b8a6);
    color: #fff; border-color: transparent;
    box-shadow: 0 4px 12px rgba(12, 140, 233, 0.35);
}
.step.active .step-label { color: var(--primary, #0c8ce9); }

.step.done .step-no { background: #18b8a6; color: #fff; border-color: transparent; }
.step.done .step-label { color: var(--text, #0d2b42); }

.step-line {
    flex: 1; min-width: 18px; height: 2px;
    background: rgba(169, 205, 226, 0.55);
    border-radius: 2px;
    transition: background var(--transition);
}
.step-line.done { background: #18b8a6; }

.stepper-nav {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    margin: 12px 0 16px;
}
.nav-pos { display: flex; flex-direction: column; align-items: center; line-height: 1.3; }
.nav-pos strong { font-size: 14px; color: var(--text, #0d2b42); }
.nav-pos span { font-size: 11px; color: var(--text-secondary); }
.stepper-nav .btn:disabled { opacity: 0.45; cursor: not-allowed; }

@media (max-width: 768px) {
    .stepper { padding: 10px 12px; }
    .step { padding: 4px 6px; gap: 6px; }
    .step-hint { display: none; }
    .step-label { font-size: 12px; }
    .step-no { width: 22px; height: 22px; font-size: 12px; }
    .step-line { min-width: 10px; }
    .stepper-nav .btn { padding: 6px 12px; font-size: 13px; }
}
</style>
