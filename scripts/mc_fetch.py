#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MarineCadastre 日包下载 + 按 bbox 抽取 + 删原包。

为什么不是「先全量下载再处理」：E: 只剩 ~22 GB，全量 154 个日包 ≈ 46 GB。
这里每一步只保留一个日包（274–352 MB），抽完即删，磁盘占用恒定。

两种取日期的方式：
  1) --from-gt   从 HawaiiCoast_GT 真值表的事件时间窗反推需要的日期（推荐）
  2) --dates     手工指定日期

用法：
  python scripts/mc_fetch.py --from-gt                  # 按真值表算日期并下载
  python scripts/mc_fetch.py --from-gt --dry-run        # 只打印将要下载什么
  python scripts/mc_fetch.py --dates 2017-01-09 2017-01-10
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import os
import queue
import shutil
import threading
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent
RAW_ROOT = REPO / "data" / "public" / "marinecadastre"
TMP_DIR = RAW_ROOT / "_tmp"
OUT_DIR = RAW_ROOT / "hawaii"
GT_CSV = REPO / "实验数据" / "HawaiiCoast_GT_GitHub" / \
    "hawaii_primary_trajectories_of_interest_2017_2020.csv"

BASE_URL = "https://coast.noaa.gov/htdata/CMSP/AISDataHandler/{year}/AIS_{year}_{mm:02d}_{dd:02d}.zip"

# 夏威夷主岛链 bbox（含考艾岛到夏威夷大岛），覆盖 HawaiiCoast_GT 全部事件
BBOX = {"lat_min": 18.0, "lat_max": 23.5, "lon_min": -161.5, "lon_max": -154.0}

# 事件时间窗前后各留这么多小时，保证 tracker 有历史、事件尾迹不截断
PAD_HOURS = 2

USER_AGENT = "Mozilla/5.0 (compatible; AIS-research/1.0)"

# 单次 read 的超时。这条链路实测会中途僵死（读到一半连接不动了），
# 设成 60s 是为了让僵死尽快抛异常交给重连逻辑，而不是挂在 1800s 的整包超时上。
STALL_TIMEOUT = 60
# 单个日包最多重连几次。日包 274–352 MB，按重连从断点续，不会重复下载。
MAX_RETRIES = 8


# ---------------------------------------------------------------- GT 日期推导

def dates_from_gt(pad_hours: int = PAD_HOURS, rows: list[dict] | None = None) -> list[str]:
    """读真值表的事件时间窗，反推需要哪几天的日包。"""
    if rows is None:
        if not GT_CSV.exists():
            raise SystemExit(f"找不到真值表：{GT_CSV}")
        with GT_CSV.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))

    need: set[str] = set()
    for row in rows:
        # 只认 AIS 可见窗：report_* 是 USCG 报告的覆盖期，可能横跨数周，
        # 中间绝大多数日子与事件无关，全下会把日期表撑大 2 倍以上。
        lo = (row.get("ais_incident_start_bound_hst") or "").strip()
        hi = (row.get("ais_incident_end_bound_hst") or "").strip()
        d0, d1 = _parse_dt(lo), _parse_dt(hi)
        if d0 is None and d1 is None:
            # AIS 窗缺失时才退回报告期端点
            d0 = _parse_dt((row.get("report_start_date_of_interest_hst") or "").strip())
            d1 = _parse_dt((row.get("report_end_date_of_interest_hst") or "").strip())
        d0 = d0 or d1
        d1 = d1 or d0
        if d0 is None:
            continue
        start = d0 - timedelta(hours=pad_hours)
        end = d1 + timedelta(hours=pad_hours)
        cur = start.replace(hour=0, minute=0, second=0, microsecond=0)
        last = end.replace(hour=0, minute=0, second=0, microsecond=0)
        while cur <= last:
            need.add(cur.strftime("%Y-%m-%d"))
            cur += timedelta(days=1)
    return sorted(need)


