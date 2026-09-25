import { get, put, post } from './request'

export function fetchStatus(userId = 'default') {
  return get(`/claw/status?user_id=${encodeURIComponent(userId)}`)
}

export function fetchPersonality(userId = 'default') {
  return get(`/claw/personality?user_id=${encodeURIComponent(userId)}`)
}

export function updatePersonality(traits, userId = 'default') {
  return put('/claw/personality', { user_id: userId, traits })
}

export function fetchGrowthLog(userId = 'default', limit = 20) {
  return get(`/claw/growth-log?user_id=${encodeURIComponent(userId)}&limit=${limit}`)
}

export function updatePreferences(prefs, userId = 'default') {
  return post('/claw/preferences', { user_id: userId, preferences: prefs })
}