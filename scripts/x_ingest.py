#!/usr/bin/env python3
"""X 舆情采集落盘器：把采集器（scripts/x_collect.py，走 opencli）产出的原始
JSON 归一成契约文件。

采集与落盘分离的原因：采集要处理登录态与频控（外部依赖，会变）；归一去重、
原子写、状态登记是确定性工作，放在这里保证「哪次抓了什么」永远可复现、可验收。
任何采集通道（opencli / 未来的别的）只要按下面的原始格式交稿即可。

契约（2026-11-10 方案确认）：

    data/x_posts/<北京日期>/<TICKER>.json   每票一个文件
    data/x_posts/<北京日期>/_status.json    当日采集状态汇总

用法：

    # 正常落盘（raw 由采集 Agent 写出）
    python3 scripts/x_ingest.py --date 2026-11-10 --ticker META --raw raw/META.json

    # 采集失败（登录墙/封禁），不写帖子文件，只登记状态
    python3 scripts/x_ingest.py --date 2026-11-10 --ticker META \\
        --status login_required --note "x.com 跳登录页"

    # 保留期清理（默认 7 天，含今天）
    python3 scripts/x_ingest.py --prune --keep-days 7

原始 JSON 两种形态都接受：
  1) {"ticker","collected_at","slices":[{name,requested,returned,posts:[...]}],"status"}
  2) [ {tweet_id,url,author,text,slice,...}, ... ]   # 扁平列表，切片由条目自述
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("x_ingest")

CST = timezone(timedelta(hours=8))
DEFAULT_ROOT = Path("data/x_posts")
KEEP_DAYS = 7

VALID_STATUS = ("ok", "partial", "empty", "login_required", "failed")

_TWEET_URL_RE = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/[^/]+/status/(\d+)")


# ------------------------------------------------------------------ 工具 --


def ticker_of(stock_code: str) -> str:
    """META.US → META；BRK.B.US → BRK.B；600519.SH → 600519。

    必须与 `src/x_sources.ticker_of` 逐字同口径：落盘文件名是按这里算的，
    读取是按那里算的，两边差一个字符，数据就写进去读不出来。
    """
    code = (stock_code or "").upper().strip()
    for suffix in (".US", ".SH", ".SZ", ".HK"):
        if code.endswith(suffix):
            return code[: -len(suffix)]
    return code


def _as_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def post_identity(post: dict[str, Any]) -> tuple[str, str]:
    """返回 (tweet_id, url)，二者尽量都取到，用于跨切片去重。"""
    url = str(post.get("url") or "").strip()
    tid = str(post.get("tweet_id") or "").strip()
    if not tid and url:
        m = _TWEET_URL_RE.match(url)
        if m:
            tid = m.group(1)
    return tid, url


def engagement(post: dict[str, Any]) -> int:
    m = post.get("metrics") if isinstance(post.get("metrics"), dict) else {}
    like = _as_int(m.get("like", post.get("like_count")))
    reply = _as_int(m.get("reply", post.get("comment_count")))
    repost = _as_int(m.get("repost", post.get("forward_count")))
    return like + reply + repost


def _now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: Any) -> None:
    """先写 .tmp 再 rename：健康检查永远看不到半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


# ------------------------------------------------------------ 原始数据归整 --


