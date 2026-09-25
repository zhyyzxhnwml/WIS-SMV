import { get } from './request'

export function fetchStats() {
  return get('/dashboard/stats')
}

export function fetchTrafficTrend() {
  return get('/dashboard/traffic-trend')
}

export function fetchViolationDistribution() {
  return get('/dashboard/violation-distribution')
}

export function fetchAlerts() {
  return get('/dashboard/alerts')
}
