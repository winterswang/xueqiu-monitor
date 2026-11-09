"""X (Twitter) 舆情源：读取、清洗、归一。

数据由编排层（Frontier Autopilot）采集后落盘，本模块只读：

    data/x_posts/<YYYY-MM-DD>/<TICKER>.json

设计约束（2026-11-02 方案评审确立）：

1. **只读一次，绝不重试/轮询**。读不到就是今天没数据，采集层每天每标的只跑
   一轮受账号频控约束，monitor 侧重复请求只会徒增失败面。所有异常一律降级为空。
2. **不写 posts 表**。posts / crawl_snapshots / sentiment_stats 是雪球口径的
   统计链（z-score、温度计、并集去重都按它算），混入 X 会污染历史基线。
   本模块产出的是与 ``fetch_stock_posts()`` 同构的 dict，仅在 prompt 组装时
   与雪球帖**并列**注入，不参与任何统计口径。
3. **按 Asia/Shanghai 归日**，与日报日期口径一致。X 原始时间戳是 UTC，
   转换在本模块内完成；日期目录名由采集层按北京日期命名。
4. **不做跨语言语义去重**。英文推文与中文帖描述同一事件时保留两条——双源
   同时出现是「交叉印证」信号，合并掉反而丢失信息。只做源内去重。

清洗规则（评审点：同文本去重 / 诈骗特征词 / live 流低互动降权）：

- 同文本去重：归一化（小写、去 URL、压缩空白）后做**前缀包含**判定 ——
  抄袭号的典型形态是整段照搬原文再追加后缀（"+ Follow me for more!"），
  等值指纹比对会漏杀。短于 20 字的碎片不参与比较，避免 "gm" 吞掉整批。
- 诈骗特征词：WhatsApp / wa.me / Message my assistant / 「$X aiming $Y」等，
  命中即整条丢弃（不是降权）——这类帖零信息量且占 prompt 额度。
- live 流低互动降权：live 切片里互动 < 5 的帖排到同源末尾，不删除
  （live 是诈骗和噪音主要出没层，但也是时效信号层）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# 诈骗/引流特征（大小写不敏感）。命中任一即丢弃整条。
SCAM_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"whatsapp",
        r"wa\.me",
        r"message my assistant",
        r"dm (me|my assistant)",
        r"\$\s?\d+[\d,.kKmM]*\s*(→|->)\s*\$\s?\d+[\d,.kKmM]*",  # $2K→$500K
        r"\baiming for\s+\$",
        r"join my (private )?(group|channel)",
        r"telegram\.me/",
    )
)

# live 切片的互动门槛：低于此值排到末尾（不删除）
MIN_LIVE_ENGAGEMENT = 5

# 正文最小长度。与雪球侧 llm.min_post_length 对齐：挡住 "gm" / 纯表情 /
# 占位串。实测真实数据里 "يا حبي لج يا $META"(17 字) 这类无分析价值的也会被挡。
MIN_TEXT_LEN = 30

# 「当日」判定阈值（天）。热门切片 top / min_faves 抓的是历史高互动帖，
# 实测 META 12 条里有 7 条在 2–7 天前；日报是「今日舆情」，这些不能混进
# 「今日新增」。超过该阈值的不删除，单独归入「历史高互动」区并标注天数。
FRESH_DAYS = 1.5

# 归日口径：日报按北京时间切日
CST = timezone(timedelta(hours=8))


# ------------------------------------------------------------------ 载入 --

def ticker_of(stock_code: str) -> str:
    """META.US → META；BRK.B.US → BRK.B（保留 ticker 内部点号，只剥市场后缀）。"""
    code = (stock_code or "").upper()
    if code.endswith(".US"):
        return code[:-3]
    if code.endswith(".SH") or code.endswith(".SZ") or code.endswith(".HK"):
        return code[:-3]
    return code


def is_us(stock_code: str) -> bool:
    return (stock_code or "").upper().endswith(".US")


def posts_path(root: str | Path, date_str: str, stock_code: str) -> Path:
    return Path(root) / date_str / f"{ticker_of(stock_code)}.json"


def load_x_posts(
    root: Optional[str | Path],
    date_str: str,
    stock_code: str,
) -> list[dict[str, Any]]:
    """读取并清洗某只标的当日的 X 舆情。任何异常返回空列表，永不抛。

    返回的每条与 ``report_generator.fetch_stock_posts()`` 的产出同构：
    ``{title, content, author, like_count, forward_count, comment_count,
       link, time, source, slice}``

    ``source`` 恒为 ``"x"``，供提示词组装标注 [X] / [雪球]。
    """
    if not root:
        return []
    path = posts_path(root, date_str, stock_code)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # 坏 JSON 只降级，不阻塞日报
        logger.warning(f"X 源解析失败 {path}: {type(e).__name__}: {str(e)[:120]}")
        return []

    items = raw.get("posts") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        logger.warning(f"X 源结构异常（posts 非数组）: {path}")
        return []

    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        post = _normalize(item, stock_code, date_str)
        if post:
            normalized.append(post)

    cleaned = clean_posts(normalized)
    fresh = sum(1 for p in cleaned if p.get("is_fresh"))
    logger.info(
        f"  X 源 {ticker_of(stock_code)}: 读入 {len(normalized)} 条 → 清洗后 "
        f"{len(cleaned)} 条（当日 {fresh} / 历史 {len(cleaned) - fresh}）"
    )
    return cleaned


# ------------------------------------------------------------------ 归一 --

def _as_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _parse_utc(value: Any) -> Optional[datetime]:
    """解析 ISO8601（含 Z / 偏移）。失败返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _day_end_ts(date_str: str) -> int:
    """日报日 date_str 的次日 00:00（北京时间）对应的 unix 时间戳。

    用它反推「这条帖距离日报日有多旧」——热门切片抓的是历史高互动帖，
    必须能区分「今天的」和「一周前的」。
    """
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=CST)
    except (ValueError, TypeError):
        return 0
    return int((dt + timedelta(days=1)).timestamp())