def parse_raw(raw: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """原始 JSON → (posts, slices 元数据, 声明状态)。

    posts 保留采集侧字段原样，不做清洗——诈骗/过短/前缀去重已在
    src/x_sources.clean_posts 做过一次，这里只负责「跨切片同一条」的合并，
    避免同一推文在 top 与 min_faves 里各占一个 prompt 名额。
    """
    slices: list[dict[str, Any]] = []
    declared = ""

    if isinstance(raw, dict):
        declared = str(raw.get("status") or "").strip()
        raw_slices = raw.get("slices")
        posts: list[dict[str, Any]] = []
        if isinstance(raw_slices, list):
            for s in raw_slices:
                if not isinstance(s, dict):
                    continue
                name = str(s.get("name") or s.get("slice") or "").strip()
                items = s.get("posts") or []
                if not isinstance(items, list):
                    items = []
                got = 0
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    it = dict(it)
                    if name and not it.get("slice"):
                        it["slice"] = name
                    posts.append(it)
                    got += 1
                slices.append(
                    {
                        "name": name,
                        "requested": _as_int(s.get("requested")) or None,
                        "returned": got,
                    }
                )
        else:
            items = raw.get("posts") or []
            posts = [dict(it) for it in items if isinstance(it, dict)]
    elif isinstance(raw, list):
        posts = [dict(it) for it in raw if isinstance(it, dict)]
    else:
        raise ValueError(f"原始 JSON 结构不支持: {type(raw).__name__}")

    if not slices:
        # 扁平列表：切片由条目自述，回填计数
        counts: dict[str, int] = {}
        for p in posts:
            n = str(p.get("slice") or "unknown")
            counts[n] = counts.get(n, 0) + 1
        slices = [
            {"name": n, "requested": None, "returned": c} for n, c in counts.items()
        ]

    return posts, slices, declared


def dedupe_across_slices(posts: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """同一推文被多个切片抓到时只留互动最高的一条。返回 (posts, 丢弃数)。"""
    best: dict[str, dict[str, Any]] = {}
    ordered: list[str] = []
    total = 0
    for p in posts:
        total += 1
        tid, url = post_identity(p)
        key = tid or url
        if not key:
            key = f"anon-{len(ordered)}"
        if key not in best:
            best[key] = p
            ordered.append(key)
            continue
        # 同一条：留互动高的；同分则留先到的（top 流优先于 live）
        if engagement(p) > engagement(best[key]):
            best[key] = p
    deduped = [best[k] for k in ordered]
    return deduped, total - len(deduped)


# 历史/外部采集器可能沿用的状态别名 → 本模块口径
_STATUS_ALIASES = {"blocked": "failed", "error": "failed", "banned": "failed"}


def derive_status(
    posts: list[dict[str, Any]],
    declared: str,
    slices: list[dict[str, Any]] | None = None,
) -> str:
    """定状态。三条规则，按优先级：

    1. 采集器显式声明失败态（login_required / failed / blocked / error）→ 照用。
       失败态不能因为「恰好有几条帖」被降级成 ok，否则封号/登录墙会静默。
    2. 有帖但**任一纯采集切片**报错或 0 返回 → partial（数据不全，下游要知道）。
    3. 其余：有帖 ok，无帖 empty。
    """
    declared = (declared or "").strip().lower()
    declared = _STATUS_ALIASES.get(declared, declared)
    if declared in VALID_STATUS and declared not in ("ok", "empty"):
        return declared
    if not posts:
        return "empty"
    for s in slices or []:
        if not isinstance(s, dict):
            continue
        if s.get("error") or (s.get("requested") and not s.get("returned")):
            return "partial"
    return "ok"


# -------------------------------------------------------------- 状态文件 --


def update_status(
    root: Path,
    date_str: str,
    ticker: str,
    *,
    status: str,
    posts: int,
    slices: list[dict[str, Any]] | None = None,
    collected_at: str = "",
    note: str = "",
) -> dict[str, Any]:
    path = root / date_str / "_status.json"
    payload: dict[str, Any] = {}
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"状态文件损坏，重建: {type(e).__name__}: {e}")
            payload = {}
    payload.setdefault("date", date_str)
    tickers = payload.setdefault("tickers", {})
    tickers[ticker] = {
        "status": status,
        "posts": posts,
        "collected_at": collected_at or _now_iso(),
        "slices": {s.get("name"): s.get("returned") for s in (slices or []) if s.get("name")},
        "note": note,
    }
    payload["updated_at"] = _now_iso()
    _atomic_write_json(path, payload)
    return payload


# ------------------------------------------------------------------ 动作 --


def ingest(
    root: Path,
    date_str: str,
    stock_code: str,
    raw_path: Path | None = None,
    *,
    status: str = "",
    note: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    ticker = ticker_of(stock_code)

    if raw_path is None:
        if status not in VALID_STATUS:
            raise ValueError(f"--status 必须是 {VALID_STATUS} 之一")
        if not dry_run:
            update_status(root, date_str, ticker, status=status, posts=0, note=note)
        return {"ticker": ticker, "status": status, "posts": 0, "written": False}

    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    posts, slices, declared = parse_raw(raw)
    posts, dropped = dedupe_across_slices(posts)
    final_status = derive_status(posts, declared or status, slices)
    collected_at = (
        str(raw.get("collected_at") or "") if isinstance(raw, dict) else ""
    ) or _now_iso()

    payload = {
        "ticker": ticker,
        "date": date_str,
        "collected_at": collected_at,
        "status": final_status,
        "slices": slices,
        "posts": posts,
    }
    if not dry_run:
        _atomic_write_json(root / date_str / f"{ticker}.json", payload)
        update_status(
            root,
            date_str,
            ticker,
            status=final_status,
            posts=len(posts),
            slices=slices,
            collected_at=collected_at,
            note=note,
        )
    logger.info(
        f"{ticker} {date_str}: {len(posts)} 条（跨切片去重 {dropped}）"
        f" 切片={ {s['name']: s['returned'] for s in slices} } status={final_status}"
    )
    return {
        "ticker": ticker,
        "status": final_status,
        "posts": len(posts),
        "dropped": dropped,
        "written": not dry_run,
    }


def _config_keep_days(config: Path) -> int | None:
    """从 config.report.json 的 x_source.keep_days 取保留期（取不到返回 None）。"""
    try:
        cfg = json.loads(config.read_text(encoding="utf-8"))
        val = (cfg.get("x_source") or {}).get("keep_days")
        return int(val) if val is not None else None
    except Exception:
        return None


def prune(root: Path, keep_days: int = KEEP_DAYS, dry_run: bool = False) -> list[str]:
    """删掉超出保留期的日期目录（按目录名 YYYY-MM-DD 判定）。

    keep_days 下限为 1（= 只保今天）：`--keep-days 0` 的 cutoff 会落到明天，
    把当天的数据也删掉，这条护栏挡住这种误操作。
    """
    if keep_days < 1:
        raise ValueError(f"keep_days 必须 ≥ 1，收到 {keep_days}")
    removed: list[str] = []
    if not root.exists():
        return removed
    cutoff = (datetime.now(CST) - timedelta(days=keep_days - 1)).date()
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        try:
            day = datetime.strptime(d.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            removed.append(d.name)
            if not dry_run:
                shutil.rmtree(d)
    if removed:
        logger.info(f"清理 {len(removed)} 个过期目录: {removed}")
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="X 舆情采集落盘器")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--date", default="", help="北京日期 YYYY-MM-DD")
    ap.add_argument("--ticker", default="", help="标的，可写 META 或 META.US")
    ap.add_argument("--raw", default="", help="采集器产出的原始 JSON")
    ap.add_argument("--status", default="", help=f"失败态登记: {VALID_STATUS}")
    ap.add_argument("--note", default="")
    ap.add_argument("--prune", action="store_true", help="清理过期日期目录")
    ap.add_argument(
        "--keep-days",
        type=int,
        default=None,
        help=f"保留天数（默认读 config.report.json 的 x_source.keep_days，取不到用 {KEEP_DAYS}）",
    )
    ap.add_argument(
        "--config",
        default="etc/config.report.json",
        help="读取 x_source.keep_days 用；文件不存在则退回内置默认",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )
    root = Path(args.root)

    if args.prune:
        keep = args.keep_days
        if keep is None:
            keep = _config_keep_days(Path(args.config)) or KEEP_DAYS
        try:
            removed = prune(root, keep, args.dry_run)
        except ValueError as e:
            logger.error(str(e))
            return 2
        print(json.dumps({"pruned": removed, "keep_days": keep}, ensure_ascii=False))
        return 0

    if not args.date or not args.ticker:
        ap.error("落盘需要 --date 和 --ticker（或用 --prune）")
    if not args.raw and not args.status:
        ap.error("需要 --raw（正常落盘）或 --status（失败登记）")

    try:
        result = ingest(
            root,
            args.date,
            args.ticker,
            Path(args.raw) if args.raw else None,
            status=args.status,
            note=args.note,
            dry_run=args.dry_run,
        )
    except Exception as e:
        logger.error(f"落盘失败: {type(e).__name__}: {e}")
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
