"""详情抓取层: news / 公告的页面正文补全 (v2 Phase 2, 2026-09-20).

为什么存在: 喂给日报 LLM 的 news 只有 ~110 字新浪摘要, 公告只有标题 —— 深度
认知的瓶颈在素材不在模型。通道全部真实验证, 2026-09-20 成本优化后分派
(智谱 reader 从主通道降为兜底):

  A股/港股公告 PDF → **urllib 直下 + pymupdf 本地解析** (零 API 成本;
                     实测 cninfo/stockn/stockmc 三处直链均 200 无需鉴权,
                     全文清晰; 此前"巨潮乱码"是 zhipu reader 服务端
                     解析问题, 本地方案不存在; 目录页自动跳过)
  news/雪球网页    → **opencli 本地浏览器** (零 API 成本; 走用户 Chrome
                     带 cookie/JS 渲染, selector 精准命中正文容器, 实测
                     5s/篇、正文比 reader 全页输出更干净) → reader 兜底
  美股公告 (link 为 xueqiu.com/S/ 列表页) → **SEC EDGAR 公开 API 直连**
                     (curl 子进程; title 里的 accession number → ticker→CIK
                     → submissions 定位 primaryDocument → 正文, 零成本,
                     仅 6-K/8-K/10-K/10-Q/20-F 拉全文) → 标题搜索兜底
  原文彻底拿不到时 → web-search-pro 搜标题拿新闻解读 (标注为解读而非原文)

news 三道过滤闸 (filter_news_posts) 是抓取/注入的前置 —— 2026-09-19 抽检
965 条去重样本: 当日新闻仅 2%, 68% 为 >7 天旧闻(最旧 10 个月), 且存在股票
代码误配 (CRCL=Circle 配到锂业公司新闻)。新浪流是"相关推荐"而非时间线,
不过滤直接进 LLM 会注入大量噪音。
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import subprocess
import threading
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


# ════════════════════════════════════════════════════════
# 通道 0: PDF 本地解析 (A股巨潮 + 港股雪球托管, 2026-09-20 实测零乱码)
# ════════════════════════════════════════════════════════

_PDF_URL_PAT = re.compile(r"\.pdf($|\?)", re.IGNORECASE)
# 目录页识别: 含"目錄/目录/CONTENTS"且 >=5 行是纯页码 (港股年报实测结构)
_TOC_TITLE_RE = re.compile(
    r"目\s*錄|目\s*录|TABLE\s+OF\s+CONTENTS|\bCONTENTS?\b", re.I
)
_TOC_PAGE_NUM_RE = re.compile(r"\d{1,4}")


def _is_toc_page(text: str) -> bool:
    """该页是否为目录页 (页内 >=5 行是纯页码)。"""
    if not _TOC_TITLE_RE.search(text or ""):
        return False
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    return sum(1 for ln in lines if _TOC_PAGE_NUM_RE.fullmatch(ln)) >= 5


def _pdf_content(pages: list[str]) -> str:
    """拼正文, 跳过封面之后的目录页 (含封面本身)。

    中期/年度报告 (70-200 页) 前两页是封面+目录, 直接截前 8000 字等于只喂
    一份目录 (2026-09-20 实测港股中期报告); 目录页对 LLM 零价值, 跳过。
    封面信息 (公司名/报告期) 已由公告标题给出, 一并丢弃。
    """
    toc_at = next(
        (i for i, t in enumerate(pages[:6]) if _is_toc_page(t)), None
    )
    if toc_at is None:
        return "".join(pages)
    start = toc_at + 1
    while start < len(pages) - 1 and _is_toc_page(pages[start]):
        start += 1
    return "".join(pages[start:])


def _is_pdf_url(url: str) -> bool:
    return bool(_PDF_URL_PAT.search(url or "")) or (
        "cninfo.com.cn" in (url or "")
    )


def fetch_pdf_local(url: str) -> dict:
    """公告 PDF 直下 + pymupdf 本地解析 (零 API 成本).

    2026-09-20 实测: 两地 PDF urllib 直下无需鉴权 (127KB/138KB),
    pymupdf 提取两地均清晰 (~3.7k 字) —— 此前"巨潮乱码"是 zhipu reader
    服务端 PDF 解析器的问题, 本地方案不存在。智谱通道保留为兜底。
    """
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            data = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        logger.warning(f"[detail] PDF 直下失败 {url[:60]}: {e}")
        return {"title": "", "content": "", "status": "error", "channel": "pdf_local"}
    if not data.startswith(b"%PDF"):
        return {"title": "", "content": "", "status": "error", "channel": "pdf_local"}
    try:
        import fitz  # pymupdf (项目环境已装)

        doc = fitz.open(stream=data, filetype="pdf")
        text = _pdf_content([page.get_text() for page in doc])
        doc.close()
    except Exception as e:
        logger.warning(f"[detail] pymupdf 解析失败 {url[:60]}: {e}")
        return {"title": "", "content": "", "status": "error", "channel": "pdf_local"}
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return {"title": "", "content": "", "status": "empty", "channel": "pdf_local"}
    # 本地抽取不会产生编码乱码, 只有替换符算坏 —— 英文公告 CJK 占比天然低,
    # 用 _looks_like_garbled 会把港股英文版公告误杀 (2026-09-20 复盘)
    if text.count("\ufffd") > len(text) * 0.02:
        return {"title": "", "content": "", "status": "garbled", "channel": "pdf_local"}
    return {"title": "", "content": text[:DETAIL_MAX_CHARS], "status": "ok",
            "channel": "pdf_local"}


# ════════════════════════════════════════════════════════
# 通道 1': opencli 本地浏览器 (news/雪球页面, 零 API 成本)
# ════════════════════════════════════════════════════════

# opencli 的会话名对应 Chrome 里的一个 tab, 多线程共用同一会话会互相顶掉
# 当前页 —— 因此按线程分配独立会话 (实测 5 线程抓 5 篇 9.7s, 串行同量 25s;
# 111 条 news 串行要 9 分钟, 并行 ~2 分钟)。PDF/EDGAR 通道不受此影响。
_OPENCLI_TLS = threading.local()
_OPENCLI_SEQ = itertools.count()
_OPENCLI_SEQ_LOCK = threading.Lock()


def _opencli_session() -> str:
    """当前线程独占的 opencli 会话名 (首次调用时分配)。"""
    name = getattr(_OPENCLI_TLS, "session", None)
    if name is None:
        with _OPENCLI_SEQ_LOCK:
            name = f"detailfetch{next(_OPENCLI_SEQ)}"
        _OPENCLI_TLS.session = name
    return name

# 正文容器候选 (按"精确度"排序, 先命中先用): 新浪/东财/雪球通用。
# 实测 2026-09-20: 新浪 div.article 1148 字 = 正文, .article-content 18794 字
# = 整站菜单堆; 东财只有 #ContentBody 命中 (2901 字, .content/body 掺推荐流)
_ARTICLE_SELECTORS = [
    "div.article", "#artibody", ".article-content", "article",
    ".article-content-detail", "#ContentBody", ".content", "body",
]
_MIN_ARTICLE_CHARS = 300     # 短于此视为"容器没命中", 继续往下试
_MAX_BODY_LINKS = 80         # body 兜底时链接过多 = 首页/频道页, 不是文章


def _opencli(*args: str, timeout: int = 40) -> Optional[dict]:
    """跑一条 opencli browser 命令, 返回解析后的 JSON (失败 None)."""
    cmd = ["opencli", "browser", _opencli_session(), *args]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning(f"[detail] opencli 超时/失败: {' '.join(args[:2])} {e}")
        return None
    out = "\n".join(
        line for line in r.stdout.splitlines()
        if "Update available" not in line and not line.strip().startswith("Run:")
    )
    try:
        return json.loads(out)
    except (ValueError, TypeError):
        return None


_JUNK_PAGE_RE = re.compile(
    r"页面没有找到|页面不存在|网页不存在|\b404\b|Not\s+Found|即将.*跳转|"
    r"无法访问此网站|This\s+site\s+can.t\s+be\s+reached|ERR_[A-Z_]{4,}|"
    r"请检查您的互联网连接",
    re.I,
)


def _looks_like_junk_page(
    content: str, title: str = "", selector: str = ""
) -> bool:
    """占位页 / 首页菜单堆 / 浏览器错误页识别 (不可当作正文注入).

    三类实测废页 (2026-09-20):
      ① 文章 404 —— 新浪渲染 "页面没有找到" 且 5 秒后跳首页, 抓晚了 body
         里就是首页菜单 (首页 body 18745 字 209 个链接; 正常文章 1000-3000
         字不足 20 个)。按链接密度判会误杀短文章 (1148 字 7 链接 = 0.61%),
         所以只在"退到 body 兜底 + 链接过百"时按密度判。
      ② Chrome 自己的错误页 (ERR_CONNECTION_CLOSED / 无法访问此网站) ——
         雪球 news 流里有 "xueqiu.com/n/<中文标题>" 这类畸形链接。
    """
    if _JUNK_PAGE_RE.search(title or "") or _JUNK_PAGE_RE.search(content[:200]):
        return True
    return selector == "body" and content.count("](http") > _MAX_BODY_LINKS


def fetch_page_opencli(url: str) -> dict:
    """opencli 本地浏览器抓页面正文 (主通道; 需 Chrome 扩展在线).

    实测 (2026-09-20): 新浪 finance 页 selector=div.article 精准提取
    843 字正文, 无菜单噪音, 优于 zhipu reader 的全页输出。
    """
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    opened = _opencli("open", url)
    if not opened or "page" not in (opened or {}):
        return {"title": "", "content": "", "status": "error", "channel": "opencli"}
    best = ""
    best_sel = ""
    title = ""
    for sel in _ARTICLE_SELECTORS:
        ext = _opencli("extract", "--selector", sel, timeout=30)
        if not ext:
            continue
        content = (ext.get("content") or "").strip()
        title = title or (ext.get("title") or "").strip()
        if len(content) > len(best):
            best, best_sel = content, sel
        if len(best) >= _MIN_ARTICLE_CHARS:
            break  # 先命中先赢: 精确容器优先, 不再试后面更宽的选择器
    _opencli("close", timeout=15)
    best = _strip_menu_junk(best)
    if not best:
        return {"title": title, "content": "", "status": "empty", "channel": "opencli"}
    if _looks_like_junk_page(best, title, best_sel):
        logger.info(f"[detail] opencli 拿到占位/首页内容, 弃用 {url[:60]}")
        return {"title": title, "content": "", "status": "junk", "channel": "opencli"}
    return {
        "title": title, "content": best[:DETAIL_MAX_CHARS], "status": "ok",
        "channel": "opencli",
    }


def fetch_url_detail(url: str) -> dict:
    """抓单个 URL 的正文, 返回 {title, content, status}。

    通道分派 (v2, 2026-09-20 成本优化):
      PDF (公告) → 本地 urllib+pymupdf (零成本, 零乱码)
      网页 (news/雪球) → opencli 本地浏览器 (零成本) → zhipu reader 兜底
    status: ok / empty / error / garbled / junk (占位页, 不再走 reader)
    """
    url = (url or "").strip()
    if not url:
        return {"title": "", "content": "", "status": "error", "channel": ""}

    if _is_pdf_url(url):
        local = fetch_pdf_local(url)
        if local["status"] == "ok":
            return local
        logger.info(f"[detail] PDF 本地失败({local['status']}), reader 兜底 {url[:60]}")

    # 网页: opencli 主通道 (Chrome 扩展在线时)
    if _opencli_available():
        result = fetch_page_opencli(url)
        if result["status"] == "ok":
            return result
        if result["status"] == "junk":
            # 页面本身就是 404/跳转占位页, reader 也救不回来 —— 省一次调用
            return result

    return _fetch_url_reader(url)


def _opencli_available() -> bool:
    """opencli Chrome 扩展是否在线 (opencli doctor 探测, 进程内缓存).

    与 xueqiu-analyzer fetcher_opencli.is_available 同款判定
    ("[OK] Extension: connected"); 一次 enrich 周期内不重复探测。
    """
    global _OPENCLI_OK
    if _OPENCLI_OK is not None:
        return _OPENCLI_OK
    import shutil
    if not shutil.which("opencli"):
        _OPENCLI_OK = False
        return _OPENCLI_OK
    try:
        r = subprocess.run(
            ["opencli", "doctor"], capture_output=True, text=True, timeout=10
        )
        _OPENCLI_OK = "[OK] Extension: connected" in (r.stdout or "")
    except (subprocess.TimeoutExpired, OSError):
        _OPENCLI_OK = False
    return _OPENCLI_OK


_OPENCLI_OK: Optional[bool] = None


def _fetch_url_reader(url: str) -> dict:
    """zhipu reader 兜底通道 (原主通道, 2026-09-20 起降为兜底)."""
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
        return {"title": "", "content": "", "status": "error", "channel": "reader"}

    result = (rsp or {}).get("reader_result") or {}
    content = _strip_menu_junk((result.get("content") or "").strip())
    title = (result.get("title") or "").strip()
    if not content:
        return {"title": title, "content": "", "status": "empty", "channel": "reader"}
    # 乱码判据只对 PDF 成立 (reader 解析巨潮 PDF 会输出坏编码); 英文网页
    # 的中文占比天然为 0, 不能拿它判废
    if _is_pdf_url(url) and _looks_like_garbled(content):
        return {"title": title, "content": "", "status": "garbled", "channel": "reader"}
    return {
        "title": title,
        "content": content[:DETAIL_MAX_CHARS],
        "status": "ok",
        "channel": "reader",
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

# 发布日期在 URL 里的三种常见形态 (2026-09-20 补: 只认新浪形态时, 东财
# 「相关推荐」的几个月前旧闻 (URL 里 /a/YYYYMMDD…html) 会绕过时效闸)
_URL_DATE_PATTERNS = (
    re.compile(r"/(\d{4})-(\d{2})-(\d{2})/"),           # 新浪 /2026-09-19/
    re.compile(r"/a/(\d{4})(\d{2})(\d{2})\d{3,}\."),    # 东财 /a/202609163875401912.html
    re.compile(r"/(\d{4})(\d{2})(\d{2})/"),             # 同花顺 stock.10jqka.com.cn/20260414/
)


def _url_date(url: str) -> Optional[str]:
    """URL 内的发布日期, 无则 None (交给噪音闸 + LLM 甄别)。"""
    for pat in _URL_DATE_PATTERNS:
        m = pat.search(url or "")
        if not m:
            continue
        y, mo, d = m.groups()[-3:]
        if not (2000 <= int(y) <= 2100 and 1 <= int(mo) <= 12 and 1 <= int(d) <= 31):
            continue
        return f"{y}-{mo}-{d}"
    return None


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

def _form_of_title(title: str) -> str:
    """取公告标题开头的表格代号 (美股 SEC form: 6-K / 20-F / 144/A …)。"""
    m = re.match(r"^([A-Z\d]+(?:[-/][A-Z\d]+)?)\s", title or "")
    return m.group(1) if m else ""


def classify_announcement(title: str) -> str:
    """公告标题分级: high (事件驱动, 抓详情) / routine (例行, 只留标题) / other。

    high 与 routine 同时命中时 high 优先 (宁可多抓)。美股公告的标题是英文
    form 名 (无中文关键词), 需按 form 单独判定 —— 否则 SEC EDGAR 通道永远
    收不到任务 (2026-09-20 复盘发现)。
    """
    t = title or ""
    if _HIGH_VALUE_ANN_PAT.search(t):
        return "high"
    if _form_of_title(t) in _US_FULLTEXT_FORMS:
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
            "channel": "cache",
        }

    detail = fetch_url_detail(url)
    logger.info(
        f"[detail] {detail.get('channel', '?')} → {detail['status']}: {url[:70]}"
    )

    dbmod.insert_detail_fetch(db_path, url, detail["status"],
                              detail["title"], detail["content"])
    return detail


# ── 美股 SEC EDGAR 直连 (不依赖 edgartools —— 其 Company API 在本机
# 版本已变更 get_all_filings → 失效; accession 在手时原文 URL 是确定性的) ──

_ACCESSION_RE = re.compile(r"Accession\s+Number:\s*(\d{10}-\d{2}-\d{6})")
# 值得拉 EDGAR 全文的美股 form (定期报告 + 重大事项; Form 4/144 等内部人
# 持股变动只有表格数字, 不拉)
_US_FULLTEXT_FORMS = frozenset({"6-K", "8-K", "10-K", "10-Q", "20-F", "40-F"})
# SEC 要求 UA 形如 "应用名/版本 邮箱" —— 缺邮箱或用 noreply 域会被 403
# (www.sec.gov 尤其严格, data.sec.gov 宽松; 2026-09-20 实测多组 UA)
_SEC_UA = "xueqiu-monitor/1.0 paradox0504@gmail.com"
_TICKER_CIK_CACHE: dict[str, str] = {}


def _sec_get(url: str, timeout: int = 30, as_json: bool = False):
    """SEC 请求 (curl 子进程)。Python urllib 对 sec.gov 的 TLS 握手在本机
    持续 SSL EOF (curl 同 URL 200, 2026-09-20 实测), 走 curl 最稳。"""
    r = subprocess.run(
        ["curl", "-s", "-m", str(timeout), "-H", f"User-Agent: {_SEC_UA}", url],
        capture_output=True, text=True, timeout=timeout + 10,
    )
    raw = r.stdout or ""
    return json.loads(raw) if as_json else raw


def _ticker_to_cik(ticker: str) -> str:
    """ticker → CIK (SEC company_tickers.json, 进程内缓存一次下载)."""
    if ticker in _TICKER_CIK_CACHE:
        return _TICKER_CIK_CACHE[ticker]
    try:
        data = _sec_get(
            "https://www.sec.gov/files/company_tickers.json", as_json=True
        )
        for item in data.values():
            if str(item.get("ticker", "")).upper() == ticker.upper():
                cik = str(item["cik_str"])
                _TICKER_CIK_CACHE[ticker] = cik
                return cik
    except Exception as e:
        logger.warning(f"[detail] company_tickers 拉取失败: {e}")
    return ""


def _html_to_text(html: str) -> str:
    """ filing 主文档 HTML → 纯文本 (stdlib, 去脚本样式表格化简).

    先剥 inline-XBRL 的 ix:hidden/ix:header —— 20-F/10-K 里那是给机器读的
    事实块 (实测 TCOM 20-F 正文首屏全是 XBRL 数字残渣), 留着会顶掉真正文。
    """
    html = re.sub(
        r"<ix:(hidden|header)[^>]*>.*?</ix:\1>", " ", html, flags=re.S | re.I
    )
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", html, flags=re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    import html as _html
    text = _html.unescape(html)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _fetch_us_filing(title: str, stock_code: str) -> str:
    """美股公告 → SEC EDGAR filing 原文 (公开 API 直连, 零成本).

    路径: title 提取 accession → ticker→CIK → submissions/CIK.json 定位
    primaryDocument → 拉主文档转文本。仅高价值 forms 拉全文。
    """
    m = _ACCESSION_RE.search(title or "")
    if not m or _form_of_title(title) not in _US_FULLTEXT_FORMS:
        return ""
    accession = m.group(1)
    ticker = stock_code.split(".")[0]
    cik = _ticker_to_cik(ticker)
    if not cik:
        return ""
    try:
        subs = _sec_get(
            f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json",
            as_json=True,
        )
        recent = subs.get("filings", {}).get("recent", {})
        accs = recent.get("accessionNumber", [])
        if accession not in accs:
            return ""
        idx = accs.index(accession)
        doc = recent.get("primaryDocument", [])[idx]
        acc_nodash = accession.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{doc}"
        html = _sec_get(url, timeout=60)
        if "<Error>" in html[:400]:  # SEC 缺件时返回 XML 错误页
            return ""
        return _html_to_text(html)[:5000]
    except Exception as e:
        logger.warning(f"[detail] EDGAR 拉取失败 {ticker}/{accession}: {e}")
        return ""


def fetch_announcement_detail(
    db_path: str, url: str, title: str, stock_code: str = ""
) -> str:
    """公告详情分派: 美股 EDGAR / PDF 本地解析 / 乱码降级搜索。返回正文。

    - 美股 (link 为 xueqiu.com/S/ 列表页, 非 PDF): SEC EDGAR 公开 API 拉
      filing 全文 (需 title 含 accession number); 拿不到时降级标题搜索
    - A股/港股 PDF: fetch_detail_cached → 本地 pymupdf (fetch_url_detail
      内分派), 失败 reader 兜底, 都拿不到时降级标题搜索
    降级产物语义是"新闻解读"而非原文, 调用方在 prompt 里应标注来源。
    """
    # 美股分支: xueqiu.com/S/CODE 形态的列表页链接
    if stock_code.endswith(".US") and "/S/" in (url or ""):
        text = _fetch_us_filing(title, stock_code)
        if text:
            logger.info(f"[detail] sec_edgar → ok: {stock_code} {title[:50]}")
            return f"[SEC EDGAR 原文]\n{text}"
        bg = search_context(f"{stock_code.split('.')[0]} {title}")
        if bg:
            from . import db as dbmod
            dbmod.insert_detail_fetch(db_path, url, "search_fallback", title, bg)
            return f"[EDGAR 原文不可得, 以下为新闻解读]\n{bg[:5000]}"
        return ""

    detail = fetch_detail_cached(db_path, url)
    if detail["status"] == "ok" and detail["content"]:
        return detail["content"][:5000]
    # 原文拿不到 (编码损坏 / 扫描件 / 下载失败) → 标题搜索给新闻解读。
    # 走到这里的一定是高权重公告 (调用方已过滤), 每日量级个位数。
    bg = search_context(f"{title} 公告 解读")
    if bg:
        from . import db as dbmod
        dbmod.insert_detail_fetch(db_path, url, "search_fallback", title, bg)
        logger.info(f"[detail] search_fallback → ok ({detail['status']}): {title[:50]}")
        return f"[原文不可得({detail['status']}), 以下为新闻解读]\n{bg[:5000]}"
    return ""
