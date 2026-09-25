/**
 * GIS工具模块 - 高德地图JS API加载器
 *
 * 使用高德地图需要先注册获取 Key 和 Security Key：
 * 1. 登录 https://lbs.amap.com/
 * 2. 创建应用 → 选择"Web端(JS API)"
 * 3. 获取 Key 和 安全密钥
 *
 * 你的密钥配置：
 *   Key: 你的Key
 *   Security Key: 0f61081cb76e9dc9de1e16b22bd64a05
 *
 * 配置方式（二选一）：
 * 方式一：在 index.html 中引入（推荐）
 *   <script src="https://webapi.amap.com/maps?v=2.0&key=你的Key"></script>
 *
 * 方式二：动态加载（本文件使用的方式）
 *
 * 注意：JSAPI 2.0 的 maps?v=2.0 只能加载一次。此前这里额外插了第二个
 * `&plugin=...` 的同类 script，会把 window.AMap 重建一遍、覆盖掉首次初始化好的
 * 模块注册表，导致 WebGL 图层（TileLayer.Satellite）时而抛
 * "U.Module.WebGLRender is not a constructor"，地图整片空白——且因为是两个脚本
 * 谁先返回的竞态，表现为随机白屏。插件请用 AMap.plugin() 按需加载。
 */

const AMAP_KEY = '77d7e4dd0385c2ae3b2640eead8dde70'
const AMAP_SECRET = '0f61081cb76e9dc9de1e16b22bd64a05'

let amapLoaded = false
let loadPromise = null

/**
 * 动态加载高德地图 JS API
 * @returns {Promise} 返回 AMap 对象
 */
export function loadAMap() {
  if (window.AMap) {
    amapLoaded = true
    return Promise.resolve(window.AMap)
  }

  if (loadPromise) return loadPromise

  // 安全密钥必须在 script 加载之前挂到 window 上，否则高德不认（2.0 的约定）
  window._AMapSecurityConfig = { securityJsCode: AMAP_SECRET }

  loadPromise = new Promise((resolve, reject) => {
    // 只加载一次 maps?v=2.0（重复加载会重建 window.AMap 并清掉已初始化的模块）
    const script = document.createElement('script')
    script.src = `https://webapi.amap.com/maps?v=2.0&key=${AMAP_KEY}`
    script.async = true
    script.onload = () => {
      amapLoaded = true
      resolve(window.AMap)
    }
    script.onerror = () => {
      // 失败后清掉缓存的 promise，否则整场会话都拿不到 AMap，地图永久打不开
      loadPromise = null
      reject(new Error('高德地图加载失败，请检查网络和 Key 配置'))
    }
    document.head.appendChild(script)
  })

  return loadPromise
}

/**
 * 创建船舶标记的 DOM 元素
 * @param {string} color 颜色
 * @param {string} status 状态（violating时增加动画）
 * @returns {HTMLElement}
 */
export function createShipMarkerElement(color, status = 'normal') {
  const div = document.createElement('div')
  div.style.cssText = `
    width: 14px; height: 14px; border-radius: 50%;
    background: ${color}; border: 2px solid #fff;
    box-shadow: 0 1px 4px rgba(0,0,0,0.3);
    ${status === 'violating' ? 'animation: pulse 1.5s infinite;' : ''}
  `
  return div
}

/**
 * 构建船舶信息窗内容
 * @param {Object} ship 船舶对象
 * @returns {string}
 */
export function buildShipPopupContent(ship) {
  return `
    <div style="font-size:13px;line-height:1.8;min-width:180px;">
      <div style="font-weight:600;font-size:15px;margin-bottom:4px;
                  border-bottom:1px solid #eee;padding-bottom:4px;">
        ${ship.name || '未命名'} <span style="font-size:11px;color:#999;">${ship.type || ''}</span>
      </div>
      <div>MMSI: ${ship.mmsi}</div>
      <div>航速: ${ship.speed} 节</div>
      <div>航向: ${ship.course}°</div>
      <div>状态: ${ship.status || '未知'}</div>
      ${ship.dotColor === 'red'
        ? '<div style="color:#ea4335;font-weight:500;margin-top:4px;">⚠ 违规船舶</div>'
        : ''}
      <div style="margin-top:6px;font-size:11px;color:#999;">
        纬度: ${ship.position?.lat?.toFixed(4)}<br>
        经度: ${ship.position?.lng?.toFixed(4)}
      </div>
    </div>
  `
}