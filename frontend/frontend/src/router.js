import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  { path: '/', redirect: '/dashboard' },

  // 监控子页原先挂在 /raw、/cleaned、/final、/zone 这类扁平路径上，后来收进了 /monitor/*。
  // 老书签和历史记录仍会打到旧地址，匹配不到路由就是整片白屏，这里重定向兼容。
  // 注意：/cascade 不是旧地址，它是「级联日志」页，别动。
  { path: '/raw', redirect: '/monitor/raw' },
  { path: '/cleaned', redirect: '/monitor/cleaned' },
  { path: '/final', redirect: '/monitor/final' },
  { path: '/zone', redirect: '/monitor/zone' },
  { path: '/login', name: 'login', component: () => import('./components/pages/Login.vue'), meta: { title: '登录' } },
  { path: '/dashboard', name: 'dashboard', component: () => import('./components/pages/Dashboard.vue'), meta: { title: '数据分析' } },
  { path: '/monitor', component: () => import('./components/pages/MonitorLayout.vue'), meta: { title: '船舶监控' },
    children: [
      { path: '', redirect: 'raw' },
      // 顺序即处理流水线的顺序：原始 → 清洗 → 级联 → 最终（步骤条按此渲染）
      { path: 'raw', name: 'monitor-raw', component: () => import('./components/pages/MonitorRaw.vue'), meta: { title: '原始数据' } },
      { path: 'cleaned', name: 'monitor-cleaned', component: () => import('./components/pages/MonitorCleaned.vue'), meta: { title: '清洗后' } },
      { path: 'cascade', name: 'monitor-cascade', component: () => import('./components/pages/MonitorCascade.vue'), meta: { title: '级联判断后' } },
      // 最终结果沿用原「船舶监控」页（未做改动）
      { path: 'final', name: 'monitor-final', component: () => import('./components/pages/ShipMonitor.vue'), meta: { title: '最终结果' } },
      // 区域分析是随取随用的工具，不属于流水线阶段
      { path: 'zone', name: 'monitor-zone', component: () => import('./components/pages/MonitorZone.vue'), meta: { title: '区域分析' } },
    ]
  },
  { path: '/report', name: 'report', component: () => import('./components/pages/ReportDocuments.vue'), meta: { title: '违规监控' } },
  { path: '/data', name: 'data', component: () => import('./components/pages/DataUpload.vue'), meta: { title: '数据上传' } },
  { path: '/ai', name: 'ai', component: () => import('./components/pages/AIChat.vue'), meta: { title: '智能对话' } },
  { path: '/ai/colors', name: 'ai-colors', component: () => import('./components/pages/AIColors.vue'), meta: { title: '颜色设置' } },
  { path: '/cascade', name: 'cascade', component: () => import('./components/pages/CascadeLog.vue'), meta: { title: '级联日志' } },
  { path: '/dev', name: 'dev', component: () => import('./components/pages/DevMode.vue'), meta: { title: '开发者模式' } },
  { path: '/admin/users', name: 'admin-users', component: () => import('./components/pages/UserManage.vue'), meta: { title: '用户管理' } },
  { path: '/admin/orgs', name: 'admin-orgs', component: () => import('./components/pages/OrgManage.vue'), meta: { title: '组织管理' } },

  // 兜底：任何没匹配上的地址都回数据分析，而不是渲染出一片空白
  { path: '/:pathMatch(.*)*', redirect: '/dashboard' }
]

const router = createRouter({
  history: createWebHashHistory(),
  routes
})

export default router
