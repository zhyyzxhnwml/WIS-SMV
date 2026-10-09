import { reactive } from 'vue'
import { get } from '../api/request'

// 当前登录用户（全局共享，后端 Flask session 决定身份）
export const user = reactive({
  id: null,
  username: '',
  name: '',
  role: '',
  roleLabel: '',   // 角色的展示名（「用户」→「普通用户」），由后端下发
  perms: [],       // 权限点清单，由 /auth/status 下发（permissions.py 现算）
  avatar: '👤',
  createdAt: '',
  loaded: false
})

// 从 /auth/status 加载当前登录用户
export async function loadUser() {
  try {
    const st = await get('/auth/status')
    if (st?.id) {
      Object.assign(user, {
        id: st.id,
        username: st.username || '',
        name: st.name || st.username || '',
        role: st.role || '',
        roleLabel: st.role_label || st.role || '',
        perms: Array.isArray(st.perms) ? st.perms : [],
        avatar: st.avatar || '👤',
        createdAt: st.created_at || '',
        loaded: true
      })
    } else {
      resetUser()
    }
  } catch (e) {
    resetUser()
  }
}

export function resetUser() {
  user.id = null
  user.username = ''
  user.name = ''
  user.role = ''
  user.roleLabel = ''
  user.perms = []
  user.avatar = '👤'
  user.createdAt = ''
  user.loaded = true
}

// 管理员/超级管理员有管理权限
export const isAdmin = () => ['超级管理员', '管理员'].includes(user.role)

// 超级管理员专属
export const isSuper = () => user.role === '超级管理员'

// 权限点判断。清单由后端下发，前端只做显隐 —— **真正的门禁在后端**
// （@perm_required），这里隐藏菜单只是不让用户点了才发现 403。
export const can = (key) => user.perms.includes(key)
