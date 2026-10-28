#!/usr/bin/env python3
"""Nightly health check: verify the last run completed successfully.

Checks:
  1. Last cron.log has [SUMMARY] entry → pipeline finished
  2. Crawl success rate >= 90%
  3. Dectect anomalies (zero posts, high failure rate)

Output: JSON to stdout, suitable for cron log monitoring.

"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
WATCHLIST_PATH = PROJECT_ROOT / "data" / "watchlist.json"

OK = "ok"
WARN = "warn"
FAIL = "fail"

# 「落回文件兜底」的告警在这么久内出现过就告警（见 3.5 节的 alert_delivery 检查）。
UNDELIVERED_ALERT_WINDOW_DAYS = 7

# 「批次还在跑」的判定阈值(秒): 日志在这么久内还被写过才算「运行中」。
# 实测单批次总耗时 11–24 分钟, 中途静默 >15 分钟已属异常 (见 pipeline_completed
# 分支的注释)。
RUNNING_FRESH_SECONDS = 900

# 当日舆情日报产物。2026-10-15: 按 skills 文档的口径, 这份日报在
# **xueqiu-crawler** 项目里 (不是本仓库) —— cron-ops.md:96 明确写了这一点,
# 并记录了「占位/最小日报 = 无真实交付」的判据。跨仓路径允许用环境变量覆盖,
# 便于容器/换机器部署。
DAILY_REPORT_DIR = Path(
    os.environ.get("XUEQIU_DAILY_REPORT_DIR")
    or (PROJECT_ROOT.parent / "xueqiu-crawler" / "data" / "daily_reports")
)


def _get_latest_log() -> Path | None:
    """Get the most recent non-empty log file by modification time.
    Searches across all log types (run_*.log and pipeline*.log) and returns
    the newest one — no type has priority."""
    all_logs = list(LOG_DIR.glob("run_*.log")) + list(LOG_DIR.glob("pipeline*.log"))
    non_empty = [f for f in all_logs if f.stat().st_size > 0]
    if non_empty:
        return max(non_empty, key=lambda f: f.stat().st_mtime)
    return None


def _get_todays_logs() -> list[Path]:
    """当天有写入的全部批次日志,按 mtime 升序(最旧先查)。

    盲区修复(20261009): 旧逻辑只盯 mtime 最新的一个日志,当天多批次组里
    除最新外全部不可见(~5/6 盲区,某组挂掉整晚无人发现)。"""
    today = datetime.date.today()
    out: list[Path] = []
    for f in list(LOG_DIR.glob("run_*.log")) + list(LOG_DIR.glob("pipeline*.log")):
        if f.stat().st_size == 0:
            continue
        if datetime.date.fromtimestamp(f.stat().st_mtime) == today:
            out.append(f)
    out.sort(key=lambda f: f.stat().st_mtime)
    return out


def _load_monitor_whitelist() -> set[str] | None:
    """Union of crawler.whitelist across all group configs (etc/config*.json).

    This is the set of stocks the pipeline is *supposed* to crawl. meta rows
    for decommissioned stocks are leftovers, not health signals. Returns None
    when no config file is readable, so the caller falls back to a full-table
    scan (missing-config environments still get checked)."""
    union: set[str] = set()
    found = False
    for cfg_path in sorted((PROJECT_ROOT / "etc").glob("config*.json")):
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        found = True
        union.update(cfg.get("crawler", {}).get("whitelist", []) or [])
    return union if found else None


def _get_latest_cron_log() -> Path | None:
    log = LOG_DIR / "cron.log"
    return log if log.exists() else None


def _is_pipeline_running() -> bool:
    """Check if pipeline process is still running."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", r"python.*src\.cli"],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


