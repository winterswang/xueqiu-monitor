#!/usr/bin/env python3
"""助理版日报：逐帖筛选 + 作者档位 + 板块固定序。

与 report_generator.py（深读版）的区别：
- 不写 1000 字的分析，只做「该看 / 边缘 / 不该看」的判定和一句话提炼
- 不输出情感分、温度计、多空交锋
- 按板块分组，组内固定顺序（用 config.report.json 的声明顺序）
- 无帖子 / 无新事实的票，一行带过

用法：
    python3 scripts/assistant_report.py --date 2026-11-15
    python3 scripts/assistant_report.py --date 2026-11-15 --limit-stocks 3
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import src.config  # noqa: E402,F401  —— 触发 .env 加载
from src.llm_config import resolve_model  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("assistant_report")

NOISE_RE = re.compile(r"(免责声明|特别提示|仅供.{0,6}参考|不构成投资建议)")
ASK_RE = re.compile(r"(有哪位大神|请问一下|求大神|指导一下|谁能解释)")
MIN_LEN = 40
MAX_POSTS_PER_STOCK = 80
EDGE_SHOW = 10

TIER_CORE = "核心"
TIER_REGULAR = "常客"
TIER_PASSERBY = "路人"


def load_config() -> dict:
    return json.loads((PROJECT_DIR / "etc" / "config.report.json").read_text(encoding="utf-8"))


def fetch_day_posts(db_path: str, date_str: str) -> dict[str, list[dict]]:
    """当日帖子，按 (stock_code, post_id) 去重，保留最新快照副本。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT p.id, p.stock_code, p.post_id, p.author, p.content, p.like_count,
                  p.comment_count, p.link, p.snapshot_id
           FROM posts p
           WHERE p.snapshot_id IN (
               SELECT id FROM crawl_snapshots
               WHERE date(crawl_time,'unixepoch','localtime')=?)
           ORDER BY p.snapshot_id DESC""",
        (date_str,),
    ).fetchall()
    conn.close()

    out: dict[str, list[dict]] = {}
    seen: set[tuple[str, str]] = set()
    for r in rows:
        key = (r["stock_code"], r["post_id"] or f"row{r['snapshot_id']}-{len(seen)}")
        if key in seen:
            continue
        seen.add(key)
        out.setdefault(r["stock_code"], []).append(dict(r))
    return out


def author_tiers(db_path: str) -> dict[tuple[str, str], dict]:
    """(stock_code, author) → {n, span_days, avg_len, likes, comments}"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT stock_code, author, COUNT(*) n,
                  ROUND(AVG(length(content))) avg_len,
                  SUM(like_count) likes, SUM(comment_count) comments,
                  MIN(date(post_ts,'unixepoch','localtime')) d0,
                  MAX(date(post_ts,'unixepoch','localtime')) d1
           FROM posts WHERE author != ''
           GROUP BY stock_code, author"""
    ).fetchall()
    conn.close()
    out = {}
    for r in rows:
        try:
            span = (datetime.fromisoformat(r["d1"]) - datetime.fromisoformat(r["d0"])).days
        except (TypeError, ValueError):
            span = 0
        n = r["n"]
        if n >= 20 and span >= 30:
            tier = TIER_CORE
        elif n >= 4:
            tier = TIER_REGULAR
        else:
            tier = TIER_PASSERBY
        out[(r["stock_code"], r["author"])] = {
            "tier": tier, "n": n, "span": span,
            "avg_len": r["avg_len"] or 0, "likes": r["likes"] or 0,
            "comments": r["comments"] or 0,
        }
    return out


def prefilter(posts: list[dict]) -> list[dict]:
    kept, seen = [], set()
    for p in posts:
        t = (p["content"] or "").strip()
        if NOISE_RE.search(t) or ASK_RE.search(t) or len(t) < MIN_LEN:
            continue
        k = t[:30]
        if k in seen:
            continue
        seen.add(k)
        kept.append(p)
    return kept[:MAX_POSTS_PER_STOCK]


PROMPT = """你是投研助理。下面是「{name}（{code}）」在 {date} 的全部分析帖。

**核心原则：宁可多给，不可漏掉。** 被 skip 的帖子读者永远看不到，所以 skip 必须保守。

