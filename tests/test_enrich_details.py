"""enrich_details 集成测试 (v2 Phase 2, 2026-09-20).

只验证"编排层"的接线: 任务筛选 (news 三道闸 / 公告分级) → 通道分派 → 落库,
渠道本身全部 mock (真实通道由日报 cron 端到端覆盖, 见 detail_fetcher 注释)。

2026-09-20 复盘修掉的两个 bug 在此回归:
  ① 公告任务 SQL 没选 stock_code, 但代码读 r["stock_code"] → 直接 IndexError;
  ② 单条抓取异常会从 pool.map 抛出, 拖垮整轮详情补全。
"""

import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from src import db as dbmod
from src import detail_fetcher as df
from src import report_generator as rg
from src.models import Announcement, CrawlSnapshot


@pytest.fixture
def tmp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        dbmod.init_db(str(path))
        yield type("Tmp", (), {
            "path": path, "conn": conn,
            "date_str": time.strftime("%Y-%m-%d"),
        })()
        conn.close()


def _seed(tmp_db, stock_code: str, posts: list[dict], anns: list[dict]) -> None:
    """写入一个快照 (含 posts_data) 及该快照的公告。"""
    snap = CrawlSnapshot(
        stock_code=stock_code,
        crawl_time=int(time.time()),
        posts_count=len(posts),
        posts_data=posts,
        status="success",
    )
    snap_id = dbmod.insert_snapshot(str(tmp_db.path), snap)
    if anns:
        dbmod.insert_announcements(str(tmp_db.path), [
            Announcement(snapshot_id=snap_id, stock_code=stock_code,
                         ann_date=int(time.time()), **a)
            for a in anns
        ])


def _news(link: str, title: str = "某公司签下大单") -> dict:
    return {"type": "news", "title": title, "content": "摘要", "link": link,
            "time": "1小时前"}


def test_news_and_announcement_wiring(tmp_db, monkeypatch):
    date = tmp_db.date_str
    news_link = f"https://finance.sina.com.cn/jjxw/{date}/doc-a.shtml"
    pdf_link = "https://stockn.xueqiu.com/02513/20260918293671.pdf"
    us_link = "https://xueqiu.com/S/TSM"
    _seed(tmp_db, "2513.HK", [_news(news_link)], [
        {"ann_title": "智谱 公告及通告 - [配售] 完成配售新H股", "ann_link": pdf_link},
        {"ann_title": "翌日披露报表", "ann_link": "https://x.com/routine.pdf"},
    ])
    _seed(tmp_db, "TSM.US", [], [
        {"ann_title": "6-K Report of foreign issuer Accession Number: "
                      "0001046179-26-000658", "ann_link": us_link},
    ])

    calls: list[tuple] = []

    def fake_news(db_path, link, **kwargs):
        calls.append(("news", link, kwargs))
        return {"title": "", "content": "NEWS_FULLTEXT", "status": "ok"}

    def fake_ann(db_path, link, title, stock_code=""):
        calls.append(("ann", link, stock_code))
        return f"ANN_DETAIL for {stock_code}"

    monkeypatch.setattr(df, "fetch_detail_cached", fake_news)
    monkeypatch.setattr(df, "fetch_announcement_detail", fake_ann)

    cfg = {"detail": {"enabled": True, "concurrency": 2, "news_per_stock": 5}}
    details, n_news, n_ann = rg.enrich_details(str(tmp_db.path), date, cfg)

    assert n_news == 1
    assert details["2513.HK"][news_link] == "NEWS_FULLTEXT"
    # 例行公告 (翌日披露报表) 不进任务; 美股 6-K 进 EDGAR 分支
    assert sorted(c[0] for c in calls) == ["ann", "ann", "news"]
    # 资讯正文走长 TTL 缓存 (公告仍用默认 24h)
    news_call = [c for c in calls if c[0] == "news"][0]
    assert news_call[2]["max_age_seconds"] == 30 * 86400
    assert ("ann", us_link, "TSM.US") in calls
    assert n_ann == 2

    rows = tmp_db.conn.execute(
        "SELECT stock_code, ann_title, ann_detail FROM announcements"
    ).fetchall()
    fetched = {r["stock_code"]: r["ann_detail"] for r in rows if r["ann_detail"]}
    assert fetched["2513.HK"].startswith("ANN_DETAIL")
    assert fetched["TSM.US"] == "ANN_DETAIL for TSM.US"
    # 例行公告只留标题, 不抓详情
    routine = [r for r in rows if "翌日披露报表" in r["ann_title"]]
    assert len(routine) == 1 and routine[0]["ann_detail"] == ""