def _normalize(
    item: dict[str, Any], stock_code: str, date_str: str = ""
) -> Optional[dict[str, Any]]:
    """单条 X 帖 → 内部结构。缺正文的丢弃。"""
    text = (item.get("text") or "").strip()
    if not text:
        return None

    metrics = item.get("metrics") or {}
    like = _as_int(metrics.get("like") if isinstance(metrics, dict) else item.get("like_count"))
    reply = _as_int(metrics.get("reply") if isinstance(metrics, dict) else item.get("comment_count"))
    repost = _as_int(metrics.get("repost") if isinstance(metrics, dict) else item.get("forward_count"))
    # views：采集侧 2026-11-03 起单独给出。只作参考，**不进 engagement**——
    # 浏览量与点赞/转发不是同一量纲，混算会让"高曝光低讨论"的帖挤到前面去。
    views = _as_int(metrics.get("views")) if isinstance(metrics, dict) else 0
    # metrics_partial：live 流按钮组只渲染出 2–3 个数字时置真，含义不可信。
    # 采集侧明确让工程侧直接吃这个标记，不要自己猜数字落到哪个位置。
    partial = bool(
        item.get("metrics_partial")
        or (isinstance(metrics, dict) and metrics.get("partial"))
    )

    dt = _parse_utc(item.get("created_at_utc") or item.get("created_at"))
    if dt is None:
        time_str = "时间未知"
    else:
        time_str = dt.astimezone(CST).strftime("%m-%d %H:%M")

    post_ts = int(dt.timestamp()) if dt else 0
    end_ts = _day_end_ts(date_str)
    # days_ago: 距日报日结束时刻的天数。时间未知时给 None（当作当日处理，
    # 宁可多留也不误删——时间缺失是采集侧问题，不该让整条内容消失）。
    days_ago = round((end_ts - post_ts) / 86400, 1) if (post_ts and end_ts) else None

    return {
        "tweet_id": str(item.get("tweet_id") or ""),
        "url": str(item.get("url") or ""),
        "author": str(item.get("author") or ""),
        "text": text,
        "lang": str(item.get("lang") or ""),
        "slice": str(item.get("slice") or ""),
        "like_count": like,
        "comment_count": reply,
        "forward_count": repost,
        "views": views,
        "metrics_partial": partial,
        "time": time_str,
        "days_ago": days_ago,
        "is_fresh": True if days_ago is None else days_ago <= FRESH_DAYS,
        "_ts": post_ts,
        "_engagement": like + reply + repost,
    }


# ------------------------------------------------------------------ 清洗 --

_URL_RE = re.compile(r"https?://\S+")
_WS_RE = re.compile(r"\s+")
_PUNCT_TAIL = re.compile(r"[\s\-–—|·:：]+$")