**skip 只给这四种**：
1. 纯情绪宣泄（"腰斩"、"亏麻了"、"何惧之有"），没有任何事实或观察
2. 求助与提问（问走势、问怎么操作、求指导）
3. 与该公司完全无关（其他公司的活动、体育赛事、平台广告、个人琐事）
4. 券商免责声明、平台广告、标题党

**其余一律进 read 或 maybe。拿不准 read 还是 maybe 时，判 read。**

- read（该看）：含任何具体信息——一手观察、具体数字、产业细节、可核验引用、
  有论据的反向推理。不要因为篇幅短、互动低、公开可查而降级。
- maybe（边缘）：与公司相关但价值较低，或与已判 read 的内容重复

对 read 和 maybe，给：
- tag：从「一手」「硬数字」「产业细节」「可核验」「反常」里选一个
- gist：一句话说清这条讲了什么，保留具体数字，不超过 60 字，不要评价

不要输出任何帖子之外的信息。不要给投资建议。

输出严格 JSON（不要代码块标记）：
{{"items":[{{"id":123,"verdict":"read","tag":"硬数字","gist":"..."}}]}}

帖子列表：
{posts}
"""


CHUNK = 20


def _judge_chunk(client, model: str, code: str, name: str, posts: list[dict], date_str: str) -> dict:
    body = "\n".join(
        f"#{p['id']} | {p['author']} | 赞{p['like_count']} 评{p['comment_count']} | "
        f"{(p['content'] or '').strip()}"
        for p in posts
    )
    prompt = PROMPT.format(name=name, code=code, date=date_str, posts=body)
    rsp = client.chat.completions.create(
        model=model, max_tokens=12000, temperature=0.1,
        messages=[{"role": "user", "content": prompt}],
    )
    text = (rsp.choices[0].message.content or "").strip()
    if not text:
        raise ValueError(f"空返回 (finish={rsp.choices[0].finish_reason})")
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    data = json.loads(text)
    return {int(it["id"]): it for it in data.get("items", []) if "id" in it}


def judge_stock(client, model: str, code: str, name: str, posts: list[dict], date_str: str) -> dict:
    """分块判定：推理型模型单次输出过长会空返回，故每 CHUNK 帖一次调用。"""
    verdicts: dict = {}
    for i in range(0, len(posts), CHUNK):
        chunk = posts[i:i + CHUNK]
        for attempt in range(2):
            try:
                verdicts.update(_judge_chunk(client, model, code, name, chunk, date_str))
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 1:
                    logger.warning(f"{code} 第{i//CHUNK+1}块判定失败: {e}")
    return verdicts


def fmt_author(author: str, info: dict | None) -> str:
    if not info:
        return f"{author} · 路人"
    tier = info["tier"]
    if tier == TIER_CORE:
        return (f"**{author} · 核心（{info['n']} 帖 / 均 {info['avg_len']} 字 / "
                f"{info['span']} 天 / 赞{info['likes']}）**")
    if tier == TIER_REGULAR:
        return f"{author} · 常客（{info['n']} 帖）"
    return f"{author} · 路人（{info['n']} 帖）"


def build(db_path: str, date_str: str, out_path: Path, limit_stocks: int | None, workers: int, stocks: str | None = None) -> None:
    cfg = load_config()
    stocks_cfg: dict = cfg.get("stocks", {})
    model = resolve_model(cfg)

    from src.report_generator import _get_llm_client

    day_posts = fetch_day_posts(db_path, date_str)
    tiers = author_tiers(db_path)

    order = list(stocks_cfg.keys())
    if stocks:
        want = [s.strip().upper() for s in stocks.split(",") if s.strip()]
        order = [c for c in order if c.upper() in want]
    elif limit_stocks:
        order = order[:limit_stocks]

    targets = []
    for code in order:
        raw = day_posts.get(code, [])
        kept = prefilter(raw)
        targets.append((code, len(raw), kept))

    if not targets:
        logger.error("没有可处理的股票")
        return

    logger.info(f"开始判定 {len(targets)} 只票，模型 {model}，并发 {workers}")

    def work(item):
        code, n_raw, kept = item
        name = stocks_cfg[code]["name"]
        if not kept:
            return code, n_raw, kept, {}
        client = _get_llm_client()
        try:
            v = judge_stock(client, model, code, name, kept, date_str)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{code} 判定失败: {e}")
            v = {}
        return code, n_raw, kept, v

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(work, targets))

    by_code = {r[0]: r for r in results}

    # 板块分组，组内保持 config 声明顺序
    sectors: dict[str, list[str]] = {}
    for code in order:
        sector = stocks_cfg[code].get("sector", "其他")
        sectors.setdefault(sector, []).append(code)

    lines: list[str] = [
        f"# 自选股舆情日报 · {date_str}（助理版）\n",
        f"> 逐帖筛选。`{date_str}` 共 {sum(r[1] for r in results)} 帖。",
        "> 判定：该看 / 边缘 / 不该看。不给投资建议。\n",
    ]

    total_read = total_maybe = total_skip = 0

    for sector, codes in sectors.items():
        sec_lines = [f"\n## {sector}\n"]
        for code in codes:
            _, n_raw, kept, v = by_code[code]
            name = stocks_cfg[code]["name"]
            reads = [p for p in kept if v.get(p["id"], {}).get("verdict") == "read"]
            maybes = [p for p in kept if v.get(p["id"], {}).get("verdict") == "maybe"]
            skips = len(kept) - len(reads) - len(maybes)
            total_read += len(reads); total_maybe += len(maybes); total_skip += skips

            if not kept:
                sec_lines.append(f"- **{code} {name}**：无新帖\n")
                continue
            if not reads and not maybes:
                sec_lines.append(f"- **{code} {name}**：{n_raw} 帖，无值得看的内容\n")
                continue

            sec_lines.append(
                f"\n### {code} · {name}\n**今日 {n_raw} 帖 · 该看 {len(reads)} · 边缘 {len(maybes)}**\n"
            )
            for i, p in enumerate(reads, 1):
                it = v[p["id"]]
                info = tiers.get((code, p["author"]))
                sec_lines.append(
                    f"{i}. `{it.get('tag','')}` **{it.get('gist','')}**\n"
                    f"   — {fmt_author(p['author'], info)} · ❤{p['like_count']} 💬{p['comment_count']} · "
                    f"[#{p['id']}]({p['link']})\n"
                )
            if maybes:
                rank = {TIER_CORE: 0, TIER_REGULAR: 1, TIER_PASSERBY: 2}
                ordered = sorted(
                    maybes,
                    key=lambda p: rank.get(
                        (tiers.get((code, p["author"])) or {}).get("tier"), 3
                    ),
                )
                sec_lines.append(f"\n**边缘 {len(maybes)} 条**\n")
                for p in ordered[:EDGE_SHOW]:
                    it = v[p["id"]]
                    info = tiers.get((code, p["author"])) or {}
                    tier_s = info.get("tier", TIER_PASSERBY)
                    sec_lines.append(
                        f"- {it.get('gist','')} — {p['author']}·{tier_s} #{p['id']}"
                    )
                if len(maybes) > EDGE_SHOW:
                    sec_lines.append(f"- …另 {len(maybes) - EDGE_SHOW} 条\n")
            if skips:
                sec_lines.append(f"**不该看 {skips} 条**：情绪、口号、提问、重复转述\n")
        if len(sec_lines) > 1:
            lines.extend(sec_lines)

    lines.append(
        f"\n---\n\n**合计**：该看 {total_read} 条 · 边缘 {total_maybe} 条 · 不该看 {total_skip} 条\n"
    )
    lines.append("\n**口径**：判定基于抓取到的卡片摘要（长文可能被截断）；"
                 "作者档位按该股历史发帖量与跨度推断，非平台认证。\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"已写入 {out_path}（{out_path.stat().st_size} 字节）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--db", default=str(PROJECT_DIR / "data" / "monitor.db"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--limit-stocks", type=int, default=None)
    ap.add_argument("--stocks", default=None, help="逗号分隔，只跑指定标的")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    out = Path(args.out) if args.out else (
        PROJECT_DIR / "data" / "daily_reports" / f"{args.date}-assistant.md"
    )
    build(args.db, args.date, out, args.limit_stocks, args.workers, args.stocks)


if __name__ == "__main__":
    main()
