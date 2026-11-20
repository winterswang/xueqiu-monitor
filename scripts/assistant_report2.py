#!/usr/bin/env python3
"""助理版日报 v2：逐帖提取「有没有具体信息」，按信息类型分组，输出 Markdown + HTML。

与 report_generator.py（深读版）的区别：
- 不做 1000 字分析，只做「这条帖子里有没有干货」的事实判断和一句话提炼
- 按板块 → 个股 → 信息类型 三级组织
- 无信息 / 无新帖的票一行带过
"""
from __future__ import annotations

import argparse
import html as H
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

import src.config  # noqa: E402,F401
from src.llm_config import resolve_model  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("assistant_report")

TAGS = ["一手", "硬数字", "产业细节", "可核验", "反常"]
TIER_CORE, TIER_REGULAR, TIER_PASSERBY = "核心", "常客", "路人"
NOISE_RE = re.compile(r"(免责声明|特别提示|仅供.{0,6}参考|不构成投资建议)")
ASK_RE = re.compile(r"(有哪位大神|请问一下|求大神|指导一下|谁能解释)")
MIN_LEN = 40
MAX_POSTS_PER_STOCK = 80
CHUNK = 20
EDGE_SHOW = 10


def load_config() -> dict:
    return json.loads((PROJECT_DIR / "etc" / "config.report.json").read_text(encoding="utf-8"))


def fetch_day_posts(db_path: str, date_str: str) -> dict[str, list[dict]]:
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
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT stock_code, author, COUNT(*) n,
                  ROUND(AVG(length(content))) avg_len,
                  SUM(like_count) likes, SUM(comment_count) comments,
                  MIN(date(post_ts,'unixepoch','localtime')) d0,
                  MAX(date(post_ts,'unixepoch','localtime')) d1
           FROM posts WHERE author != '' GROUP BY stock_code, author"""
    ).fetchall()
    conn.close()
    out = {}
    for r in rows:
        try:
            span = (datetime.fromisoformat(r["d1"]) - datetime.fromisoformat(r["d0"])).days
        except (TypeError, ValueError):
            span = 0
        n = r["n"]
        tier = TIER_CORE if (n >= 20 and span >= 30) else (TIER_REGULAR if n >= 4 else TIER_PASSERBY)
        out[(r["stock_code"], r["author"])] = {
            "tier": tier, "n": n, "span": span,
            "avg_len": r["avg_len"] or 0, "likes": r["likes"] or 0,
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


PROMPT = """你是投研助理，正在为一位自己会读公告和研报的投资者筛选「{name}（{code}）」在 {date} 的帖子。

对每条帖子，只回答一个问题：**这条帖子里有没有可以提取出来的具体信息？**

具体信息指下面任一项：
- 一手：实地走访、从业者自述、亲身使用，且有可复述的细节
- 硬数字：财报、公告、回购、产能、交易、价格等具体数字
- 产业细节：工艺、交期、供应链、竞争节奏
- 可核验：引用媒体报道、会议表述、公告原文
- 反常：与主流叙事相反，且给出了论据

判定：
- read：有具体信息
- maybe：有相关信息，但和前面某条 read 说的是同一件事且更简略
- skip：没有任何具体信息（纯情绪、口号、求助、提问、与公司无关）

对 read / maybe，给：
- tag：从「一手」「硬数字」「产业细节」「可核验」「反常」里选一个
- gist：80–110 字。把帖子里的关键数字、传导链条、结论讲清楚；只陈述，不评价、不建议

输出严格 JSON（不要代码块标记）：
{{"items":[{{"id":123,"verdict":"read","tag":"硬数字","gist":"..."}}]}}

帖子列表：
{posts}
"""


SUMMARY_PROMPT = """下面是「{name}（{code}）」在 {date} 被筛出的信息条目。

请把今天这只票的新增信息串成一段综述，150–300 字。

要求：
- 只陈述事实与数据，按重要性排列
- 保留具体数字
- 不评价、不给建议、不写「这意味着」「值得关注」这类话
- 纯文字段落，不要列表、不要标题