def sample_incidents(n: int, seed: int = 20261013) -> list[dict]:
    """按 (事件类型 × 证据强度) 分层抽样 n 起事件。

    为什么不是直接取前 n 起：真值表里 Irregular tow 占 76/208 行，直接截断会
    得到一个几乎单一类别的基准，而"级联系统能否区分违规类型"正是要评的东西。
    分层后每个格子都有代表，类别分布可控，也让后续按类报指标有意义。
    """
    import random
    if not GT_CSV.exists():
        raise SystemExit(f"找不到真值表：{GT_CSV}")

    strata: dict[tuple[str, str], list[dict]] = {}
    with GT_CSV.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = ((row.get("ais_incident_type") or "?").strip(),
                   (row.get("ais_incident_evidence") or "?").strip())
            strata.setdefault(key, []).append(row)

    rng = random.Random(seed)
    total = sum(len(v) for v in strata.values())

    # 按层大小降序，优先保证大层入场——它们代表数据里的主要现象。
    # （原实现是 sorted(strata) 后 picks[:n]，于是抽到谁取决于层名的字母序，
    #  换个命名就会抽到完全不同的基准。）
    keys = sorted(strata, key=lambda k: -len(strata[k]))

    if n <= len(keys):
        # 名额不够覆盖所有层：取最大的 n 层各 1 条
        alloc = {k: 1 for k in keys[:n]}
    else:
        # 先每层 1 条保证覆盖，余量按层大小加权（最大余数法，避免取整丢名额）
        alloc = {k: 1 for k in keys}
        extra = n - len(keys)
        quota = {k: extra * len(strata[k]) / total for k in keys}
        for k in keys:
            alloc[k] += int(quota[k])
        rem = n - sum(alloc.values())
        for k in sorted(keys, key=lambda k: -(quota[k] - int(quota[k])))[:max(rem, 0)]:
            alloc[k] += 1

    # 分配额不能超过该层实际存量，否则总和会悄悄少于 n——
    # 而 n 直接决定下载量，少几起事件意味着少下几天数据，事后很难发现。
    for k in list(alloc):
        alloc[k] = min(alloc[k], len(strata[k]))
    short = n - sum(alloc.values())
    if short > 0:
        room = sorted((k for k in keys if len(strata[k]) > alloc.get(k, 0)),
                      key=lambda k: -(len(strata[k]) - alloc.get(k, 0)))
        for k in room[:short]:
            alloc[k] = alloc.get(k, 0) + 1

    picked: list[dict] = []
    for k in keys:
        take = alloc.get(k, 0)
        if not take:
            continue
        rng.shuffle(strata[k])
        picked.extend(strata[k][:take])
    return picked


def _parse_dt(s: str) -> datetime | None:
    """解析 GT 里的时间串，**统一返回 UTC 的 naive datetime**。

    为什么必须先试 fromisoformat：GT 的 ais_incident_*_bound_hst 全是
    `2017-01-07 23:58:51-10:00` 这种带偏移的 ISO 串，下面那张 strptime 格式表
    **一条都匹配不上**（表里没有能吃掉 `-10:00` 后缀的格式），于是每行都返回 None，
    dates_from_gt 就静默掉进 fallback 分支改用 report_* 日期 —— 那既是 HST 日、
    又可能横跨数周，算出来的日包列表与真正需要的差一天且大量无关。
    MarineCadastre 的日包按 **UTC 日** 归档，所以这里带偏移的串一律换算到 UTC。
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = None
    if dt is not None:
        # 带偏移 → 换到 UTC；纯日期串（report_* 是 HST 日期）保持原样，
        # 别硬当 UTC 换算，否则会平白错开一天。
        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- 下载 + 抽取

THREADS = 16  # 并发连接数，见 download() 里关于拐点的实测
CHUNK = 1 << 20  # 每块的字节数（1 MB）


def _probe_size(url: str) -> int:
    """探测文件总字节数（Range 0-0，只取 1 字节）。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=STALL_TIMEOUT) as resp:
        cr = resp.headers.get("Content-Range") or ""
        if "/" in cr:
            return int(cr.rsplit("/", 1)[1])
        return int(resp.headers.get("Content-Length") or 0)