def _latest_pipeline_window(
    log_text: str,
) -> tuple[datetime.datetime | None, datetime.datetime | None]:
    """Return the newest pipeline's start and completion timestamps.

    Source failures are only meaningful when they occur inside this window.
    Using "since start" alone would also count manual smoke-test failures written
    after the scheduled pipeline had already finished.
    """
    start = None
    end = None
    timestamps: list[datetime.datetime] = []
    timestamp_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    for line in log_text.splitlines():
        match = timestamp_pattern.match(line)
        if not match:
            continue
        timestamp = datetime.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        timestamps.append(timestamp)
        if "从 morning-brief 加载" in line:
            start = timestamp
            end = None
        elif "[SUMMARY]" in line and start is not None and end is None:
            end = timestamp

    if start is not None:
        return start, end

    # Legacy fallback: treat the first and last timestamps as one run window.
    # Note the pattern is anchored with ^ and has no MULTILINE, so it cannot be
    # run over the whole text — every timestamp must be collected line by line.
    if not timestamps:
        return None, None
    return timestamps[0], timestamps[-1]


def _read_source_records_in_window(
    fail_log: Path,
    window: tuple[datetime.datetime | None, datetime.datetime | None],
) -> list[dict]:
    """Read source failure/recovery records inside the latest pipeline run.

    When there is no usable window (no pipeline log yet), fall back to today's
    records — the previous behaviour. Returning [] here would report "no source
    failures" without any evidence, which is the silent-OK failure mode this
    whole check exists to prevent.
    """
    start, end = window
    today = datetime.date.today().isoformat()
    rows: list[dict] = []
    for line in fail_log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            ts = datetime.datetime.fromisoformat(str(rec.get("ts", "")))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if start is not None:
            if ts < start or (end is not None and ts > end):
                continue
        elif not str(rec.get("ts", "")).startswith(today):
            continue
        rows.append(rec)
    return rows


def _read_source_failures_in_window(
    fail_log: Path,
    window: tuple[datetime.datetime | None, datetime.datetime | None],
) -> list[dict]:
    """失败记录；event=recovered 的恢复记录不是失败（record_source_recovery）。"""
    return [
        r for r in _read_source_records_in_window(fail_log, window)
        if r.get("event") != "recovered"
    ]


def _read_source_recoveries_in_window(
    fail_log: Path,
    window: tuple[datetime.datetime | None, datetime.datetime | None],
) -> list[dict]:
    """重试恢复记录（crawler 重试成功后由 record_source_recovery 写入）。"""
    return [
        r for r in _read_source_records_in_window(fail_log, window)
        if r.get("event") == "recovered"
    ]


