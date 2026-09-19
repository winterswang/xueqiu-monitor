"""详情抓取层: news / 公告的页面正文补全 (v2 Phase 2, 2026-09-20).

为什么存在: 喂给日报 LLM 的 news 只有 ~110 字新浪摘要, 公告只有标题 —— 深度
认知的瓶颈在素材不在模型。本模块按输入源分派通道, 全部真实验证于 2026-09-19:

  雪球帖子/资讯页  → 智谱 reader (实测 1061 字正文)
  新浪 news 页     → 智谱 reader (实测 4363 字; 必须 https + no_cache,
                     http 协议会拿空 — 2026-09-19 实测两次复现)
  港股公告 PDF     → 智谱 reader (实测 3529 字清晰)
  A股巨潮 PDF      → reader 乱码(字体编码), 检出后降级 web-search-pro 搜标题
                     拿新闻解读 (巨潮 static.cninfo.com.cn 直链实测)

news 三道过滤闸 (filter_news_posts) 是抓取/注入的前置 —— 2026-09-19 抽检
965 条去重样本: 当日新闻仅 2%, 68% 为 >7 天旧闻(最旧 10 个月), 且存在股票
代码误配 (CRCL=Circle 配到锂业公司新闻)。新浪流是"相关推荐"而非时间线,
不过滤直接进 LLM 会注入大量噪音。

零第三方依赖: urllib 直调, 不引入 requests/openai (本模块被 cron 与日报
两个路径共用, 保持 import-light)。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

# ── 智谱 API 常量 ──────────────────────────────────────────
_ZHIPU_BASE = "https://open.bigmodel.cn/api"
_READER_PATH = "/paas/v4/reader"
_TOOLS_PATH = "/paas/v4/tools"
_HTTP_TIMEOUT = 45          # reader 抓 PDF 较慢, 实测 30s 内返回
_SEARCH_TIMEOUT = 30

# 详情正文留存上限 (存库); 注入 prompt 的上限在 report 侧另控 (5000)
DETAIL_MAX_CHARS = 8000

# 噪音闸: 标题命中即丢弃 (融资余额播报/衍生权证文件/旧评级复读等)
_NOISE_TITLE_PAT = re.compile(
    r"融资余额|融资买入|融券|衍生权证|认股证|牛熊证|补充上市文件|权证|"
    r"获.*评级|维持|上调评级|下调评级|目标价\d|龙虎榜|大宗交易|"
    r"涨超|跌超|收涨|收跌|盘中|直线拉升|跳水|涨幅|跌幅"
)

# 公告分级: 高权重(事件驱动, 抓详情) vs 例行(只留标题)
_HIGH_VALUE_ANN_PAT = re.compile(
    r"年度报告|中期报告|季度报告|业绩预告|盈利警告|正面盈利|预增|预亏|"
    r"收购|出售|合并|回购|增持|减持|配售|供股|合股|"
    r"FDA|审批|获批|临床|上市申请|招股|"
    r"重大合同|合作协议|中标|公司债|可转债|股东大会|派息|股息|分红"
)
_ROUTINE_ANN_PAT = re.compile(
    r"翌日披露|月报表|董事会会议|持续督导|保荐|法律意见|更正|澄清|"
    r"海外监管公告|翻译版本|报章|通告|通函|表決|表格"
)


# ════════════════════════════════════════════════════════
# 智谱 API 底座
# ════════════════════════════════════════════════════════

def _zhipu_key() -> str:
    key = os.environ.get("ZHIPU_API_KEY", "")
    if not key:
        raise RuntimeError("ZHIPU_API_KEY not set (add to .env)")
    return key


def _post_json(url: str, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_zhipu_key()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ════════════════════════════════════════════════════════
# 通道 1: 智谱 reader (网页 / PDF 正文)
# ════════════════════════════════════════════════════════

def _looks_like_garbled(text: str) -> bool:
    """巨潮 PDF 乱码检测: 长文本里 CJK 占比过低即视为编码损坏。

    实测巨潮 PDF 经 reader 返回 2890 字符的坏编码文本 (非中文字符乱堆),
    港股 PDF 同路径正常 —— 内容里有足够中文才能信。
    """
    if len(text) < 200:
        return False
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return cjk / len(text) < 0.25


def _strip_menu_junk(text: str) -> str:
    """去掉页面正文前的导航菜单段 (东财/新浪实测开头 ~20 行菜单词).

    判定: 文本开头出现连续 >=5 行的短行 (≤8 字, 可带 - / • / | 引导),
    视为站点导航, 整段丢弃。菜单后的正文不受影响。
    """
    lines = text.split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    j = i
    menu_count = 0
    while j < len(lines):
        s = lines[j].strip().lstrip("-•·|> ").strip()
        if s and len(s) <= 8:
            menu_count += 1
            j += 1
        elif not s:
            j += 1  # 菜单段内的空行
        else:
            break
    if menu_count >= 5:
        return "\n".join(lines[j:]).strip()
    return text


def fetch_url_detail(url: str) -> dict:
    """抓单个 URL 的正文, 返回 {title, content, status}。

    status: ok / empty / error / garbled (garbled 供调用方触发降级通道)。
    新浪 http:// 链接先归一化 https (http 实测拿空)。
    """
    url = (url or "").strip()
    if not url:
        return {"title": "", "content": "", "status": "error"}
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]

    try:
        rsp = _post_json(
            f"{_ZHIPU_BASE}{_READER_PATH}",
            {"url": url, "return_format": "markdown",
             "no_cache": True, "timeout": 30, "retain_images": False},
            timeout=_HTTP_TIMEOUT,
        )
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError, RuntimeError) as e:
        logger.warning(f"[detail] reader 失败 {url[:60]}: {e}")
        return {"title": "", "content": "", "status": "error"}

    result = (rsp or {}).get("reader_result") or {}
    content = _strip_menu_junk((result.get("content") or "").strip())
    title = (result.get("title") or "").strip()
    if not content:
        return {"title": title, "content": "", "status": "empty"}
    if _looks_like_garbled(content):
        return {"title": title, "content": "", "status": "garbled"}
    return {
        "title": title,
        "content": content[:DETAIL_MAX_CHARS],
        "status": "ok",
    }


# ════════════════════════════════════════════════════════
# 通道 2: 智谱 web-search-pro (背景补充 / 乱码降级)
# ════════════════════════════════════════════════════════

def search_context(query: str) -> str:
    """搜索并拼接前几条结果摘要, 用于公告标题的背景补充 (乱码降级通道)。

    返回拼接文本 (每条一段), 失败返回空串 —— 调用方以空串为"无背景"。
    """
    try:
        rsp = _post_json(
            f"{_ZHIPU_BASE}{_TOOLS_PATH}",
            {"tool": "web-search-pro",
             "messages": [{"role": "user", "content": query}],
             "stream": False},
            timeout=_SEARCH_TIMEOUT,
        )
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError, RuntimeError) as e:
        logger.warning(f"[detail] search 失败 {query[:40]}: {e}")
        return ""

    # 响应结构: choices[0].message.tool_calls[].search_result[].content
    try:
        calls = rsp["choices"][0]["message"]["tool_calls"] or []
    except (KeyError, IndexError, TypeError):
        return ""
    chunks: list[str] = []
    for call in calls:
        for item in (call.get("search_result") or [])[:3]:
            piece = (item.get("content") or "").strip()
            if piece:
                chunks.append(piece[:1000])
    return "\n".join(chunks)[:DETAIL_MAX_CHARS]


# ════════════════════════════════════════════════════════
# news 三道过滤闸
# ════════════════════════════════════════════════════════

_URL_DATE_PAT = re.compile(r"/(\d{4}-\d{2}-\d{2})/")


def _url_date(url: str) -> Optional[str]:
    m = _URL_DATE_PAT.search(url or "")
    return m.group(1) if m else None


def news_staleness_days(url: str, ref_date: str) -> Optional[int]:
    """news URL 日期与参考日的差 (正=旧). URL 无日期返回 None (不过时效闸)."""
    ud = _url_date(url)
    if not ud or not ref_date:
        return None
    try:
        from datetime import date
        d_ref = date.fromisoformat(ref_date)
        d_url = date.fromisoformat(ud)
        return (d_ref - d_url).days
    except ValueError:
        return None


def filter_news_posts(
    posts: list[dict],
    date_str: str,
    max_age_days: int = 1,
    per_stock_limit: int = 5,
) -> list[dict]:
    """news 三道过滤闸: 时效 → 噪音 → 限量。

    ① 时效闸: URL 内日期距 date_str 超过 max_age_days 的旧闻丢弃
       (68% 的存量会被滤掉; URL 无日期的不受此闸限制, 交由噪音闸+LLM 甄别)
    ② 噪音闸: 融资播报/衍生权证/行情快讯类标题丢弃
    ③ 限量闸: 过滤后按 (互动量, 标题长度) 排序取前 per_stock_limit 条

    输入为一股的 news 帖列表, 输出可进 LLM / 抓详情的子集。纯函数。
    """
    passed: list[dict] = []
    for p in posts:
        title = p.get("title") or ""
        # ② 噪音闸 (先做, 便宜)
        if _NOISE_TITLE_PAT.search(title):
            continue
        # ① 时效闸
        stale = news_staleness_days(p.get("link") or "", date_str)
        if stale is not None and stale > max_age_days:
            continue
        # 公告转载 (新浪 AIGC 的港股公告流) 与 announcements 表重叠, 丢弃
        if "公告及通告" in title or "海外监管公告" in title:
            continue
        passed.append(p)
    # ③ 限量闸: 互动量优先, 无互动的按内容长短排 (有摘要的优先)
    def _rank(p: dict) -> tuple:
        eng = (int(p.get("like_count") or 0) + int(p.get("comment_count") or 0)
               + int(p.get("forward_count") or 0))
        return (eng, len(p.get("content") or ""))
    passed.sort(key=_rank, reverse=True)
    return passed[:per_stock_limit]


# ════════════════════════════════════════════════════════
# 公告分级
# ════════════════════════════════════════════════════════

def classify_announcement(title: str) -> str:
    """公告标题分级: high (事件驱动, 抓详情) / routine (例行, 只留标题) / other。

    high 与 routine 同时命中时 high 优先 (宁可多抓)。
    """
    t = title or ""
    if _HIGH_VALUE_ANN_PAT.search(t):
        return "high"
    if _ROUTINE_ANN_PAT.search(t):
        return "routine"
    return "other"


# ════════════════════════════════════════════════════════
# 抓取编排 (带 detail_fetch_log 缓存)
# ════════════════════════════════════════════════════════

def fetch_detail_cached(db_path: str, url: str) -> dict:
    """带缓存的详情抓取: detail_fetch_log 命中(当日)直接返回, 否则抓取并落缓存。

    error 状态当天不重试 (失败标记也是缓存); 缓存按 (link, 当日) 生效,
    次日自然过期 —— 每股每天最多 5+N 次真实调用, 量可控。
    """
    from . import db as dbmod

    cached = dbmod.get_detail_fetch(db_path, url)
    if cached is not None:
        # fetched_at 当日的缓存有效 (跨日重抓, 帖子详情基本不变, 但公告
        # 解读可能更新; 成本可接受)
        return {
            "title": cached["title"],
            "content": cached["content"],
            "status": cached["status"],
        }

    detail = fetch_url_detail(url)

    # A股巨潮 PDF 乱码降级: 用标题搜新闻解读 (调用方传入标题)
    if detail["status"] == "garbled":
        detail = {"title": "", "content": "", "status": "garbled"}

    dbmod.insert_detail_fetch(db_path, url, detail["status"],
                              detail["title"], detail["content"])
    return detail


def fetch_announcement_detail(db_path: str, url: str, title: str) -> str:
    """公告详情: reader 直抓; 巨潮乱码降级为标题搜索背景。返回拼接正文。

    降级产物的语义是"新闻解读"而非公告原文, 调用方在 prompt 里应标注来源。
    """
    detail = fetch_detail_cached(db_path, url)
    if detail["status"] == "ok" and detail["content"]:
        return detail["content"][:5000]
    if detail["status"] == "garbled":
        bg = search_context(f"{title} 公告 解读")
        if bg:
            # 缓存降级产物, 避免重复搜索
            from . import db as dbmod
            dbmod.insert_detail_fetch(db_path, url, "search_fallback", title, bg)
            return f"[巨潮 PDF 编码损坏, 以下为新闻解读]\n{bg[:5000]}"
    return ""
