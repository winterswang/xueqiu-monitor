#!/usr/bin/env python3
"""助理版日报 · 生产版（含历史叙事沉淀）

设计依据（实测，见对话记录）：
- 整票一次调用，比分块快 2.7 倍且口径一致
- 关闭推理：快 5 倍、省 4 倍 token、重复一致性 0.41→0.82，质量不变
- 抽取而非判断：输出 kind/fact/nums，避免二值判断的不稳定
- 覆盖率校验：判出 < 送入 95% 即重试，两次不足降级开推理
- 每票维护叙事档案（thread / anchors / open_questions），每天更新
- 综述只写「相对上次的变化」，不复述条目里的数字
- 按票落盘缓存，中断可续跑；改排版可秒级重出
"""
from __future__ import annotations

import argparse
import html as H
import json
import logging
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import src.config  # noqa: E402,F401
from src.llm_config import resolve_model  # noqa: E402
from src import x_sources  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("assistant_daily")

KIND_ORDER = ["一手", "硬数字", "产业细节", "可核验", "反常"]
TIER_CORE, TIER_REGULAR, TIER_PASSERBY = "核心", "常客", "路人"
NOISE_RE = re.compile(r"(免责声明|特别提示|仅供.{0,6}参考|不构成投资建议)")
ASK_RE = re.compile(r"(有哪位大神|请问一下|求大神|指导一下|谁能解释)")
MIN_LEN = 40
MAX_POSTS = 80
MIN_COVERAGE = 0.95
MAX_TOKENS = 120000
X_ID_BASE = 900000

EXTRACT_PROMPT = """你是投研助理。下面是「{name}（{code}）」在 {date} 的全部帖子（已做基础去噪）。

## 任务一：逐条抽取

对**每一条**帖子，抽取其中的具体信息：

- kind：一手 / 硬数字 / 产业细节 / 可核验 / 反常 / 无
    · 一手：实地走访、从业者自述、亲身使用
    · 硬数字：财报、公告、回购、产能、交易、价格等具体数字
    · 产业细节：工艺、交期、供应链、竞争节奏
    · 可核验：引用媒体报道、会议表述、公告原文
    · 反常：与主流叙事相反且给了论据
- fact：40–70 字，陈述这条帖子说了什么，保留具体数字；不评价、不建议
- nums：帖中关键数字（最多 5 个，原样字符串）

没有任何具体信息（纯情绪、口号、提问、与公司无关）→ kind 填「无」，省略 fact 和 nums。

**特别地**：纯大盘/指数/板块行情、ETF 净值与成交、其他公司的新闻，一律 kind 填「无」——
这些不是这家公司的信息。只有当帖子明确把行情与该公司的因果关系统起来讲时才保留。

**必须对每一条帖子都输出，不能遗漏、不能合并。**

## 任务二：变化与趋势

## 历史叙事档案（截至上一期，可能为空）

{narrative}

- change：60–120 字。相对上一期的变化：新事实、疑问被推进或证伪、讨论重心转移。
  · 不要复述条目里已有的具体数字 · 若今天没有实质推进，写「今日无实质推进」
- trend：80–140 字。把近期轨迹和今天连起来看：单边推进、来回反复，还是叙事切换？
  与前期最大差异是什么？只陈述走向，不预测、不建议。历史为空时写「暂无历史轨迹」。
- thread：一句话说清当前主线（≤40 字）
- anchors：当前 3–5 条关键事实锚点
- open_questions：尚未解决的问题，2–4 条

## 输出

严格 JSON：
{{"items":[{{"id":123,"kind":"硬数字","fact":"...","nums":["31.9亿美元"]}}],
 "change":"...","trend":"...","thread":"...","anchors":["..."],"open_questions":["..."]}}

帖子列表：
{posts}
"""