def download(date_str: str, threads: int = THREADS) -> Path:
    """分块并发下载单个日包到 _tmp，返回路径。已存在则跳过（断点续跑）。

    【为什么要多线程】
      实测这条链路**按单连接限速**，不是总带宽受限：
        1 线程 32.6 KB/s → 6 线程 138.6 → 12 线程 323.6 → 20 线程 339.9 KB/s
      拐点在 12 附近（20 线程几乎不涨）。12 线程就是 **10×**：
      一个 296 MB 的日包从 2.6 小时降到 **15.6 分钟**。
      按单线程估的"60 天要下 8 天"因此作废，实际约 15.6 小时。
      所以这里不设 1 线程的退路——单线程估出来的时间会把整个实验计划带偏。

    【续跑为什么靠 .done 而不是文件大小】
      分了块之后文件大小不再代表完整性：后面的块先落盘，文件就已经是全长，
      中间可能还是空洞。所以另存一个 .part.done 记录**已完成块的序号**，
      关机后续跑只补缺的块。
    """
    d = datetime.strptime(date_str, "%Y-%m-%d")
    url = BASE_URL.format(year=d.year, mm=d.month, dd=d.day)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    dst = TMP_DIR / f"AIS_{d.year}_{d.month:02d}_{d.day:02d}.zip"

    # 已完成的日包留一个 .ok 标记：只凭体积判断会把「半截文件」误当成已完成，
    # 整批下载里中断是常态，这个标记决定续跑是否可信。
    if dst.exists() and dst.with_suffix(".zip.ok").exists():
        print(f"  [跳过] 已完成 {dst.name} ({dst.stat().st_size/1e6:.0f} MB)")
        return dst

    part = dst.with_suffix(".zip.part")
    # 注意用 ".part.done" 而不是 ".zip.part.done"：part 已经叫 xxx.zip.part，
    # with_suffix 是**替换**后缀，传 ".zip.part.done" 会得到 xxx.zip.zip.part.done。
    done_f = part.with_suffix(".part.done")
    t0 = time.time()

    total = _probe_size(url)
    if not total:
        raise RuntimeError(f"{dst.name} 无法探测长度（服务端未返回 Content-Range）")
    n_chunks = (total + CHUNK - 1) // CHUNK
    # 续跑前对齐 .part 与 .done：两者不同步会让"已完成的块"与文件内容错位，
    # 结果是写出一份长度正确、中间是空洞的包——解压时才炸，且极难回溯。
    # 两种失同步：.part 被删但 .done 还在；.part 尺寸不等于 total（上一次是别的 URL
    # 或旧版单线程下载留下的残骸）。任一命中就丢弃 .done 从头下。
    part.touch(exist_ok=True)
    if part.stat().st_size != total:
        if done_f.exists():
            print(f"  [续跑作废] {part.name} 尺寸 {part.stat().st_size} ≠ {total}，"
                  f"丢弃块记录重下", flush=True)
            done_f.unlink()
        with part.open("r+b") as f:  # 截到准确长度，避免残留尾部字节污染 zip
            f.truncate(total)

    done: set[int] = set()
    if done_f.exists():
        done = {int(x) for x in done_f.read_text(encoding="utf-8").split() if x.strip()}

    todo = [i for i in range(n_chunks) if i not in done]
    if not todo:
        pass  # 全在下文统一收尾
    else:
        print(f"  [下载] {dst.name}  {total/1e6:.0f} MB / {n_chunks} 块  "
              f"待下 {len(todo)} 块 × {threads} 线程"
              + (f"（续跑，已完成 {len(done)} 块）" if done else ""), flush=True)

    # 进度：只在主线程累加，块内计数不带锁——用一个整数和一把锁，开销可忽略
    lock = threading.Lock()
    state = {"bytes": len(done) * CHUNK, "fail": 0}
    q = queue.Queue()
    for i in todo:
        q.put(i)
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            try:
                idx = q.get_nowait()
            except queue.Empty:
                return
            a = idx * CHUNK
            b = min(a + CHUNK, total) - 1
            for attempt in range(MAX_RETRIES):
                try:
                    req = urllib.request.Request(
                        url, headers={"User-Agent": USER_AGENT,
                                      "Range": f"bytes={a}-{b}"})
                    with urllib.request.urlopen(req, timeout=STALL_TIMEOUT) as resp:
                        buf = resp.read()
                    if len(buf) != b - a + 1:
                        raise IOError(f"块 {idx} 长度不符 {len(buf)}/{b-a+1}")
                    with part.open("r+b") as out:  # 各线程写互不重叠的区间
                        out.seek(a)
                        out.write(buf)
                    with done_f.open("a", encoding="utf-8") as df:
                        df.write(f"{idx}\n")  # 追加即落盘，关机最多丢这一块的记录
                    with lock:
                        state["bytes"] += len(buf)
                    break
                except Exception as e:  # noqa: BLE001 — 单块失败要重试，不能拖垮整批
                    if attempt == MAX_RETRIES - 1:
                        with lock:
                            state["fail"] += 1
                        print(f"  [块失败] {dst.name} 块 {idx} — "
                              f"{type(e).__name__}: {e}", flush=True)
                        return
                    time.sleep(min(2 ** attempt, 15))

    ts = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
    for t in ts:
        t.start()
    # 主线程每 30 秒报一次进度：15.6 分钟一天，没有进度就分不清"在下"和"僵死"
    while any(t.is_alive() for t in ts):
        time.sleep(30)
        el = time.time() - t0
        got = state["bytes"]
        print(f"    {got/1e6:6.0f}/{total/1e6:.0f} MB  {el:5.0f}s  "
              f"{got/1024/max(el, 1e-6):5.0f} KB/s", flush=True)
    for t in ts:
        t.join()

    got = state["bytes"]
    if got < total:
        raise RuntimeError(f"{dst.name} 未下完：{got}/{total}"
                           f"（失败块 {state['fail']}）—— 重跑本命令即可续传")
    part.replace(dst)
    done_f.unlink(missing_ok=True)
    dst.with_suffix(".zip.ok").write_text(str(total), encoding="utf-8")
    dt = time.time() - t0
    print(f"  [下载] {dst.name}  {total/1e6:.0f} MB  {dt:.0f}s  "
          f"({total/1024/max(dt, 1e-6):.0f} KB/s)")
    return dst


