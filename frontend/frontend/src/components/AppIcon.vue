<template>
  <svg v-if="key" class="app-icon" :class="{ 'app-icon-inline': inline }"
       :width="dim" :height="dim" viewBox="0 0 24 24" fill="none"
       stroke="currentColor" :stroke-width="strokeWidth"
       stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"
       v-html="ICONS[key]"></svg>
  <span v-else class="app-icon-text">{{ name }}</span>
</template>

<script setup>
import { computed } from 'vue'
import { ICONS, EMOJI_TO_ICON } from '../assets/icons.js'

const props = defineProps({
  // 图标键（如 'ship' / 'bar-chart'）或 emoji（如 '🚢'）或未知字符串（回退原样文本）
  name: { type: String, default: '' },
  size: { type: [Number, String], default: 20 },
  strokeWidth: { type: [Number, String], default: 1.8 },
  inline: { type: Boolean, default: false }
})

// 归一化：去掉 emoji 变体选择符 U+FE0F，兼容 🛰️/🛰、⚠️/⚠
function normalize(v) {
  return String(v || '').replace(/️/g, '')
}

const key = computed(() => {
  const n = normalize(props.name)
  if (ICONS[n]) return n
  return EMOJI_TO_ICON[n] || null
})

const dim = computed(() => (props.inline ? undefined : (typeof props.size === 'number' ? props.size + 'px' : props.size)))
</script>

<style scoped>
.app-icon {
  display: inline-block;
  flex-shrink: 0;
  vertical-align: middle;
}
.app-icon-inline {
  width: 1.2em;
  height: 1.2em;
  vertical-align: -0.22em;
}
.app-icon-text {
  display: inline-block;
  line-height: 1;
}
</style>