条目：
{items}
"""


def build_summary(client, model, name, code, date_str, items) -> str:
    body = "\n".join(f"- {it}" for it in items)
    try:
        rsp = client.chat.completions.create(
            model=model, max_tokens=4000, temperature=0.1,
            messages=[{"role": "user", "content": SUMMARY_PROMPT.format(
                name=name, code=code, date=date_str, items=body)}],
        )
        return (rsp.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"{code} 综述失败: {e}")
        return ""


def _judge_chunk(client, model, name, code, posts, date_str) -> dict:
    body = "\n".join(
        f"#{p['id']} | {p['author']} | 赞{p['like_count']} 评{p['comment_count']} | "
        f"{(p['content'] or '').strip()}"
        for p in posts
    )
    rsp = client.chat.completions.create(
        model=model, max_tokens=12000, temperature=0.1,
        messages=[{"role": "user", "content": PROMPT.format(
            name=name, code=code, date=date_str, posts=body)}],
    )
    text = (rsp.choices[0].message.content or "").strip()
    if not text:
        raise ValueError(f"空返回 (finish={rsp.choices[0].finish_reason})")
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    data = json.loads(text)
    return {int(it["id"]): it for it in data.get("items", []) if "id" in it}


def judge_stock(client, model, name, code, posts, date_str) -> dict:
    verdicts: dict = {}
    for i in range(0, len(posts), CHUNK):
        chunk = posts[i:i + CHUNK]
        for attempt in range(2):
            try:
                verdicts.update(_judge_chunk(client, model, name, code, chunk, date_str))
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 1:
                    logger.warning(f"{code} 第{i//CHUNK+1}块失败: {e}")
    return verdicts


def author_label(author: str, info: dict | None) -> str:
    tier = (info or {}).get("tier", TIER_PASSERBY)
    if tier == TIER_CORE:
        return f"{author}·核心({info['n']}帖)"
    return f"{author}·{tier}"


def _nums(t: str) -> list:
    """取有辨识度的数字：剔除 4 位年份与小于 10 的整数。"""
    out = []
    for x in re.findall(r"\d+(?:\.\d+)?", t):
        if "." in x:
            out.append(f"{round(float(x), 1):g}")
        else:
            v = int(x)
            if 10 <= v <= 1900 or v > 2100 or v in (2025, 2026, 2027, 2028):
                out.append(x)
    return out


def build(db_path, date_str, out_md: Path, out_html: Path | None, workers: int, stocks: str | None = None):
    cfg = load_config()
    stocks_cfg: dict = cfg.get("stocks", {})
    model = resolve_model(cfg)
    from src.report_generator import _get_llm_client

    day = fetch_day_posts(db_path, date_str)
    tiers = author_tiers(db_path)
    order = list(stocks_cfg.keys())
    if stocks:
        want = {x.strip().upper() for x in stocks.split(",") if x.strip()}
        order = [c for c in order if c.upper() in want]

    targets = [(c, day.get(c, []), prefilter(day.get(c, []))) for c in order]
    logger.info(f"判定 {len(targets)} 只票，模型 {model}")

    cache_path = PROJECT_DIR / "data" / "cache" / f"{date_str}-verdicts.json"
    cache: dict = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            logger.info(f"命中判定缓存 {cache_path}（{len(cache)} 只）")
        except ValueError:
            cache = {}

    def work(item):
        code, raw, kept = item
        if not kept:
            return code, len(raw), kept, {}
        if code in cache:
            return code, len(raw), kept, {int(k): v for k, v in cache[code].items()}
        client = _get_llm_client()
        try:
            return code, len(raw), kept, judge_stock(client, model, stocks_cfg[code]["name"], code, kept, date_str)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{code} 失败: {e}")
            return code, len(raw), kept, {}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = {r[0]: r for r in ex.map(work, targets)}

    cache_out = {c: {str(k): v for k, v in r[3].items()} for c, r in results.items() if r[3]}
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache_out, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning(f"缓存写入失败: {e}")

    sectors: dict[str, list[str]] = {}
    for c in order:
        sectors.setdefault(stocks_cfg[c].get("sector", "其他"), []).append(c)

    data = []
    totals = {"read": 0, "edge": 0, "skip": 0, "posts": 0}
    for sector, codes in sectors.items():
        sec = []
        for code in codes:
            _, n_raw, kept, v = results[code]
            totals["posts"] += n_raw
            raw_reads = [p for p in kept if v.get(p["id"], {}).get("verdict") == "read"]
            edges = [p for p in kept if v.get(p["id"], {}).get("verdict") == "maybe"]
            # 去重：数字指纹重叠 ≥70% 视为同一事实，保留信息最完整的一条
            reads, kept_nums = [], []
            for p in sorted(raw_reads, key=lambda x: -len(v[x["id"]].get("gist", ""))):
                nums = set(_nums(v[p["id"]].get("gist", "")))
                dup = False
                if len(nums) >= 2:
                    for kn in kept_nums:
                        if len(kn) >= 2:
                            inter = len(nums & kn)
                            if inter / max(1, min(len(nums), len(kn))) >= 0.5:
                                dup = True
                                break
                if dup:
                    edges.append(p)
                    continue
                reads.append(p)
                kept_nums.append(nums)
            totals["read"] += len(reads)
            totals["edge"] += len(edges)
            totals["skip"] += len(kept) - len(reads) - len(edges)
            by_tag: dict[str, list] = {}
            for p in reads:
                by_tag.setdefault(v[p["id"]].get("tag", "其他"), []).append(p)
            summary = ""
            if len(reads) >= 3:
                cl = _get_llm_client()
                summary = build_summary(
                    cl, model, stocks_cfg[code]["name"], code, date_str,
                    [v[p["id"]].get("gist", "") for p in reads],
                )
            sec.append({
                "code": code, "name": stocks_cfg[code]["name"], "raw": n_raw,
                "read": reads, "edges": edges, "by_tag": by_tag, "v": v,
                "summary": summary,
            })
        data.append((sector, sec))

    build_md(data, totals, date_str, tiers, out_md)
    if out_html:
        build_html(data, totals, date_str, tiers, out_html)
    logger.info(f"该看 {totals['read']} / 边缘 {totals['edge']} / 不该看 {totals['skip']}")


def build_md(data, totals, date_str, tiers, out: Path):
    L = [f"# 自选股舆情日报 · {date_str}\n",
         f"> 共 {totals['posts']} 帖 · 有信息 {totals['read']} 条 · 边缘 {totals['edge']} 条\n"]
    for sector, sec in data:
        L.append(f"\n## {sector}\n")
        for s in sec:
            if not s["read"] and not s["edges"]:
                L.append(f"- **{s['code']} {s['name']}**：{s['raw']} 帖，无新内容\n" if s["raw"]
                         else f"- **{s['code']} {s['name']}**：无新帖\n")
                continue
            L.append(f"\n### {s['code']} · {s['name']}\n**{s['raw']} 帖 · 有信息 {len(s['read'])} 条**\n")
            if s.get("summary"):
                L.append(f"\n{s['summary']}\n")
            for tag in TAGS:
                items = s["by_tag"].get(tag) or []
                if not items:
                    continue
                L.append(f"\n**{tag}**\n")
                for p in items:
                    it = s["v"][p["id"]]
                    lab = author_label(p["author"], tiers.get((s["code"], p["author"])))
                    L.append(f"- {it.get('gist','')} — {lab} [#{p['id']}]({p['link']})\n")
            if s["edges"]:
                L.append(f"\n**边缘 {len(s['edges'])} 条**\n")
                for p in s["edges"][:EDGE_SHOW]:
                    it = s["v"][p["id"]]
                    lab = author_label(p["author"], tiers.get((s["code"], p["author"])))
                    L.append(f"- {it.get('gist','')} — {lab} #{p['id']}\n")
                if len(s["edges"]) > EDGE_SHOW:
                    L.append(f"- …另 {len(s['edges']) - EDGE_SHOW} 条\n")
    L.append(f"\n---\n\n**合计**：有信息 {totals['read']} · 边缘 {totals['edge']} · 无信息 {totals['skip']}\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")


CSS = """
:root{--bg:#fbfbf9;--fg:#1c1c1a;--muted:#6b6b66;--line:#e3e2dc;--chip:#f0efe9;--accent:#2f6f4e}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#e8e6e0;--muted:#9a9890;--line:#2a2a28;--chip:#242421;--accent:#7fbf9a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.7 -apple-system,"PingFang SC","Helvetica Neue",sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:24px 18px 64px}
h1{font-size:22px;margin:8px 0 4px}
.meta{color:var(--muted);font-size:14px;margin-bottom:24px}
.toc{border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin-bottom:28px;font-size:14px}
.toc a{color:inherit;text-decoration:none;border-bottom:1px dotted var(--line)}
.toc div{margin:4px 0}
h2{font-size:18px;margin:36px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
details{border:1px solid var(--line);border-radius:10px;margin:10px 0;overflow:hidden}
summary{cursor:pointer;padding:12px 14px;font-weight:600;list-style:none}
summary::-webkit-details-marker{display:none}
summary .cnt{font-weight:400;color:var(--muted);font-size:13px;margin-left:8px}
.body{padding:2px 14px 14px}
.tag{display:inline-block;background:var(--chip);color:var(--muted);font-size:12px;
     padding:2px 8px;border-radius:99px;margin:14px 0 6px}
ul{margin:6px 0;padding-left:18px}
li{margin:7px 0}
.who{color:var(--muted);font-size:13px}
.core{color:var(--accent);font-weight:600}
a.id{color:var(--muted);text-decoration:none;font-size:12px}
.sum{background:var(--chip);border-radius:8px;padding:12px 14px;margin:12px 0;font-size:15px;line-height:1.85}
.edges{border-top:1px dashed var(--line);margin-top:14px;padding-top:10px;font-size:14px;color:var(--muted)}
"""


def build_html(data, totals, date_str, tiers, out: Path):
    P = []
    P.append(f"<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
             f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>自选股舆情日报 {date_str}</title><style>{CSS}</style></head><body><div class='wrap'>")
    P.append(f"<h1>自选股舆情日报 · {date_str}</h1>")
    P.append(f"<div class='meta'>共 {totals['posts']} 帖 · 有信息 {totals['read']} 条 · "
             f"边缘 {totals['edge']} 条</div>")
    P.append("<div class='toc'>")
    for sector, sec in data:
        links = " ".join(
            f"<a href='#{s['code']}'>{s['name']}</a>" for s in sec if s["read"]
        )
        P.append(f"<div><b>{H.escape(sector)}</b>　{links}</div>")
    P.append("</div>")
    for sector, sec in data:
        groups = [s for s in sec if s["read"] or s["edges"]]
        if not groups:
            continue
        P.append(f"<h2>{H.escape(sector)}</h2>")
        for s in sec:
            title = f"{H.escape(s['name'])} <span class='cnt'>{s['code']} · {s['raw']} 帖"
            if s["read"]:
                title += f" · 有信息 {len(s['read'])} 条"
            title += "</span>"
            P.append(f"<details id='{s['code']}'{' open' if s['read'] else ''}><summary>{title}</summary>")
            P.append("<div class='body'>")
            if not s["read"] and not s["edges"]:
                P.append("<p class='who'>无新内容</p>")
            if s.get("summary"):
                P.append(f"<p class='sum'>{H.escape(s['summary'])}</p>")
            for tag in TAGS:
                items = s["by_tag"].get(tag) or []
                if not items:
                    continue
                P.append(f"<div class='tag'>{tag}</div><ul>")
                for p in items:
                    it = s["v"][p["id"]]
                    info = tiers.get((s["code"], p["author"])) or {}
                    cls = "core" if info.get("tier") == TIER_CORE else "who"
                    P.append(
                        f"<li>{H.escape(it.get('gist',''))}"
                        f"<br><span class='{cls}'>{H.escape(p['author'])}·{info.get('tier', TIER_PASSERBY)}</span> "
                        f"<a class='id' href='{p['link']}' target='_blank'>#{p['id']}</a></li>"
                    )
                P.append("</ul>")
            if s["edges"]:
                P.append(f"<div class='edges'><b>边缘 {len(s['edges'])} 条</b><ul>")
                for p in s["edges"][:EDGE_SHOW]:
                    it = s["v"][p["id"]]
                    info = tiers.get((s["code"], p["author"])) or {}
                    P.append(f"<li>{H.escape(it.get('gist',''))} — "
                             f"{H.escape(p['author'])}·{info.get('tier', TIER_PASSERBY)} #{p['id']}</li>")
                if len(s["edges"]) > EDGE_SHOW:
                    P.append(f"<li>…另 {len(s['edges']) - EDGE_SHOW} 条</li>")
                P.append("</ul></div>")
            P.append("</div></details>")
    P.append("</div></body></html>")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(P), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--db", default=str(PROJECT_DIR / "data" / "monitor.db"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--html", default=None)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--stocks", default=None)
    a = ap.parse_args()
    out = Path(a.out) if a.out else PROJECT_DIR / "data" / "daily_reports" / f"{a.date}-assistant.md"
    htmlp = Path(a.html) if a.html else out.with_suffix(".html")
    build(a.db, a.date, out, htmlp, a.workers, a.stocks)


if __name__ == "__main__":
    main()