def test_single_failure_does_not_kill_the_batch(tmp_db, monkeypatch):
    date = tmp_db.date_str
    good = f"https://finance.sina.com.cn/jjxw/{date}/doc-good.shtml"
    bad = f"https://finance.sina.com.cn/jjxw/{date}/doc-bad.shtml"
    _seed(tmp_db, "2513.HK", [_news(good, "好新闻甲"), _news(bad, "坏新闻乙")], [])

    def fake_news(db_path, link, **_kwargs):
        if link == bad:
            raise RuntimeError("boom")
        return {"title": "", "content": "OK", "status": "ok"}

    monkeypatch.setattr(df, "fetch_detail_cached", fake_news)
    cfg = {"detail": {"enabled": True, "concurrency": 2, "news_per_stock": 5}}

    details, n_news, _ = rg.enrich_details(str(tmp_db.path), date, cfg)
    assert n_news == 1
    assert details["2513.HK"] == {good: "OK"}


def _long_post(title: str = "某公司今日讨论帖") -> dict:
    """过得了日报 min_length(30) 闸的普通讨论帖。"""
    return {"type": "discussion", "title": title,
            "content": "内容" * 30, "time": "1小时前", "link": "https://x/1"}


def test_old_news_not_fetched_when_report_uses_today_window(tmp_db, monkeypatch):
    """有当日帖 → 日报用当日窗口 → 不该再去抓 5 天前资讯的全文。"""
    date = tmp_db.date_str
    old_link = "https://xueqiu.com/S/SZ000933/410143037"
    new_link = "https://xueqiu.com/S/SZ000933/411499999"
    old = _news(old_link, "五天前的旧闻")
    old["time"] = "5天前"
    _seed(tmp_db, "000933.SZ", [_long_post(), old, _news(new_link, "今日新闻")], [])

    calls: list[str] = []

    def fake_news(_db_path, link, **_kwargs):
        calls.append(link)
        return {"title": "", "content": "FULL", "status": "ok"}

    monkeypatch.setattr(df, "fetch_detail_cached", fake_news)
    cfg = {"detail": {"enabled": True, "concurrency": 1, "news_per_stock": 5},
           "llm": {"min_post_length": 30}}

    details, n_news, _ = rg.enrich_details(str(tmp_db.path), date, cfg)
    assert calls == [new_link]
    assert n_news == 1
    assert old_link not in details.get("000933.SZ", {})


def test_low_traffic_stock_falls_back_to_week_window(tmp_db, monkeypatch):
    """当日窗口无帖 → 日报回退 7 日窗口 → 详情闸同步放宽, 5 天前资讯也抓。"""
    date = tmp_db.date_str
    old_link = "https://xueqiu.com/S/SZ000933/410143037"
    old = _news(old_link, "五天前的旧闻")
    old["time"] = "5天前"
    _seed(tmp_db, "000933.SZ", [old], [])

    calls: list[str] = []

    def fake_news(_db_path, link, **_kwargs):
        calls.append(link)
        return {"title": "", "content": "FULL", "status": "ok"}

    monkeypatch.setattr(df, "fetch_detail_cached", fake_news)
    cfg = {"detail": {"enabled": True, "concurrency": 1, "news_per_stock": 5},
           "llm": {"min_post_length": 30}}

    details, n_news, _ = rg.enrich_details(str(tmp_db.path), date, cfg)
    assert calls == [old_link]
    assert details["000933.SZ"][old_link] == "FULL"
