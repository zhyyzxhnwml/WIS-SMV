<template>
  <div class="page">
    <PageHeader icon="palette" title="颜色设置" subtitle="船舶状态与轨迹的显示颜色配置" />

    <div class="card">
      <div class="card-header">
        <h3>船舶轨迹颜色设置</h3>
        <span class="card-subtitle">智能体可设置船舶状态与轨迹的显示颜色（如标出违规船舶）</span>
      </div>
      <div class="card-body">
        <div class="color-grid">
          <div v-for="opt in colorOptions" :key="opt.key" class="color-item">
            <span class="color-dot" :style="{ background: form[opt.key] }"></span>
            <div class="color-info">
              <strong>{{ opt.label }}</strong>
              <small>{{ opt.desc }}</small>
            </div>
            <input type="color" class="color-picker" v-model="form[opt.key]">
            <span class="color-hex">{{ form[opt.key] }}</span>
          </div>
        </div>

        <div class="preview-row">
          <span class="preview-label">图例预览：</span>
          <span v-for="opt in colorOptions" :key="opt.key" class="legend-item">
            <span v-if="opt.key === 'track'" class="track-line" :style="{ background: form[opt.key] }"></span>
            <span v-else class="color-dot sm" :style="{ background: form[opt.key] }"></span>
            {{ opt.label }}
          </span>
        </div>

        <div class="form-actions">
          <button class="btn btn-primary" @click="save"><AppIcon name="save" inline /> 保存配置</button>
          <button class="btn btn-secondary" @click="reset"><AppIcon name="refresh-cw" inline /> 恢复默认</button>
        </div>
        <p class="form-tip">保存后，船舶监控页面的图例与标记颜色将同步更新。接口 PUT /ai/colors 已写入 API 文档，后端可随时修改默认配色。</p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import PageHeader from '../../components/PageHeader.vue'
import AppIcon from '../../components/AppIcon.vue'
import { aiConfig, DEFAULT_COLORS, saveColors } from '../../store/ai.js'

const form = ref({ ...DEFAULT_COLORS })

const colorOptions = [
  { key: 'normal', label: '正常航行', desc: '正常航行状态船舶的标记颜色' },
  { key: 'anchored', label: '锚泊', desc: '锚泊状态船舶的标记颜色' },
  { key: 'berthed', label: '靠泊', desc: '靠泊状态船舶的标记颜色' },
  { key: 'violating', label: '违规船舶', desc: '违规船舶的高亮标记颜色' },
  { key: 'track', label: '航行轨迹', desc: '船舶历史轨迹线的颜色' }
]

onMounted(() => {
  form.value = { ...DEFAULT_COLORS, ...aiConfig.colors }
})

async function save() {
  try {
    await saveColors(form.value)
    alert('颜色配置已保存，船舶监控页面已同步')
  } catch (e) {
    alert('保存失败')
  }
}

function reset() {
  form.value = { ...DEFAULT_COLORS }
}
</script>

<style scoped>
.color-grid { display: flex; flex-direction: column; gap: 14px; max-width: 720px; }
.color-item {
    display: flex; align-items: center; gap: 16px;
    padding: 14px 16px; border: 1px solid var(--border); border-radius: var(--radius-sm);
}
.color-dot {
    width: 28px; height: 28px; border-radius: 50%; flex-shrink: 0;
    border: 2px solid rgba(0,0,0,0.08);
}
.color-dot.sm { width: 12px; height: 12px; border: none; }
.color-info { flex: 1; }
.color-info strong { font-size: 14px; display: block; }
.color-info small { font-size: 12px; color: var(--text-secondary); }
.color-picker {
    width: 44px; height: 32px; border: none; border-radius: 6px; cursor: pointer;
    padding: 0; background: none;
}
.color-hex {
    font-size: 12px; color: var(--text-secondary); font-family: monospace; width: 76px;
}
.preview-row {
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    padding: 16px; background: var(--bg); border-radius: var(--radius-sm); margin-top: 20px;
}
.preview-label { font-size: 13px; font-weight: 600; }
.legend-item { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-secondary); }
.track-line { width: 18px; height: 4px; border-radius: 2px; display: inline-block; }
.form-tip { font-size: 12px; color: var(--text-secondary); margin-top: 16px; }

/* 小屏：配色行换行、取色器全宽 */
@media (max-width: 480px) {
    .color-item { flex-wrap: wrap; row-gap: 10px; }
    .color-picker { width: 100%; height: 36px; }
}
</style>
