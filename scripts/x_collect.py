#!/usr/bin/env python3
"""X 舆情采集器：经 opencli 的 twitter 适配器抓 $TICKER 三个切片。

为什么走 opencli 而不是自己驱动浏览器（2026-11-10 实测结论）：
- Chrome 156 的 cookie 已启用 app-bound 加密，复制 profile 或自行解密都拿
  不到登录态（解密结果是乱码），这是浏览器安全机制，不去绕。
- Playwright 新起 profile 需要人工登录一次；headless 又被 x.com 直接 403。
- opencli 的 twitter 适配器**已经带着 11-02 试点用的那个登录态**
  （whoami → paradox0504），一条命令 5~6 秒拿到结构化 JSON，字段含
  id/author/text/created_at/likes/views/url/quoted_tweet。

每只票 3 切片（试点配方 2026-11-02 验证）：
    top          热门流 `$T`                  —— 全景 + 行情卡
    min_faves    高互动流 `$T min_faves:100`  —— 深内容集中层
    live         最新流 `$T` --product live   —— 时效信号，也是噪音层（下游降权）

产出交给 scripts/x_ingest.py 归一落盘，本脚本只写 raw JSON。

用法：
    python3 scripts/x_collect.py --probe
    python3 scripts/x_collect.py --tickers META,GOOGL,RDDT --date 2026-11-10 --ingest
    python3 scripts/x_collect.py --tickers META --slices live --max 10

退出码：0 成功；3 登录态不可用（跑 opencli twitter login）；4 采集失败；2 参数/依赖。
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("x_collect")

CST = timezone(timedelta(hours=8))

# 切片名 → (query, product)
SLICE_QUERIES = {
    "top": ("${t}", "top"),
    "min_faves": ("${t} min_faves:100", "top"),
    "live": ("${t}", "live"),
}


# ------------------------------------------------------------------ opencli --


def _run(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["opencli", *args], capture_output=True, text=True, timeout=timeout
    )


def whoami() -> dict[str, Any]:
    """登录态探针。opencli 输出是 `k: v` 形式的纯文本。"""
    out = _run(["twitter", "whoami"], timeout=90)
    info: dict[str, Any] = {"logged_in": False, "raw": (out.stdout or out.stderr)[:300]}
    for line in (out.stdout or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    info["logged_in"] = str(info.get("logged_in", "")).lower() == "true"
    return info


def search(query: str, product: str, limit: int) -> list[dict[str, Any]]:
    out = _run(
        ["twitter", "search", query, "--product", product, "--limit", str(limit), "-f", "json"],
        timeout=240,
    )
    text = (out.stdout or "").strip()
    if not text:
        raise RuntimeError(f"opencli 无输出: {(out.stderr or '')[:200]}")
    # opencli 偶尔在 JSON 前后打提示（更新提醒等），截取第一个 [ 到最后一个 ]
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        raise RuntimeError(f"opencli 输出非 JSON: {text[:200]}")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, list):
        raise RuntimeError("opencli 返回不是数组")
    return data


# ------------------------------------------------------------------ 归一 --


def _iso_utc(created_at: str) -> str:
    """转 ISO UTC。吃得下 opencli 现在给的两种形态，也吃得下带毫秒的 RFC3339。

    - `Mon Nov 09 07:03:37 +0000 2026`（opencli 当前格式）
    - `2026-11-09T07:03:37+0000` / `2026-11-09T07:03:37.000Z`（适配器换编码器时）

    转不出来就返回空串；空串会被 validate 拦下来，不会静默当"今天"。
    """
    s = (created_at or "").strip()
    if not s:
        return ""
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    try:  # 带毫秒的 RFC3339：fromisoformat 直接吃（3.11+ 支持 Z 后缀）
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return ""


def _to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def to_post(item: dict[str, Any], slice_name: str) -> dict[str, Any] | None:
    text = (item.get("text") or "").strip()
    tid = str(item.get("id") or "").strip()
    if not text or not tid:
        return None
    likes = _to_int(item.get("likes"))
    views = _to_int(item.get("views"))
    metrics: dict[str, int] = {}
    if likes is not None:
        metrics["like"] = likes
    if views is not None:
        metrics["views"] = views
    author = str(item.get("author") or "").strip()
    return {
        "tweet_id": tid,
        "url": f"https://x.com/{author or 'i'}/status/{tid}",
        "author": author,
        # opencli 搜索结果不带展示名，只有 handle；保留 handle 即可定位作者
        "text": text,
        "lang": str(item.get("lang") or ""),
        "slice": slice_name,
        "created_at_utc": _iso_utc(str(item.get("created_at") or "")),
        "metrics": metrics,
        # opencli 不给 reply/repost，只有 like/views：like 可信，标 partial=False；
        # 完全没有数字时才标 partial，让下游降权而不是当 0 互动。
        "metrics_partial": likes is None,
        "quoted_tweet_id": (item.get("quoted_tweet") or {}).get("id") if isinstance(item.get("quoted_tweet"), dict) else None,
    }


def collect_one(ticker: str, slice_name: str, want: int) -> dict[str, Any]:
    q, product = SLICE_QUERIES[slice_name]
    query = q.replace("${t}", f"${ticker}")
    logger.info(f"  {ticker}/{slice_name}: opencli twitter search {query!r} --product {product}")
    try:
        raw = search(query, product, want)
    except Exception as e:
        logger.error(f"  {ticker}/{slice_name} 失败: {type(e).__name__}: {str(e)[:160]}")
        return {"name": slice_name, "requested": want, "returned": 0, "posts": [], "error": str(e)[:200]}

    posts, skipped = [], 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        p = to_post(item, slice_name)
        if p:
            posts.append(p)
        else:
            skipped += 1
    posts = posts[:want]
    logger.info(f"  → {len(posts)} 条（无正文跳过 {skipped}）")
    return {"name": slice_name, "requested": want, "returned": len(posts), "posts": posts}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="X 舆情采集器（opencli twitter）")
    ap.add_argument("--tickers", default="", help="逗号分隔，如 META,GOOGL,RDDT")
    ap.add_argument("--date", default="", help="北京日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--slices", default="top,min_faves,live")
    ap.add_argument("--max", type=int, default=25, help="每切片上限")
    ap.add_argument("--raw-dir", default="", help="raw JSON 输出目录")
    ap.add_argument("--probe", action="store_true", help="只检查登录态")
    ap.add_argument("--ingest", action="store_true", help="采集后直接调 x_ingest 落盘")
    ap.add_argument("--root", default="data/x_posts")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    date_str = args.date or datetime.now(CST).strftime("%Y-%m-%d")
    raw_dir = Path(args.raw_dir) if args.raw_dir else Path(tempfile.mkdtemp(prefix="xraw-"))
    raw_dir.mkdir(parents=True, exist_ok=True)

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    slices = [s.strip() for s in args.slices.split(",") if s.strip()]
    for s in slices:
        if s not in SLICE_QUERIES:
            ap.error(f"未知切片 {s}，可选 {list(SLICE_QUERIES)}")

    me = whoami()
    logger.info(f"opencli 登录态: {me.get('logged_in')} ({me.get('username', '?')})")
    if not me["logged_in"]:
        print(json.dumps({"login_ok": False, "hint": "opencli twitter login"}, ensure_ascii=False))
        return 3
    if args.probe:
        print(json.dumps({"login_ok": True, "username": me.get("username")}, ensure_ascii=False))
        return 0
    if not tickers:
        ap.error("采集需要 --tickers（或用 --probe）")

    results: dict[str, dict[str, Any]] = {}
    failed_slices = 0
    for ticker in tickers:
        slices_out = []
        for i, s in enumerate(slices):
            if i:
                time.sleep(random.uniform(4, 7))  # 频控：切片之间必停
            sl = collect_one(ticker, s, args.max)
            if sl.get("error"):
                failed_slices += 1
            slices_out.append(sl)
        total = sum(sl["returned"] for sl in slices_out)
        logger.info(f"{ticker}: 三切片共 {total} 条")
        payload = {
            "ticker": ticker,
            "date": date_str,
            "collected_at": datetime.now(CST).isoformat(timespec="seconds"),
            # 有切片失败但整票还有数据 → partial（下游/健康检查要看得出"少一层流"）；
            # 全切片失败 → failed；全成功但 0 条 → empty。
            "status": (
                "failed"
                if slices_out and failed_slices == len(slices_out)
                else "partial"
                if failed_slices
                else "ok"
                if total
                else "empty"
            ),
            "slices": slices_out,
        }
        out = raw_dir / f"{ticker}.raw.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        results[ticker] = {"raw": str(out), "posts": total}
        time.sleep(random.uniform(5, 9))  # 标的之间也停

    summary = {"date": date_str, "raw_dir": str(raw_dir), "login_ok": True, "results": results}
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.ingest and results:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import x_ingest

        for ticker, info in results.items():
            x_ingest.ingest(Path(args.root), date_str, ticker, Path(info["raw"]))

    all_zero = all(r["posts"] == 0 for r in results.values()) if results else True
    return 4 if all_zero else 0


if __name__ == "__main__":
    sys.exit(main())
