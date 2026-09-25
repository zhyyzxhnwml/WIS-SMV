const BASE_URL = '/api/v1'

async function request(url, options = {}) {
  const resp = await fetch(`${BASE_URL}${url}`, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options
  })
  // 检查响应类型，防止后端返回HTML
  const contentType = resp.headers.get('content-type') || ''
  if (!contentType.includes('application/json') && !contentType.includes('text/json')) {
    const text = await resp.text()
    console.error('API返回非JSON:', text.slice(0, 200))
    throw new Error('后端返回格式异常，请检查后端服务')
  }
  const result = await resp.json()
  if (result.code !== 200) {
    throw new Error(result.message || '请求失败')
  }
  return result.data
}

export function get(url) {
  return request(url)
}

export function post(url, data) {
  return request(url, { method: 'POST', body: JSON.stringify(data) })
}

export function put(url, data) {
  return request(url, { method: 'PUT', body: JSON.stringify(data) })
}

export function del(url) {
  return request(url, { method: 'DELETE' })
}