# 前缀判定门槛：短于此长度的归一化文本不参与包含比较，避免 "gm" / "lol"
# 这类碎片把整批帖子吞掉。
MIN_PREFIX_LEN = 20


def _norm_text(text: str) -> str:
    s = _URL_RE.sub(" ", text.lower())
    s = _WS_RE.sub(" ", s).strip()
    return _PUNCT_TAIL.sub("", s)


def _is_scam(text: str) -> bool:
    return any(p.search(text) for p in SCAM_PATTERNS)


def _is_copy(a: str, b: str) -> bool:
    """两条归一化正文是否构成抄袭关系。

    不用「取前 N 字符做等值指纹」：抄袭号的典型形态是**整段照搬原文再追加
    后缀**（"+ Follow me for more!"），前 N 字符与原文并不相等，等值比对
    必然漏杀。改用前缀包含，并对短文本设门槛。
    """
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= MIN_PREFIX_LEN and long.startswith(short)


def clean_posts(
    posts: Iterable[dict[str, Any]], min_len: int = MIN_TEXT_LEN
) -> list[dict[str, Any]]:
    """源内清洗：丢诈骗/过短、按正文去重（留互动最高）、live 低互动降到末尾。

    URL 先去重（同一推文可能被多个切片抓到），再做正文前缀去重。
    """
    kept: list[dict[str, Any]] = []
    seen_url: set[str] = set()
    dropped_scam = dropped_short = dropped_dup = 0

    for p in posts:
        text = p["text"]
        if _is_scam(text):
            dropped_scam += 1
            continue
        if len(text.strip()) < min_len:
            dropped_short += 1
            continue
        url = p.get("url") or ""
        if url and url in seen_url:
            dropped_dup += 1
            continue
        if url:
            seen_url.add(url)
        p["_norm"] = _norm_text(text)
        kept.append(p)

    # 正文去重：按互动降序处理，先到先留，后来的若与已留条目构成前缀包含则丢弃。
    # 这样「原文（高互动）+ 抄袭（低互动）」留下的必然是原文。
    deduped: list[dict[str, Any]] = []
    for p in sorted(kept, key=lambda x: (-x["_engagement"], -x["_ts"])):
        if any(_is_copy(p["_norm"], q["_norm"]) for q in deduped):
            dropped_dup += 1
            continue
        deduped.append(p)

    # 排序：互动不可信的降权 → 其余按互动降序、时间降序。
    # 两类降权：① 采集侧标记 metrics_partial（数字含义不可信，没法比）；
    # ② live 切片低互动（噪音层）。都不删除——内容本身可能仍有信息。
    def sort_key(p: dict[str, Any]):
        weak = bool(p.get("metrics_partial")) or (
            p.get("slice") == "live" and p["_engagement"] < MIN_LIVE_ENGAGEMENT
        )
        return (1 if weak else 0, -p["_engagement"], -p["_ts"])

    deduped.sort(key=sort_key)

    if dropped_scam or dropped_short or dropped_dup:
        logger.info(
            f"  清洗: 丢弃诈骗 {dropped_scam} 条 / 过短 {dropped_short} 条 / "
            f"重复 {dropped_dup} 条"
        )
    return deduped


# ---------------------------------------------------------------- 对外转换 --

def to_post_dict(p: dict[str, Any]) -> dict[str, Any]:
    """内部结构 → 与 fetch_stock_posts() 同构的 dict（提示词组装直接吃）。

    X 没有「标题」概念，title 留空、正文全部进 content；互动数沿用
    like/reply/repost 映射，但**不与雪球帖混排比较**——调用方会把两源
    分段注入，避免量级差异把 X 帖压到截断线之外。
    """
    return {
        "title": "",
        "content": p["text"],
        "author": p["author"],
        "like_count": p["like_count"],
        "forward_count": p["forward_count"],
        "comment_count": p["comment_count"],
        "link": p["url"],
        "time": p["time"],
        "source": "x",
        "slice": p.get("slice", ""),
        "days_ago": p.get("days_ago"),
        "is_fresh": p.get("is_fresh", True),
        "views": p.get("views", 0),
        "metrics_partial": p.get("metrics_partial", False),
    }


def as_prompt_posts(posts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """批量转换，供 report_generator 使用。"""
    return [to_post_dict(p) for p in posts]
