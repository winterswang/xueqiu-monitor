from __future__ import annotations

from src.cli import _select_reply_candidates


def test_reply_candidates_deduplicate_same_url_in_feed():
    posts = [
        {
            "type": "discussion",
            "link": "https://xueqiu.com/1/100",
            "comment_count": 20,
        },
        {
            "type": "discussion",
            "link": "https://xueqiu.com/1/100",
            "comment_count": 20,
        },
        {
            "type": "discussion",
            "link": "https://xueqiu.com/1/101",
            "comment_count": 10,
        },
    ]

    selected = _select_reply_candidates(posts, set(), 5, 5)

    assert selected == [
        "https://xueqiu.com/1/100",
        "https://xueqiu.com/1/101",
    ]


def test_reply_candidates_use_global_existing_set():
    posts = [
        {
            "type": "discussion",
            "link": "https://xueqiu.com/1/100",
            "comment_count": 20,
        }
    ]

    assert _select_reply_candidates(
        posts, {"https://xueqiu.com/1/100"}, 5, 5
    ) == []