def check(require_daily_report: bool = False) -> None:
    """跑全部检查, 打 JSON 到 stdout, 并按 status 决定退出码。

    Args:
        require_daily_report: 要求当日舆情日报产物存在且非空 (收尾巡检查这条,
            白天批间巡检时日报尚未生成, 不要传)。
    """
    results = []
    errors = []

    # ── 1. Check DB exists and has data ──
    db_path = PROJECT_ROOT / "data" / "monitor.db"
    if db_path.exists():
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        snapshots = conn.execute("SELECT COUNT(*) FROM crawl_snapshots").fetchone()[0]
        stocks = conn.execute("SELECT COUNT(DISTINCT stock_code) FROM crawl_snapshots").fetchone()[0]
        total_posts = conn.execute("SELECT SUM(posts_count) FROM crawl_snapshots").fetchone()[0] or 0
        conn.close()
        results.append({"check": "db_snapshots", "status": OK, "detail": f"{snapshots} snapshots, {stocks} stocks, {total_posts} posts"})
    else:
        results.append({"check": "db_exists", "status": FAIL, "detail": "data/monitor.db not found"})
        errors.append("db_missing")

    # ── 2. Check TODAY's pipeline logs for [SUMMARY] ──
    logs = _get_todays_logs()
    if not logs:
        log = _get_latest_log()
        logs = [log] if log else []
    # 默认窗口 (None, None)：没有可用日志时，下面 3.5 会退回老的「当天」口径。
    # 必须先赋值 —— _get_latest_log() 可能返回 None，否则 3.5 会 NameError。
    source_window: tuple[datetime.datetime | None, datetime.datetime | None] = (None, None)
    if logs:
        pipeline_active = _is_pipeline_running()
        now_ts = datetime.datetime.now().timestamp()
        with_summary, without_summary = [], []
        unfinished: list[str] = []
        total_skip = total_err = 0
        for idx, lg in enumerate(logs):
            text = lg.read_text(encoding="utf-8", errors="replace")
            if idx == len(logs) - 1:
                source_window = _latest_pipeline_window(text)
            # 每股级失败必须**在 [SUMMARY] 的 continue 之前**累计 ——
            # 否则「批次正常收尾、但里面有股票抓失败」的日志会被整条跳过
            # (2026-10-16 修: 实测 6/8 成功的组, 其 skip/err 一条都数不到),
            # crawl_failures / high_failure_rate / detect_errors 于是只在
            # 「批次根本没跑完」时才可能生效 —— 而那已经是另一个检查的职责了。
            total_skip += len(re.findall(r"\[SKIP\] stock=.+ status=(timeout|failed)", text))
            total_err += len(re.findall(r"\[ERROR\] stock=|CRITICAL", text))
            summary_match = re.search(r"\[SUMMARY\] (.+)", text)
            if summary_match:
                with_summary.append(f"{lg.name}:{summary_match.group(1)[:36]}")
                continue
            # 「运行中」= 这股批次**自己的**日志还在被写。
            # 2026-10-15 (review): 不能只看全局进程 —— 任一批次活着就把当天所有
            # 未收尾日志都算「运行中」的话, 上一组崩溃(日志停在 45 分钟前)会被
            # 同一时段另一组的进程掩盖, gate 从此永久静默放行。所以收紧为
            # 「进程活着 **且** 这股日志 15 分钟内还在写」; 实测单批次总耗时
            # 11–24 分钟, 中途静默 >15 分钟已属异常。
            still_writing = (now_ts - lg.stat().st_mtime) < RUNNING_FRESH_SECONDS
            if pipeline_active and still_writing:
                without_summary.append(f"{lg.name}(运行中)")
            else:
                without_summary.append(lg.name)
                unfinished.append(lg.name)
        if without_summary:
            results.append({
                "check": "pipeline_completed", "status": WARN,
                "detail": f"{len(without_summary)}/{len(logs)} 当日批次无 [SUMMARY]: " + ", ".join(without_summary),
            })
            # 2026-10-15: 只有「确实没跑完」才计错误。「还在跑」是正常态 ——
            # health_check 自己都把它标成「(运行中)」, 计进 errors 会让 status=warn,
            # 而 14:25–18:25 每小时的批间巡检 (同一脚本) 就会把「上一组还没收尾」
            # 当成失败, 每天最多发 5 张红卡片 (实测批次 11–24 分钟, gate 在 :25
            # 跑, g1/g2 这种正好压在边界上)。
            # 停滞判定仍由上面 recent(<1h) 兜住: 进程活着但一小时没写日志 → 不算
            # 「运行中」, 照旧计错误并告警。检查项本身保持 WARN, 卡片照常展示。
            if unfinished:
                errors.append("no_summary")
        else:
            results.append({
                "check": "pipeline_completed", "status": OK,
                "detail": f"{len(with_summary)}/{len(logs)} 当日批次全部有 SUMMARY",
            })
        if total_skip > 0:
            results.append({"check": "crawl_failures", "status": WARN,
                            "detail": f"{total_skip} stocks skipped(当日累计)"})
            if total_skip > 5:
                errors.append("high_failure_rate")
        if total_err > 0:
            results.append({"check": "detect_errors", "status": WARN,
                            "detail": f"{total_err} errors(当日累计)"})
            errors.append("detect_errors")
    else:
        results.append({"check": "log_exists", "status": WARN, "detail": "No run log found"})

    # ── 2.5 当日舆情日报产物 (仅收尾巡检要求) ──
    # 2026-10-15: 之前 cron 巡检 prompt 声称「日报缺失或为空视为失败」, 但脚本
    # 根本不读 sys.argv、也没有这一项 —— `--require-daily-report` 是空转 flag
    # (skills/data-science/xueqiu-monitor/references/cron-ops.md:96), 只能人工
    # 核验 + 手工往卡片表格补行。这里把它做实。
    # 判据抄 cron-ops.md:96-99 的三连: 存在 → 非空 → 不是「今日无新增」占位形态。
    # 白天批间巡检 (14:25–18:25) 不传此参: 那时日报本来还没生成。
    if require_daily_report:
        report_path = DAILY_REPORT_DIR / (datetime.date.today().isoformat() + ".md")
        if not DAILY_REPORT_DIR.is_dir():
            # 跨仓路径不存在 = 本机/容器没配好, 而不是「今天没交付」。报 WARN 并
            # 指路, 免得在没有 crawler 检出的环境里天天红灯。
            results.append({"check": "daily_report", "status": WARN,
                            "detail": f"日报目录不存在: {DAILY_REPORT_DIR}"
                                      f"（用 XUEQIU_DAILY_REPORT_DIR 指定）"})
        else:
            try:
                size = report_path.stat().st_size if report_path.is_file() else None
                head = report_path.read_text(encoding="utf-8", errors="replace")[:2000] if size else ""
            except OSError as exc:
                results.append({"check": "daily_report", "status": FAIL,
                                "detail": f"读取失败: {exc}"})
                errors.append("daily_report_invalid")
            else:
                if size is None:
                    results.append({"check": "daily_report", "status": FAIL,
                                    "detail": f"未生成今日文件 {report_path.name}"})
                    errors.append("daily_report_missing")
                elif size == 0:
                    results.append({"check": "daily_report", "status": FAIL,
                                    "detail": f"{report_path.name} 为空"})
                    errors.append("daily_report_invalid")
                elif "今日无新增" in head:
                    # 「今日无新增」最小日报: 文件在, 但没有真实内容 —— cron-ops.md
                    # 判它是无交付 (crawler PR #54 正是为了让这天仍落一份最小日报)。
                    results.append({"check": "daily_report", "status": FAIL,
                                    "detail": f"{report_path.name} 是「今日无新增」占位形态 "
                                              f"({size} bytes), 无真实内容"})
                    errors.append("daily_report_placeholder")
                else:
                    results.append({"check": "daily_report", "status": OK,
                                    "detail": f"{report_path.name} ({size} bytes)"})

    # ── 3. Check watchlist ──
    if WATCHLIST_PATH.exists():
        wl = json.loads(WATCHLIST_PATH.read_text())
        results.append({"check": "watchlist", "status": OK, "detail": f"{len(wl)} stocks"})
    else:
        results.append({"check": "watchlist", "status": FAIL, "detail": "watchlist.json not found"})
        errors.append("watchlist_missing")

    # ── 3.5 告警投递: 落回文件兜底的告警其实无人消费（2026-10-16） ──
    # `notifier.dispatch_messages_tracked` 在 lark-cli 不可用或发送失败时, 把没发出去
    # 的消息写进 pending 文件(默认 /tmp/xueqiu_monitor_pending.json)并记
    # status='fallback' —— 但**全仓没有任何消费者**读那个文件: 文档写「由外部调度器
    # 读取」, 实际不存在。也就是说 P0 告警的兜底通道是黑洞。
    # (截至 2026-10-16 该路径从未真正走过 —— 库里 fallback 记录为 0 —— 所以这是
    #  潜在缺陷; 但正因为它「只会静默不投递」, 不做成检查项就永远不会被发现。)
    if db_path.exists():
        import sqlite3
        import time as _time
        since = int(_time.time()) - UNDELIVERED_ALERT_WINDOW_DAYS * 86400
        try:
            conn = sqlite3.connect(str(db_path))
            has_tbl = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='push_history'"
            ).fetchone()
            if has_tbl is None:
                # 表还没有 = 这台库还没产生过告警, 不是问题(测试用的临时库也走这条)。
                conn.close()
                results.append({"check": "alert_delivery", "status": OK,
                                "detail": "尚无 push_history 表"})
                n_undelivered = None
            else:
                n_undelivered = conn.execute(
                    "SELECT COUNT(*) FROM push_history "
                    "WHERE status='fallback' AND push_time >= ?",
                    (since,),
                ).fetchone()[0]
                conn.close()
            if n_undelivered:
                results.append({
                    "check": "alert_delivery", "status": WARN,
                    "detail": (f"{n_undelivered} 条告警落回文件兜底但无人消费"
                               f"(近 {UNDELIVERED_ALERT_WINDOW_DAYS} 天)"),
                })
                errors.append("undelivered_alerts")
            else:
                results.append({
                    "check": "alert_delivery", "status": OK,
                    "detail": f"近 {UNDELIVERED_ALERT_WINDOW_DAYS} 天无落回文件兜底的告警",
                })
        except sqlite3.Error as exc:
            results.append({"check": "alert_delivery", "status": WARN,
                            "detail": f"读取失败: {exc}"})

    # ── 3.5 按信息源检查抓取失败（news / notices / discussions / replies / stock）──
    # 之前只统计讨论数，资讯整源挂掉也报 healthy；这里把源级失败显式列出来。
    try:
        fail_log = LOG_DIR / "source_failures.jsonl"
        if fail_log.exists():
            all_failures = _read_source_failures_in_window(fail_log, source_window)
            recoveries = _read_source_recoveries_in_window(fail_log, source_window)
            # 瞬时失败且已重试恢复的不告警（2026-10-26: comments 01138 首拉超时、
            # crawler 30s 重试成功，旧口径照样红卡）。恢复记录按 source+target
            # 与失败配对，只剩「失败且未恢复」才计入。
            rec_keys = {(r.get("source"), r.get("target")) for r in recoveries}
            rows = [
                r for r in all_failures
                if (r.get("source"), r.get("target")) not in rec_keys
            ]
            resolved = [
                r for r in all_failures
                if (r.get("source"), r.get("target")) in rec_keys
            ]
            if rows:
                agg = {}
                for rec in rows:
                    key = (rec.get("source") or "?", rec.get("reason") or "其他")
                    agg[key] = agg.get(key, 0) + 1
                detail = "; ".join(
                    "%s: %d 次（%s）" % (src_name, cnt, why)
                    for (src_name, why), cnt in sorted(agg.items(), key=lambda kv: -kv[1])
                )
                results.append({"check": "source_failures", "status": WARN,
                                "detail": "%d 次失败 → %s" % (len(rows), detail)})
                errors.append("source_failures")
            elif resolved:
                agg = {}
                for rec in resolved:
                    key = (rec.get("source") or "?", rec.get("target") or "?")
                    agg[key] = agg.get(key, 0) + 1
                detail = "; ".join(
                    "%s→%s: %d 次" % (src_name, target, cnt)
                    for (src_name, target), cnt in sorted(agg.items(), key=lambda kv: -kv[1])
                )
                start, end = source_window
                if start is not None:
                    span = start.strftime("%Y-%m-%d %H:%M:%S")
                    if end is not None:
                        span += " → " + end.strftime("%H:%M:%S")
                    results.append({"check": "source_failures", "status": OK,
                                    "detail": "%d 次失败均已重试恢复 → %s（窗口 %s）"
                                              % (len(resolved), detail, span)})
                else:
                    results.append({"check": "source_failures", "status": OK,
                                    "detail": "%d 次失败均已重试恢复 → %s（今日无 pipeline 窗口）"
                                              % (len(resolved), detail)})
            else:
                start, end = source_window
                if start is not None:
                    span = start.strftime("%Y-%m-%d %H:%M:%S")
                    if end is not None:
                        span += " → " + end.strftime("%H:%M:%S")
                    detail = "最近一轮无源级失败（窗口 %s）" % span
                else:
                    # 没有可用日志 → 用的是「当天」兜底口径，如实说明
                    detail = "今日无源级失败（未能确定 pipeline 窗口）"
                results.append({"check": "source_failures", "status": OK, "detail": detail})
        else:
            results.append({"check": "source_failures", "status": OK, "detail": "无失败记录文件"})
    except Exception as exc:
        results.append({"check": "source_failures", "status": WARN, "detail": "读取失败: %s" % exc})
    # ── 4. Check disk usage ──
    try:
        du = subprocess.run(["du", "-sh", str(PROJECT_ROOT / "data")], capture_output=True, text=True, timeout=10)
        results.append({"check": "disk_usage", "status": OK, "detail": du.stdout.strip()})
    except Exception:
        pass

    # ── 5. Data freshness sentinel (v0.7 F2) — semantic checks ──
    #    Catches silent degradation (e.g. 8/8 ISO-8601 time-format drift that
    #    went unnoticed for 5 days): stale last_crawl_time, high post-time parse
    #    failure rate, and stale-post pileup.
    db_path = PROJECT_ROOT / "data" / "monitor.db"
    if db_path.exists():
        import sqlite3 as _sqlite3
        import time as _time
        # Lazy import: crawler pulls xueqiu-analyzer path — keep module load light.
        sys.path.insert(0, str(PROJECT_ROOT))
        try:
            from src.crawler import _parse_post_time
        except Exception:
            _parse_post_time = None
        conn = _sqlite3.connect(str(db_path))
        try:
            now = _time.time()
            # 5a. last_crawl_time freshness: any stock not crawled for 48h → FAIL
            #     Scope = monitor whitelist union (what the pipeline is supposed
            #     to crawl), NOT the full meta table — rows for decommissioned
            #     stocks are leftover bookkeeping, not health signals. Falls
            #     back to the full table only when no config is readable.
            rows = conn.execute(
                "SELECT stock_code, last_crawl_time FROM xueqiu_monitor_meta"
            ).fetchall()
            whitelist = _load_monitor_whitelist()
            if whitelist is not None:
                rows = [r for r in rows if r[0] in whitelist]
            if rows:
                now_48h = now - 48 * 3600
                stale = [
                    (sc, lct) for sc, lct in rows
                    if lct and (now - lct) > 48 * 3600
                ]
                fresh = [lct for sc, lct in rows if lct and lct >= now_48h]
                # FAIL only when the pipeline is broadly stalled: either NO stock
                # crawled in 48h (the 8/8 accident signature) or a majority stale
                # (whitelist rotated out en masse). A few stale in-whitelist
                # stocks (e.g. one group's cron failing while others run)
                # → WARN, so normal operation never false-alarms.
                if not fresh or len(stale) > len(rows) / 2:
                    results.append({
                        "check": "crawl_freshness", "status": FAIL,
                        "detail": f"{len(stale)}/{len(rows)} stocks not crawled in 48h (freshest: {_time.strftime('%m-%d %H:%M', _time.localtime(max(fresh or [0])))})",
                    })
                    errors.append("stale_last_crawl_time")
                elif stale:
                    worst = max(stale, key=lambda x: x[1])
                    results.append({
                        "check": "crawl_freshness", "status": WARN,
                        "detail": f"{len(stale)}/{len(rows)} whitelist stocks not crawled in 48h (stalest: {worst[0]}, last={_time.strftime('%m-%d %H:%M', _time.localtime(worst[1]))})",
                    })
                else:
                    results.append({
                        "check": "crawl_freshness", "status": OK,
                        "detail": f"all {len(rows)} whitelisted stocks crawled within 48h",
                    })
            else:
                results.append({
                    "check": "crawl_freshness", "status": WARN,
                    "detail": "xueqiu_monitor_meta empty — no crawl bookkeeping",
                })

            # 5b/5c. Post-time health on the latest snapshot per stock:
            #       parse-failure rate > 30% → FAIL (the 8/8 accident signature);
            #       > 50% discussion posts older than 7 days → WARN (stale pileup).
            #       旧帖占比只统计讨论: news/article 是时间线回放, cli 每轮把整页
            #       资讯/文章原样并入快照 (增量水位只管讨论), 安静股票的快照天然
            #       被旧内容填满 —— 2026-10-26 实测 53% 旧帖里 news 占 87%, 剔除后
            #       讨论类只有 42%。「没带来新讨论」才是这个检查要抓的退化信号。
            if _parse_post_time is not None:
                snap_rows = conn.execute(
                    """SELECT cs.stock_code, cs.posts_data, cs.crawl_time
                       FROM crawl_snapshots cs
                       WHERE cs.id IN (
                           SELECT MAX(id) FROM crawl_snapshots
                           WHERE status='success' AND posts_data IS NOT NULL
                           GROUP BY stock_code
                       )"""
                ).fetchall()
                total = fail_parse = old_posts = old_denom = 0
                old_cutoff = now - 7 * 86400
                for _sc, posts_data, _ct in snap_rows:
                    try:
                        posts = json.loads(posts_data)
                    except Exception:
                        continue
                    for p in posts:
                        total += 1
                        ts = _parse_post_time(p.get("time", "") or "", now)
                        if ts <= 0:
                            fail_parse += 1
                            continue
                        if (p.get("type") or "discussion") in ("news", "article"):
                            continue
                        old_denom += 1
                        if ts < old_cutoff:
                            old_posts += 1
                if total:
                    fail_rate = fail_parse / total
                    if fail_rate > 0.30:
                        results.append({
                            "check": "time_parse_rate", "status": FAIL,
                            "detail": f"{fail_parse}/{total} posts time-parse failed ({fail_rate:.0%})",
                        })
                        errors.append("high_time_parse_failure")
                    else:
                        results.append({
                            "check": "time_parse_rate", "status": OK,
                            "detail": f"{fail_parse}/{total} posts time-parse failed ({fail_rate:.0%})",
                        })
                    if not old_denom:
                        results.append({
                            "check": "post_freshness", "status": OK,
                            "detail": "no discussion posts to evaluate (replay-only snapshots)",
                        })
                    else:
                        old_ratio = old_posts / old_denom
                        if old_ratio > 0.50:
                            results.append({
                                "check": "post_freshness", "status": WARN,
                                "detail": f"{old_posts}/{old_denom} discussion posts older than 7d ({old_ratio:.0%})",
                            })
                        else:
                            results.append({
                                "check": "post_freshness", "status": OK,
                                "detail": f"{old_posts}/{old_denom} discussion posts older than 7d ({old_ratio:.0%})",
                            })
                else:
                    results.append({
                        "check": "time_parse_rate", "status": WARN,
                        "detail": "no posts_data to evaluate",
                    })
        finally:
            conn.close()

    # ── Output ──
    status = FAIL if any(e in ["db_missing", "watchlist_missing", "daily_report_missing",
                               "daily_report_invalid", "daily_report_placeholder",
                               "stale_last_crawl_time",
                               "high_time_parse_failure"] for e in errors) else \
             WARN if errors else OK

    report = {
        "status": status,
        "errors": errors,
        "checks": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if status != FAIL else 1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="雪球 monitor 日更健康检查")
    parser.add_argument("--require-daily-report", action="store_true",
                        help="要求当日 data/daily_reports/<date>-sentiment.md 存在且非空"
                             "（收尾巡检用；白天批间巡检不要传）")
    check(require_daily_report=parser.parse_args().require_daily_report)
