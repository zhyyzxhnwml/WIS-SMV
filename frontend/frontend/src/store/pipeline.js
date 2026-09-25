/**
 * store/pipeline.js — 四个阶段页共享的流水线数据
 *
 * 累积叠加要求后面每一步都能拿到前面几步的数据：
 *   「清洗后」要能看到被剔除的原始点（叠一层淡化的原始轨迹）
 *   「级联判断后」要能看到清洗结果（再叠一层），才谈得上"只做加法"
 * 这些接口服务端本来就带缓存，但前端每页各拉一遍既慢、又可能各页拿到的
 * 不是同一份数据（分析写入后指纹变了），所以这里按 promise 记忆 ——
 * 一次会话内每个接口只拉一次。
 *
 * 需要强制重取时调 resetPipeline()（例如用户点了「刷新」按钮）。
 */
import { fetchRawTracks, fetchCascadeTracks } from '../api/ships'
import { get } from '../api/request'

let cache = {}

function memo(key, fn) {
  if (!cache[key]) {
    // 失败时把缓存清掉，否则一次网络抖动会让整个会话都拿不到数据
    cache[key] = fn().catch(err => { cache[key] = null; throw err })
  }
  return cache[key]
}

/** 原始数据：每船 rawTrack（带 quality flags）+ 船舶单位清洗统计。 */
export function getRawTracks() { return memo('raw', fetchRawTracks) }

/**
 * 清洗后：/ships/all-tracks，每船 track（保留点，带 tags / 陆地标记）。
 *
 * 这里刻意不用 /ships：那个接口只返回 mmsi/速度这类摘要，**不带 track 字段**
 * （画不出线），而且 pageSize 默认 50，一次只给前 50 条船。画轨迹必须走这个。
 */
export function getAllTracks() { return memo('all', () => get('/ships/all-tracks')) }

/** 级联判断后：每船 track + l1/l2/l3。 */
export function getCascadeTracks() { return memo('cascade', fetchCascadeTracks) }

export function resetPipeline() { cache = {} }

/** 把 ships 数组按 mmsi 建成索引，便于跨接口按船取数据。 */
export function byMmsi(ships) {
  const m = new Map()
  for (const s of ships || []) m.set(String(s.mmsi), s)
  return m
}
