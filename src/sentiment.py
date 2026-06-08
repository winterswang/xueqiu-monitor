"""xueqiu-monitor: LLM sentiment analysis (MiniMax M2.7, per-type batching)

Groups posts by type (discussions / articles / news), then makes
batched LLM calls. Discussions and articles use LLM;
news uses keyword matching (no API cost).

Batch strategy:
  - Discussions: sub-batch of 80 items max (avoids MiniMax timeout on
    400+ item groups). Each sub-batch = one compact LLM call.
  - Articles (up to 30 items):   single call, max_tokens=4096
  - News:                        keyword-based (0 calls)

Key design decisions:
  - MiniMax M2.7 thinking blocks expand with input complexity;
    the tight prompt ("NO analysis") minimises thinking waste.
  - When thinking overflows the output budget → retry at 2× tokens.
  - Client timeout raised to 300s to allow server-side processing of
    large batches; individual API calls still have a 300s cap.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

# ── Batch sizing ─────────────────────────────────────────────
# Discussions are split into sub-batches when the group exceeds
# this threshold. Each sub-batch gets its own LLM call.
DISCUSSION_BATCH_SIZE = 80

# ── Per-call token budgets ───────────────────────────────────
# For discussions (sub-batch ≤80 items): moderate budget
MAX_TOKENS_DISCUSSION   = 4096
MAX_TOKENS_DISCUSSION_R = 8192

# For articles (small group, detailed content): moderate budgets
MAX_TOKENS_ARTICLE      = 4096
MAX_TOKENS_ARTICLE_R    = 8192

# ── Timeout overrides ────────────────────────────────────────
# MiniMax can take 2-5 min for large batches; 300s gives enough
# headroom while catching true hangs.
LLM_CLIENT_TIMEOUT = 120.0
LLM_CALL_TIMEOUT   = 90.0
SENTIMENT_TOTAL_TIMEOUT = 120.0  # thread-level cap for entire sentiment call

_client: Any | None = None

# ── News keyword matching ────────────────────────────────────
_BULLISH_PAT = re.compile(
    r"(大涨|暴涨|涨停|新高|突破|利好|业绩超预期|营收增长|利润大增|"
    r"回购|增持|分红|派息|中标|签约|产能释放|放量|扭亏|复苏)"
)
_BEARISH_PAT = re.compile(
    r"(大跌|暴跌|跌停|新低|亏损|暴雷|退市|ST|减持|套现|"
    r"诉讼|处罚|调查|警告函|业绩暴雷|商誉减值|债务违约)"
)


def _extract_text(response: Any) -> str:
    """Extract text from response, preferring TextBlock over ThinkingBlock."""
    text = ""
    thinking_fallback = ""
    for block in response.content:
        if hasattr(block, "text") and block.text:
            text = str(block.text)
            break
        try:
            t = getattr(block, "thinking", "")
            if isinstance(t, str) and t:
                thinking_fallback += t
        except Exception:
            pass
    return text or thinking_fallback


def _load_openclaw_api_key() -> tuple[str, str] | None:
    """Try to read MiniMax API key from OpenClaw gateway config."""
    import json
    from pathlib import Path
    config_path = Path.home() / ".openclaw" / "openclaw.json"
    if not config_path.exists():
        return None
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        minimax = cfg.get("models", {}).get("providers", {}).get("minimax", {})
        api_key = minimax.get("apiKey", "")
        base_url = minimax.get("baseUrl", "")
        if api_key and base_url:
            return api_key, base_url
    except Exception as e:
        logger.debug(f"OpenClaw config read failed: {e}")
    return None


def _get_client() -> Any | None:
    global _client
    if _client is not None:
        return _client

    api_key = os.environ.get("MINIMAX_API_KEY", "")
    base_url = os.environ.get(
        "MINIMAX_BASE_URL", "https://api.minimaxi.com/anthropic"
    )

    # Fallback: read from OpenClaw gateway config (like morning-brief does)
    if not api_key:
        oc = _load_openclaw_api_key()
        if oc:
            api_key, base_url = oc
            logger.info("Sentiment LLM: loaded API key from OpenClaw config")

    if not api_key:
        logger.warning("MINIMAX_API_KEY 未设置，sentiment 返回 0.0")
        _client = None
        return None

    try:
        from anthropic import Anthropic
        _client = Anthropic(
            api_key=api_key,
            base_url=base_url,
            max_retries=1,
            timeout=LLM_CLIENT_TIMEOUT,
        )
        logger.info(f"Sentiment LLM ready: base_url={base_url}, timeout={LLM_CLIENT_TIMEOUT}s")
        return _client
    except ImportError:
        logger.warning("anthropic 未安装")
        _client = None
    except Exception as e:
        logger.error(f"LLM client init failed: {e}")
        _client = None
    return None


# ════════════════════════════════════════════════════════
# Public API
# ════════════════════════════════════════════════════════

def analyze_sentiment_batch(posts: list[dict]) -> list[float]:
    """Analyse sentiment for all posts with a hard thread-level timeout.

    Strategy:
      1. Group by type (discussion / article / news)
      2. News -> keyword matching (0 LLM calls)
      3. Each group -> one LLM call (titles only for discussions,
         titles + first 100 chars of body for articles)

    Returns sentiment scores in the same order as posts.
    Falls back to all-zeros if total call exceeds SENTIMENT_TOTAL_TIMEOUT.
    """
    import threading

    n = len(posts)
    if n == 0:
        return []

    _result: list[float] = []
    _done = threading.Event()

    def _run():
        try:
            _result.extend(_analyze_sentiment_impl(posts))
        except Exception as e:
            logger.warning(f"Sentiment total failed: {e}")
            _result.extend([0.0] * n)
        finally:
            _done.set()

    t = threading.Thread(target=_run, daemon=True)
    t0 = time.time()
    t.start()
    finished = _done.wait(timeout=SENTIMENT_TOTAL_TIMEOUT)
    elapsed = time.time() - t0

    if not finished:
        logger.warning(
            f"Sentiment total timeout ({SENTIMENT_TOTAL_TIMEOUT}s, "
            f"elapsed={elapsed:.1f}s), fallback to 0.0"
        )
        return [0.0] * n

    if len(_result) != n:
        logger.warning(f"Sentiment result mismatch ({len(_result)} vs {n})")
        return _result[:n] + [0.0] * max(0, n - len(_result)) if _result else [0.0] * n

    return _result


def _analyze_sentiment_impl(posts: list[dict]) -> list[float]:
    """Actual implementation -- called inside timeout wrapper."""
    n = len(posts)

    # Group by type
    groups: dict[str, list[int]] = {"discussion": [], "article": [], "news": []}
    for i, p in enumerate(posts):
        t = p.get("type", "discussion")
        groups.setdefault(t, []).append(i)

    # Sentiment array (init neutral)
    scores = [0.0] * n

    client = _get_client()
    if not client:
        _analyze_news(posts, groups.get("news", []), scores)
        return scores

    # News: keyword (always, regardless of client)
    news_idxs = groups.get("news", [])
    if news_idxs:
        _analyze_news(posts, news_idxs, scores)
        logger.info(f"Sentiment news (keyword): {len(news_idxs)} posts")

    # Discussions: sub-batched to avoid timeout
    disc_idxs = groups.get("discussion", [])
    if disc_idxs:
        n_disc = len(disc_idxs)
        n_batches = (n_disc + DISCUSSION_BATCH_SIZE - 1) // DISCUSSION_BATCH_SIZE
        if n_batches > 1:
            logger.info(
                f"Sentiment discussion: {n_disc} posts -> {n_batches} batches "
                f"(<={DISCUSSION_BATCH_SIZE}/batch)"
            )
        for b in range(n_batches):
            start = b * DISCUSSION_BATCH_SIZE
            end = min(start + DISCUSSION_BATCH_SIZE, n_disc)
            batch_idxs = disc_idxs[start:end]
            _analyze_group(
                posts, batch_idxs, scores, client,
                group_label="discussion" if n_batches == 1 else f"discussion-b{b+1}",
                max_tokens=MAX_TOKENS_DISCUSSION,
                max_tokens_retry=MAX_TOKENS_DISCUSSION_R,
            )

    # Articles: single call
    art_idxs = groups.get("article", [])
    if art_idxs:
        _analyze_group(
            posts, art_idxs, scores, client,
            group_label="article",
            max_tokens=MAX_TOKENS_ARTICLE,
            max_tokens_retry=MAX_TOKENS_ARTICLE_R,
        )

    return scores

def _analyze_group(
    posts: list[dict],
    idxs: list[int],
    scores: list[float],
    client: Any,
    group_label: str,
    max_tokens: int,
    max_tokens_retry: int,
) -> None:
    """One LLM call for an entire group of posts."""
    n = len(idxs)
    if n == 0:
        return

    # Build compact prompt: titles only for discussions,
    # title+body-snippet for articles (they have rich content)
    items = []
    for local_idx, global_idx in enumerate(idxs):
        p = posts[global_idx]
        title = (p.get("title") or p.get("content") or "")[:60]
        if group_label == "article":
            body = (p.get("content") or "")[:120]
            if body:
                title = f"{title} | {body}"
        items.append(f"[{local_idx}] {title}")

    batch_text = "\n".join(items)

    prompt = (
        f"Classify {n} stock-related posts by sentiment.\n"
        "Score: -1=negative, 0=neutral, 1=positive.\n"
        "Output ONLY a compact JSON array — NO explanation, NO analysis, NO markdown.\n\n"
        f"{batch_text}\n\n"
        'JSON: [{"i":0,"s":0.5},"..."]'
    )

    for attempt, mt in enumerate([max_tokens, max_tokens_retry]):
        try:
            t0 = time.time()
            response = client.messages.create(
                model="MiniMax-M2.7",
                max_tokens=mt,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                timeout=LLM_CALL_TIMEOUT,
            )
            elapsed_ms = (time.time() - t0) * 1000

            text = _extract_text(response)
            if not text and attempt == 0:
                logger.warning(
                    f"Sentiment {group_label}: no text (stop={response.stop_reason}), "
                    f"retrying max_tokens={max_tokens_retry}"
                )
                continue

            if not text:
                logger.warning(f"Sentiment {group_label}: empty response after retry")
                return

            # Parse JSON and map back to global scores
            parsed = _parse_json(text)
            mapped = 0
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                local_idx = item.get("i", item.get("id", -1))
                if not isinstance(local_idx, int):
                    local_idx = -1
                if 0 <= local_idx < len(idxs):
                    global_idx = idxs[local_idx]
                    try:
                        scores[global_idx] = float(item.get("s", item.get("sentiment", 0.0)))
                        mapped += 1
                    except (TypeError, ValueError):
                        pass
            if mapped == 0 and parsed:
                logger.debug(f"Sentiment {group_label}: parse mismatch, preview={text[:300]}")

            pos = sum(1 for idx in idxs if scores[idx] > 0.1)
            neg = sum(1 for idx in idxs if scores[idx] < -0.1)
            logger.info(
                f"Sentiment {group_label}: {n}篇 +{pos}/{n-pos-neg}/-{neg}, "
                f"mapped={mapped}, {elapsed_ms:.0f}ms, attempt={attempt}"
            )
            return

        except Exception as e:
            err_msg = str(e).lower()
            if attempt == 0 and ("max_tokens" in err_msg or "too long" in err_msg):
                logger.warning(
                    f"Sentiment {group_label}: {e}, retrying max_tokens={max_tokens_retry}"
                )
                continue
            logger.warning(f"Sentiment {group_label} failed: {e}")
            return


# ════════════════════════════════════════════════════════
# News keyword matching
# ════════════════════════════════════════════════════════

def _analyze_news(
    posts: list[dict],
    idxs: list[int],
    scores: list[float],
) -> None:
    """Keyword-based sentiment for news headlines (0 LLM cost)."""
    for global_idx in idxs:
        p = posts[global_idx]
        title = (p.get("title") or "") + " " + (p.get("content") or "")[:100]

        bull = bool(_BULLISH_PAT.search(title))
        bear = bool(_BEARISH_PAT.search(title))

        if bull and not bear:
            scores[global_idx] = 0.5
        elif bear and not bull:
            scores[global_idx] = -0.5
        elif bull and bear:
            scores[global_idx] = 0.0  # mixed signal → neutral
        else:
            scores[global_idx] = 0.0


# ════════════════════════════════════════════════════════
# JSON parsing
# ════════════════════════════════════════════════════════

def _parse_json(text: str) -> list[dict]:
    """Parse sentiment JSON array from LLM output."""
    text = text.strip()

    # Direct array
    if text.startswith("["):
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # Code block
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Bracket matching (last resort)
    start = text.find("[")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break

    logger.debug(f"Sentiment JSON parse failed, preview: {text[:200]}")
    return []
