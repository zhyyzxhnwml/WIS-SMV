<template>
  <div class="page-header">
    <div class="ph-icon" :class="{ 'ph-icon-lg': !subtitle }">
      <img v-if="iconIsImage" :src="icon" alt="" class="ph-icon-img" />
      <AppIcon v-else :name="icon" :size="iconSize" />
    </div>
    <div class="ph-text">
      <h1 class="ph-title">{{ title }}</h1>
      <p v-if="subtitle" class="ph-subtitle">{{ subtitle }}</p>
    </div>
    <div v-if="$slots.default" class="ph-actions">
      <slot />
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { isImageUrl } from '../store/ai.js'
import AppIcon from './AppIcon.vue'

const props = defineProps({
  icon: { type: String, default: '' },
  title: { type: String, required: true },
  subtitle: { type: String, default: '' }
})

const iconIsImage = computed(() => isImageUrl(props.icon))
const iconSize = computed(() => (props.subtitle ? 26 : 30))
</script>

<style scoped>
.page-header {
  display: flex; align-items: center; gap: 16px;
  padding: 18px 22px; margin-bottom: 20px;
  border-radius: var(--radius);
  background:
    radial-gradient(600px 120px at 100% 0%, rgba(24, 184, 166, 0.1), transparent 60%),
    radial-gradient(500px 140px at 0% 100%, rgba(12, 140, 233, 0.08), transparent 55%),
    linear-gradient(135deg, rgba(255, 255, 255, 0.92), rgba(244, 251, 255, 0.85));
  border: 1px solid rgba(169, 205, 226, 0.5);
  box-shadow: var(--shadow);
  position: relative; overflow: hidden;
}
.page-header::after {
  content: ''; position: absolute; right: -36px; top: -36px;
  width: 132px; height: 132px; border-radius: 50%;
  background: radial-gradient(circle, rgba(12, 140, 233, 0.12), transparent 70%);
}
.ph-icon {
  width: 52px; height: 52px; border-radius: 15px; flex-shrink: 0;
  background: var(--gradient-primary);
  box-shadow: 0 6px 18px rgba(12, 140, 233, 0.35);
  display: flex; align-items: center; justify-content: center;
  font-size: 26px; color: #fff; transform: rotate(-4deg);
  transition: transform var(--transition);
  overflow: hidden;
}
.ph-icon-lg { width: 58px; height: 58px; font-size: 30px; }
.ph-icon .app-icon { transform: rotate(4deg); }
.ph-icon:has(.ph-icon-img) { transform: rotate(0); }
.ph-icon-img { width: 100%; height: 100%; object-fit: cover; border-radius: inherit; }
.page-header:hover .ph-icon { transform: rotate(0deg) scale(1.04); }
.page-header:hover .ph-icon:has(.ph-icon-img) { transform: scale(1.06); }
.ph-text { flex: 1; min-width: 0; position: relative; z-index: 1; }
.ph-title { font-size: 20px; font-weight: 800; color: var(--text); letter-spacing: 0.5px; line-height: 1.3; }
.ph-subtitle { font-size: 13px; color: var(--text-secondary); margin-top: 3px; }
.ph-actions { position: relative; z-index: 1; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
</style>
