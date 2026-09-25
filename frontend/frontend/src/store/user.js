import { reactive } from 'vue'
import { get } from '../api/request'

// 当前登录用户（全局共享，后端 Flask session 决定身份）
export const user = reactive({
  id: null,
  username: '',
  name: '',
  role: '',
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
  user.avatar = '👤'
  user.createdAt = ''
  user.loaded = true
}

// 管理员/超级管理员有管理权限
export const isAdmin = () => ['超级管理员', '管理员'].includes(user.role)

// 超级管理员专属
export const isSuper = () => user.role === '超级管理员'