CLUSTER_PROMPT = """下面是「{name}」在 {date} 从帖子中抽取出的信息条目。

请把它们**按事实合并**：同一件事（同一公告数据、同一门店现象、同一论点）合成一条。

对每条合并后的事实给出：
- kind：一手 / 硬数字 / 产业细节 / 可核验 / 反常
- fact：60–100 字，把这个事实讲清楚，保留关键数字
- nums：该事实涉及的关键数字（最多 6 个）
- sources：提及该事实的条目 id，最多 8 个
- rep：其中信息最完整的那条 id

不同城市/门店的一手观察合并成一条，fact 里列全地点。
抽取值很低、与公司无关的条目可以丢弃。

输出严格 JSON：
{{"facts":[{{"kind":"硬数字","fact":"...","nums":["31.9亿美元"],"sources":[101,102],"rep":101}}]}}

条目列表（id | kind | fact）：
{items}
"""


def load_config() -> dict:
    return json.loads((PROJECT_DIR / "etc" / "config.report.json").read_text(encoding="utf-8"))


def fetch_day_posts(db_path: str, date_str: str) -> dict[str, list[dict]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT p.id, p.stock_code, p.post_id, p.author, p.content,
                  p.like_count, p.comment_count, p.link, p.snapshot_id
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


def snapshot_visible_count(db_path: str, date_str: str, stock_code: str) -> int:
    """当日快照里「可见」的帖数（含历史帖）——只读，**仅用于渲染说明**。

    2026-11-25 (FA 反向验证)：`fetch_day_posts` 按 `snapshot_id IN (当日快照)` 取数，
    它的语义是「**今天首次被采到**的帖」= 当日新增。APP/UBER 这类低换手标的当天
    没新帖时就是 0 帖——这是**事实**，不是采集故障。

    但日报只写「0 帖，无新内容」，读者无法区分「真的没新帖」与「采集挂了」。
    本函数取当日快照 `posts_data` 的条数（含历史重复），专供渲染时补一句
    「（当日快照可见 N 条历史帖）」——**绝不参与取数与统计**。

    ⚠️ 不许用它替代 `fetch_day_posts`：那会把 4 个月前的帖当成今日新闻。
       实测 APP.US 11-24 快照的 11 条，发布时间 2026-07-10~11-22，**11-24 的 0 条**。
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """SELECT posts_data FROM crawl_snapshots
               WHERE stock_code=? AND date(crawl_time,'unixepoch','localtime')=?""",
            (stock_code, date_str),
        ).fetchall()
        seen: set[str] = set()
        for (raw,) in rows:
            if not raw:
                continue
            try:
                posts = json.loads(raw)
            except (TypeError, ValueError):
                continue
            for p in posts if isinstance(posts, list) else []:
                key = str(p.get("post_id") or p.get("link") or "").strip()
                if key:
                    seen.add(key)
        return len(seen)
    except sqlite3.Error as e:
        logger.warning(f"{stock_code} snapshot_visible_count 失败（不阻断渲染）: {e}")
        return 0
    finally:
        conn.close()


def author_tiers(db_path: str) -> dict[tuple[str, str], dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT stock_code, author, COUNT(*) n, ROUND(AVG(length(content))) avg_len,
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
    return kept[:MAX_POSTS]


def narrative_text(n: dict | None) -> str:
    if not n:
        return "（无，这是首次分析）"
    parts = [f"主线：{n.get('thread','')}"]
    hist = n.get("history") or []
    if hist:
        parts.append("近期轨迹（由远及近）：\n" + "\n".join(
            f"- {h.get('date','')}：{h.get('change','')}" for h in hist[-10:]))
    if n.get("anchors"):
        parts.append("关键事实：\n" + "\n".join(f"- {a}" for a in n["anchors"]))
    if n.get("open_questions"):
        parts.append("待解问题：\n" + "\n".join(f"- {q}" for q in n["open_questions"]))
    parts.append(f"更新于：{n.get('updated','')}")
    return "\n".join(parts)


def _parse(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    if not text:
        raise ValueError("空返回")
    return json.loads(text)


EMPTY = {"facts": [], "noise_ids": [], "change": "", "trend": "", "thread": "", "anchors": [], "open_questions": []}


def analyze_stock(client, model, name, code, posts, date_str, prior: dict | None,
                  x_posts: list[dict] | None = None) -> dict:
    """两段：①逐条抽取（快）②按事实聚类（输入小，也快）。"""
    lines = [
        f"#{p['id']} | {p['author']} | 赞{p['like_count']} 评{p['comment_count']} | "
        f"{(p['content'] or '').strip()}"
        for p in posts
    ]
    x_ids = []
    for i, xp in enumerate(x_posts or []):
        xid = X_ID_BASE + i
        x_ids.append(xid)
        lines.append(
            f"#{xid} | [X] {xp.get('author','')} | 赞{xp.get('like_count',0)} 评{xp.get('comment_count',0)} | "
            f"{(xp.get('content') or '').strip()}"
        )
    body = "\n".join(lines)
    prompt = EXTRACT_PROMPT.format(name=name, code=code, date=date_str,
                                   narrative=narrative_text(prior), posts=body)
    sent = {p["id"] for p in posts} | set(x_ids)
    fast = {"extra_body": {"thinking": {"type": "disabled"}}}

    best_items, best_hit, narr = {}, -1, {}
    for tag, kw in [("extract", fast), ("extract-retry", fast), ("extract-reasoning", {})]:
        t0 = time.time()
        try:
            rsp = client.chat.completions.create(
                model=model, max_tokens=MAX_TOKENS, temperature=0.1,
                messages=[{"role": "user", "content": prompt}], **kw)
            data = _parse(rsp.choices[0].message.content or "")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{code} [{tag}] 失败: {e}")
            continue
        items = {int(i["id"]): i for i in data.get("items", []) if "id" in i}
        hit = len(sent & set(items))
        logger.info(f"  {code} [{tag}] {time.time()-t0:.0f}s 覆盖 {hit/max(1,len(sent)):.0%} ({hit}/{len(sent)})")
        if hit > best_hit:
            best_items, best_hit = items, hit
            narr = {
                "change": (data.get("change") or "").strip(),
                "trend": (data.get("trend") or "").strip(),
                "thread": (data.get("thread") or "").strip(),
                "anchors": data.get("anchors") or [],
                "open_questions": data.get("open_questions") or [],
            }
        if hit / max(1, len(sent)) >= MIN_COVERAGE:
            break

    info = [(i, it) for i, it in best_items.items() if it.get("kind") not in (None, "", "无") and it.get("fact")]
    noise = sorted(i for i, it in best_items.items() if it.get("kind") in (None, "", "无"))
    facts: list[dict] = []
    if info:
        payload = "\n".join(f"#{i} | {it.get('kind')} | {it.get('fact','')}" for i, it in info)
        cp = CLUSTER_PROMPT.format(name=name, date=date_str, items=payload)
        for tag, kw in [("cluster", fast), ("cluster-retry", fast)]:
            t0 = time.time()
            try:
                rsp = client.chat.completions.create(
                    model=model, max_tokens=MAX_TOKENS, temperature=0.1,
                    messages=[{"role": "user", "content": cp}], **kw)
                data = _parse(rsp.choices[0].message.content or "")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{code} [{tag}] 失败: {e}")
                continue
            for f in data.get("facts", []):
                try:
                    src = [int(x) for x in (f.get("sources") or [])]
                except (TypeError, ValueError):
                    continue
                src = [x for x in src if x in best_items]
                if not src:
                    continue
                facts.append({
                    "kind": (f.get("kind") or "其他").strip(),
                    "fact": (f.get("fact") or "").strip(),
                    "nums": f.get("nums") or [],
                    "sources": src,
                    "rep": int(f.get("rep") or src[0]) if str(f.get("rep") or src[0]).isdigit() else src[0],
                })
            logger.info(f"  {code} [{tag}] {time.time()-t0:.0f}s 合并为 {len(facts)} 条事实")
            break

    if not facts:  # 聚类失败则退回逐条
        facts = [{"kind": it.get("kind", "其他"), "fact": it.get("fact", ""),
                  "nums": it.get("nums") or [], "sources": [i], "rep": i} for i, it in info]

    return {"facts": facts, "noise_ids": noise, **narr}


def sort_key(entry, tiers, code):
    p, it = entry
    info = tiers.get((code, p["author"])) or {}
    rank = {TIER_CORE: 0, TIER_REGULAR: 1, TIER_PASSERBY: 2}.get(info.get("tier"), 3)
    return (-len(it.get("nums") or []), rank, -len(it.get("fact") or ""))


def build(db_path, date_str, out_md: Path, out_html: Path, workers: int, use_cache: bool, only: str | None = None):
    cfg = load_config()
    stocks_cfg = cfg.get("stocks", {})
    model = resolve_model(cfg)
    from src.report_generator import _get_llm_client

    day = fetch_day_posts(db_path, date_str)
    tiers = author_tiers(db_path)
    order = list(stocks_cfg.keys())
    if only:
        want = {x.strip().upper() for x in only.split(",") if x.strip()}
        order = [c for c in order if c.upper() in want]
    cache_dir = PROJECT_DIR / "data" / "cache" / date_str
    cache_dir.mkdir(parents=True, exist_ok=True)
    x_cfg = cfg.get("x_source", {}) or {}
    x_root = PROJECT_DIR / (x_cfg.get("root") or "data/x_posts")
    narr_dir = PROJECT_DIR / "data" / "narrative"
    narr_dir.mkdir(parents=True, exist_ok=True)

    def prior_of(code):
        p = narr_dir / f"{code}.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                return None
        return None

    def work(code):
        raw = day.get(code, [])
        kept = prefilter(raw)
        # 2026-11-25 (X 合并失效): 先把 X 读出来再判空 —— 原来「雪球空 → 直接 return
        # empty」，而美股在雪球中文社区当天 0 新增并不罕见（低换手标的常见），
        # 于是 X 那 54~58 条英文帖一条都没进过日报。X 与雪球是并列信息源，
        # 任一有内容就该进分析。
        xp = []
        if x_cfg.get("enabled", True) and x_sources.is_us(code):
            try:
                xp = x_sources.as_prompt_posts(
                    x_sources.load_x_posts(x_root, date_str, code))
            except Exception as e:  # 兜底：X 读取失败不拖垮雪球那条路
                logger.warning(f"{code}: X 源读取异常(忽略): {type(e).__name__}: {str(e)[:100]}")
                xp = []
        if not kept and not xp:
            return code, len(raw), kept, dict(EMPTY), "empty", xp
        cp = cache_dir / f"{code}.json"
        if use_cache and cp.exists():
            try:
                cached = json.loads(cp.read_text(encoding="utf-8"))
                if cached.get("x_count", 0) != len(xp):
                    raise ValueError("x 数据变化，重算")
                cached["items"] = {int(k): v for k, v in (cached.get("items") or {}).items()}
                for k in ("change", "trend", "thread"):
                    cached.setdefault(k, "")
                cached.setdefault("anchors", [])
                cached.setdefault("open_questions", [])
                return code, len(raw), kept, cached, "cache", xp
            except (ValueError, AttributeError):
                pass
        res = analyze_stock(_get_llm_client(), model, stocks_cfg[code]["name"],
                            code, kept, date_str, prior_of(code), xp)
        res["x_count"] = len(xp)
        try:
            cp.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            logger.warning(f"{code} 缓存写入失败: {e}")
        return code, len(raw), kept, res, "llm", xp

    t_all = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        done = list(ex.map(work, order))
    logger.info(f"全部完成 {time.time()-t_all:.0f}s")

    by_code = {d[0]: d for d in done}

    # 写回叙事档案（只更新本期有内容的票）
    for code, _raw, _kept, res, _src, _xp in done:
        if res.get("thread") or res.get("change"):
            try:
                prev = prior_of(code) or {}
                hist = [h for h in (prev.get("history") or []) if h.get("date") != date_str]
                if res.get("change"):
                    hist.append({"date": date_str, "change": res.get("change")})
                (narr_dir / f"{code}.json").write_text(json.dumps({
                    "thread": res.get("thread", ""),
                    "anchors": res.get("anchors", []),
                    "open_questions": res.get("open_questions", []),
                    "history": hist[-10:],
                    "updated": date_str,
                }, ensure_ascii=False, indent=1), encoding="utf-8")
            except OSError as e:
                logger.warning(f"{code} 叙事写入失败: {e}")

    sectors: dict[str, list[str]] = {}
    for c in order:
        sectors.setdefault(stocks_cfg[c].get("sector", "其他"), []).append(c)

    data, totals = [], {"posts": 0, "info": 0, "none": 0, "stocks": 0, "changed": 0}
    for sector, codes in sectors.items():
        sec = []
        for code in codes:
            _, n_raw, kept, res, _src, xp = by_code[code]
            by_id = {pp["id"]: pp for pp in kept}
            # 2026-11-25 (X 合并失效第二层): X 帖在 analyze_stock 里用 X_ID_BASE+i
            # 作为虚拟 id 参与抽取，facts[].rep/sources 引用的就是这些 id。
            # 渲染时若不补进 by_id，`rep not in by_id → continue` 会把 X 来源的
            # 事实**全部丢掉**——只修「雪球空提前 return」是不够的。
            for _i, _x in enumerate(xp):
                by_id.setdefault(X_ID_BASE + _i, _x)
            groups: dict[str, list] = {}
            for f in (res.get("facts") or []):
                if f.get("kind") in (None, "", "无") or not f.get("fact"):
                    continue
                rep = f.get("rep")
                if rep not in by_id:
                    rep = next((x for x in f.get("sources", []) if x in by_id), None)
                if rep is None:
                    continue
                rp = by_id[rep]
                info = tiers.get((code, rp["author"])) or {}
                groups.setdefault(f["kind"], []).append({
                    **f, "rep": rep,
                    "_author": rp["author"], "_tier": info.get("tier", TIER_PASSERBY),
                    "_link": rp["link"], "_n": len([x for x in f.get("sources", []) if x in by_id]),
                    "_ids": [x for x in f.get("sources", []) if x in by_id][:5],
                })
            for k in list(groups):
                groups[k].sort(key=lambda f: (-len(f.get("nums") or []),
                                              {TIER_CORE: 0, TIER_REGULAR: 1, TIER_PASSERBY: 2}
                                              .get(f["_tier"], 3),
                                              -len(f.get("fact") or "")))
            none_n = len([x for x in (res.get("noise_ids") or []) if x in by_id])
            info_n = sum(len(v) for v in groups.values())
            totals["posts"] += n_raw
            totals["info"] += info_n
            totals["none"] += none_n
            if info_n:
                totals["stocks"] += 1
            if res.get("change") and "无实质推进" not in res.get("change", ""):
                totals["changed"] += 1
            sec.append({"code": code, "name": stocks_cfg[code]["name"], "raw": n_raw,
                        "groups": groups, "change": res.get("change", ""),
                        "trend": res.get("trend", ""),
                        "thread": res.get("thread", ""), "anchors": res.get("anchors", []),
                        "open_questions": res.get("open_questions", []),
                        "info": info_n, "none": none_n,
                        # 2026-11-25：X 条数随行走。美股常有「雪球 0 帖、X 数十帖」，
                        # 不显示出来读者无法判断这一段的信息是否含英文源。
                        "x_n": len(xp),
                        # 2026-11-25 (FA)：只在「今天真的没新帖」时才算一次，
                        # 供渲染补一句「当日快照可见 N 条历史帖」，让读者能区分
                        # 「没新帖」与「采集挂了」。有内容的票不算，省查询。
                        "visible_n": (0 if info_n else snapshot_visible_count(db_path, date_str, code))})
        data.append((sector, sec))

    render_md(data, totals, date_str, tiers, out_md)
    render_html(data, totals, date_str, tiers, out_html)
    logger.info(f"有信息 {totals['info']} / 无信息 {totals['none']} / 有关键变化 {totals['changed']} 只")


def _author(p, tiers, code):
    info = tiers.get((code, p["author"])) or {}
    return p["author"], info.get("tier", TIER_PASSERBY)


def render_md(data, totals, date_str, tiers, out: Path):
    L = [f"# 自选股舆情日报 · {date_str}\n",
         f"> {totals['posts']} 帖 · {totals['stocks']} 只有内容 · 其中 {totals['changed']} 只叙事有推进\n"]
    for sector, sec in data:
        L.append(f"\n## {sector}\n")
        for s in sec:
            xpart = f" · X {s['x_n']} 条" if s.get("x_n") else ""
            if not s["info"] and not s["change"]:
                # 2026-11-25：原来只写「0 帖，无新内容」——读者分不清是「真没新帖」
                # 还是「采集挂了」。补一句快照可见数（含历史帖），两者都有了。
                vis = s.get("visible_n") or 0
                hint = f"（当日快照可见 {vis} 条历史帖）" if vis else ""
                L.append(f"- **{s['code']} {s['name']}**：今日无新增帖{xpart}{hint}\n")
                continue
            L.append(f"\n### {s['code']} · {s['name']}\n"
                     f"**{s['raw']} 帖{xpart} · 有信息 {s['info']} 条**\n")
            nar = []
            if s["thread"]:
                nar.append(f"**主线**　{s['thread']}")
            if s["trend"]:
                nar.append(f"**近期趋势**　{s['trend']}")
            if s["change"]:
                nar.append(f"**今日变化**　{s['change']}")
            if s["open_questions"]:
                nar.append("**待解**　· " + "　·　".join(s["open_questions"]))
            if nar:
                L.append("> " + "\n>\n> ".join(nar) + "\n")
            for kind in KIND_ORDER:
                g = s["groups"].get(kind) or []
                if not g:
                    continue
                L.append(f"\n**{kind}**\n")
                for f in g:
                    n = f.get("_n") or 1
                    extra = f"　*（{n} 帖提及）*" if n >= 3 else ""
                    L.append(f"- {f.get('fact','')}{extra}\n")
            if s["none"]:
                L.append(f"\n*另有 {s['none']} 条无具体信息（情绪/提问/重复），已折叠*\n")
    L.append(f"\n---\n\n**合计**：有信息 {totals['info']} 条 · 无信息 {totals['none']} 条\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")


CSS = """
:root{--bg:#fbfbf9;--fg:#1c1c1a;--muted:#6b6b66;--line:#e3e2dc;--chip:#f2f1ec;--accent:#2f6f4e}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#e8e6e0;--muted:#9a9890;--line:#2b2b28;--chip:#232320;--accent:#7fbf9a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.75 -apple-system,"PingFang SC",sans-serif}
.wrap{max-width:800px;margin:0 auto;padding:24px 18px 72px}
h1{font-size:21px;margin:6px 0 4px}
.meta{color:var(--muted);font-size:14px;margin-bottom:22px}
.toc{border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin-bottom:26px;font-size:14px;line-height:2}
.toc a{color:inherit;text-decoration:none;border-bottom:1px dotted var(--line)}
h2{font-size:17px;margin:34px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
details{border:1px solid var(--line);border-radius:10px;margin:10px 0;overflow:hidden}
summary{cursor:pointer;padding:11px 14px;font-weight:600;list-style:none}
summary::-webkit-details-marker{display:none}
summary .cnt{font-weight:400;color:var(--muted);font-size:13px;margin-left:8px}
.body{padding:0 14px 14px}
.nar{border-left:3px solid var(--accent);padding:8px 0 8px 12px;margin:10px 0 6px;
     font-size:15px;line-height:1.85;background:var(--chip);border-radius:0 8px 8px 0}
.nar b{font-weight:600}.nar>div+div{margin-top:10px}
.nar .q{color:var(--muted);font-size:14px;margin-top:6px}
.tag{font-size:12px;color:var(--muted);letter-spacing:.04em;margin:16px 0 4px;border-left:3px solid var(--line);padding-left:8px}
li{margin:11px 0;list-style:none;padding-left:0}li{position:relative;padding-left:14px}li:before{content:'';position:absolute;left:0;top:.72em;width:5px;height:5px;border-radius:50%;background:var(--line)}
ul{padding-left:0;margin:0}
.who{color:var(--muted);font-size:13px}
.core{color:var(--accent);font-weight:600}.cnt2{color:var(--muted);font-size:12px;margin-left:8px;white-space:nowrap}
a.id{color:var(--muted);text-decoration:none;font-size:12px}
.edge{color:var(--muted);font-size:13px;margin-top:12px;border-top:1px dashed var(--line);padding-top:8px}
"""


def render_html(data, totals, date_str, tiers, out: Path):
    P = [f"<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
         f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>自选股舆情日报 {date_str}</title><style>{CSS}</style></head><body><div class='wrap'>",
         f"<h1>自选股舆情日报 · {date_str}</h1>",
         f"<div class='meta'>{totals['posts']} 帖 · {totals['stocks']} 只有内容 · "
         f"{totals['changed']} 只叙事有推进 · 有信息 {totals['info']} 条</div>", "<div class='toc'>"]
    for sector, sec in data:
        links = " ".join(f"<a href='#{s['code']}'>{H.escape(s['name'])}</a>"
                         for s in sec if s["info"] or s["change"])
        if links:
            P.append(f"<div><b>{H.escape(sector)}</b>　{links}</div>")
    P.append("</div>")
    for sector, sec in data:
        if not any(s["info"] or s["change"] for s in sec):
            continue
        P.append(f"<h2>{H.escape(sector)}</h2>")
        for s in sec:
            head = (f"{H.escape(s['name'])} <span class='cnt'>{s['code']} · {s['raw']} 帖"
                    + (f" · X {s['x_n']} 条" if s.get("x_n") else "")
                    + (f" · 有信息 {s['info']} 条" if s["info"] else "") + "</span>")
            P.append(f"<details id='{s['code']}'{' open' if s['change'] else ''}><summary>{head}</summary>")
            P.append("<div class='body'>")
            if not s["info"] and not s["change"]:
                P.append("<p class='who'>无新内容</p></div></details>")
                continue
            nar = []
            if s["thread"]:
                nar.append(f"<div><b>主线</b>　{H.escape(s['thread'])}</div>")
            if s["trend"]:
                nar.append(f"<div style='margin-top:6px'><b>近期趋势</b>　{H.escape(s['trend'])}</div>")
            if s["change"]:
                nar.append(f"<div style='margin-top:6px'><b>今日变化</b>　{H.escape(s['change'])}</div>")
            if s["open_questions"]:
                nar.append("<div class='q'><b>待解</b>　" +
                           " · ".join(H.escape(q) for q in s["open_questions"]) + "</div>")
            if nar:
                P.append("<div class='nar'>" + "".join(nar) + "</div>")
            for kind in KIND_ORDER:
                g = s["groups"].get(kind) or []
                if not g:
                    continue
                P.append(f"<div class='tag'>{kind}</div><ul>")
                for f in g:
                    n = f.get("_n") or 1
                    extra = f"<span class='cnt2'>{n} 帖提及</span>" if n >= 3 else ""
                    P.append(f"<li>{H.escape(f.get('fact',''))}{extra}</li>")
                P.append("</ul>")
            if s["none"]:
                P.append(f"<div class='edge'>另有 {s['none']} 条无具体信息（情绪/提问/重复），已折叠</div>")
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
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--stocks", default=None)
    ap.add_argument("--no-cache", action="store_true")
    a = ap.parse_args()
    out = Path(a.out) if a.out else PROJECT_DIR / "data" / "daily_reports" / f"{a.date}-assistant.md"
    htmlp = Path(a.html) if a.html else out.with_suffix(".html")
    build(a.db, a.date, out, htmlp, a.workers, not a.no_cache, a.stocks)


if __name__ == "__main__":
    main()