def extract_bbox(zip_path: Path, date_str: str) -> int:
    """从日包里抽出 bbox 内的行，写成 gzip CSV。返回保留行数。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dst = OUT_DIR / f"{date_str}.csv.gz"
    kept = 0
    total = 0

    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise RuntimeError(f"{zip_path.name} 里没有 CSV：{zf.namelist()}")
        with zf.open(names[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
            rdr = csv.DictReader(text)
            fields = rdr.fieldnames or []
            with gzip.open(dst, "wt", encoding="utf-8", newline="") as out:
                wtr = csv.DictWriter(out, fieldnames=fields)
                wtr.writeheader()
                for row in rdr:
                    total += 1
                    try:
                        lat = float(row.get("LAT") or "nan")
                        lon = float(row.get("LON") or "nan")
                    except ValueError:
                        continue
                    if (BBOX["lat_min"] <= lat <= BBOX["lat_max"]
                            and BBOX["lon_min"] <= lon <= BBOX["lon_max"]):
                        wtr.writerow(row)
                        kept += 1

    print(f"  [抽取] {date_str}: {kept:,} / {total:,} 行落进 bbox "
          f"→ {dst.name} ({dst.stat().st_size/1e6:.1f} MB)")
    return kept


def run(dates: list[str], dry_run: bool = False, keep_zip: bool = False,
        threads: int = THREADS) -> None:
    print(f"共 {len(dates)} 个日期，估计 {len(dates)*0.3:.0f} GB 下载量")
    print(f"夏威夷 bbox: {BBOX}")
    if dry_run:
        for d in dates:
            print("  ", d)
        return

    free0 = shutil.disk_usage(REPO).free
    print(f"起始磁盘余量: {free0/1e9:.1f} GB\n")

    ok = fail = 0
    for i, d in enumerate(dates, 1):
        print(f"[{i}/{len(dates)}] {d}")
        zp = None
        try:
            zp = download(d, threads=threads)
            extract_bbox(zp, d)
            if not keep_zip:
                zp.unlink(missing_ok=True)
            ok += 1
        except urllib.error.HTTPError as e:
            print(f"  [失败] HTTP {e.code}（该日可能无数据）")
            fail += 1
        except Exception as e:  # noqa: BLE001 — 单个日期失败不该中断整批
            print(f"  [失败] {type(e).__name__}: {e}")
            fail += 1
            # 抽取失败时也要把原包删掉：E: 只剩 22 GB，若 73 个日包（每个 ~300 MB）
            # 因为某种系统性原因全部抽取失败，攒下来的包正好把磁盘填满，
            # 而那时脚本还在跑，写盘失败会把后面的日期一起带崩。
            # 同时撤掉 .ok 标记——它只表示"包下载完整"，抽取没成功就不该让续跑跳过。
            if zp is not None and not keep_zip:
                zp.unlink(missing_ok=True)
                zp.with_suffix(".zip.ok").unlink(missing_ok=True)

    free1 = shutil.disk_usage(REPO).free
    print(f"\n完成：成功 {ok}，失败 {fail}")
    print(f"磁盘余量 {free0/1e9:.1f} GB → {free1/1e9:.1f} GB")
    print(f"抽取结果在 {OUT_DIR}")


def main() -> None:
    ap = argparse.ArgumentParser(description="MarineCadastre 日包下载 + bbox 抽取")
    ap.add_argument("--dates", nargs="*", default=None, help="YYYY-MM-DD ...")
    ap.add_argument("--from-gt", action="store_true", help="从 HawaiiCoast_GT 反推日期")
    ap.add_argument("--sample", type=int, default=None,
                    help="分层抽 N 起事件（按 事件类型×证据强度），而非全量 154 起")
    ap.add_argument("--dry-run", action="store_true", help="只列日期，不下载")
    ap.add_argument("--keep-zip", action="store_true", help="保留原始日包（占磁盘）")
    ap.add_argument("--pad-hours", type=int, default=PAD_HOURS, help="事件窗前后padding")
    ap.add_argument("--threads", type=int, default=THREADS,
                    help=f"并发连接数（默认 {THREADS}）。实测：1→32.6 KB/s、"
                         f"12→324、20→340、32→497 KB/s，按单连接限速")
    args = ap.parse_args()

    if args.sample:
        rows = sample_incidents(args.sample)
        dates = dates_from_gt(args.pad_hours, rows)
        from collections import Counter
        dist = Counter((r.get("ais_incident_type") or "?").strip() for r in rows)
        print(f"分层抽样 {len(rows)} 起事件（全量 154 起）：")
        for k, v in dist.most_common():
            print(f"  {v:3d}  {k}")
        print()
    elif args.from_gt:
        dates = dates_from_gt(args.pad_hours)
    elif args.dates:
        dates = sorted(args.dates)
    else:
        ap.error("需要 --from-gt 或 --dates")

    run(dates, dry_run=args.dry_run, keep_zip=args.keep_zip, threads=args.threads)


if __name__ == "__main__":
    main()
