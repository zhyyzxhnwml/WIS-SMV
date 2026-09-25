import { get, post, put } from './request'

export function fetchProfile() {
  return get('/ai/profile')
}

export function updateProfile(data) {
  return put('/ai/profile', data)
}

export function updateColors(data) {
  return put('/ai/colors', data)
}

export function generateLogo() {
  return post('/ai/logo')
}

export function sendChat(data) {
  return post('/ai/chat', data)
}

export function fetchTools() {
  return get('/ai/tools')
}
