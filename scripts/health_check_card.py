#!/usr/bin/env python3
"""Render health_check.py JSON as a readable Feishu alert card."""

from __future__ import annotations

import json
import sys
from datetime import datetime


LABELS = {
    "db_snapshots": "数据库快照",
    "db_exists": "数据库文件",
    "pipeline_completed": "当日批次完成度",
    "crawl_failures": "个股爬取失败",
    "detect_errors": "检测处理错误",
    "watchlist": "自选股清单",
    "source_failures": "数据源失败",
    "disk_usage": "数据目录占用",
    "crawl_freshness": "爬取新鲜度",
    "time_parse_rate": "发布时间解析",
    "post_freshness": "旧帖比例",
    "daily_report": "当日日报产物",
    "log_exists": "运行日志",
}
STATUS = {"ok": "✅ 正常", "warn": "⚠️ 关注", "fail": "❌ 失败"}


def guidance(check: str, detail: str) -> str:
    if check == "daily_report":
        return "检查 19:00 的 xueqiu-daily-report 运行记录及 logs/report-generation.log，确认日报是否写入 data/daily_reports。"
    if check == "pipeline_completed":
        return "查看详情中列出的批次日志，确认对应 pipeline-g 任务是否启动并写出 [SUMMARY]。"
    if check in {"source_failures", "crawl_failures"}:
        return "按详情中的来源和时间窗口查看 logs/source_failures.jsonl 与对应 pipeline 日志，确认是单源波动还是持续失败。"
    if check == "crawl_freshness":
        return "查看过期股票所属分组的最近运行记录，并确认任务调度和雪球数据源可用。"
    if check == "time_parse_rate":
        return "抽查无法解析的帖子时间格式，确认上游格式变化后再调整解析器。"
    if check == "post_freshness":
        return "这是存量帖子年龄指标；结合爬取新鲜度判断。若持续上升，再检查增量抓取覆盖。"
    if check == "disk_usage":
        return "确认增长来源后清理可再生缓存或旧日志，保留数据库和原始数据。"
    if check in {"db_snapshots", "db_exists", "watchlist", "log_exists"}:
        return "检查详情所指向的本地数据文件是否存在、可读取且有近期更新。"
    if check == "detect_errors":
        return "在对应批次日志中定位错误上下文，确认是否影响后续分析或日报生成。"
    return "查看对应检查项的原始日志和数据，确认影响范围后处理。"


def escape_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def render(report: dict) -> dict:
    # 只接受 dict 元素：本脚本是告警链路的最后兜底,遇到畸形 checks
    # (如字符串元素)也必须渲染出卡片,而不是抛 AttributeError 崩掉 ——
    # 崩了连 --text 回退一起失败,只剩硬编码文案。
    raw_checks = report.get("checks")
    checks = [c for c in raw_checks if isinstance(c, dict)] if isinstance(raw_checks, list) else []
    overall = str(report.get("status", "fail")).lower()
    failed = [c for c in checks if c.get("status") == "fail"]
    warned = [c for c in checks if c.get("status") == "warn"]
    passed = [c for c in checks if c.get("status") == "ok"]

    if overall == "fail":
        title, template, summary = "雪球日更检查未通过", "red", "❌ 有检查未通过"
    elif overall == "warn":
        title, template, summary = "雪球日更检查需关注", "orange", "⚠️ 存在需要关注的项目"
    else:
        title, template, summary = "雪球日更检查正常", "green", "✅ 检查通过"

    table = ["### 检查结果", "", "| 检查项 | 状态 | 详情 |", "|---|---|---|"]
    for item in checks:
        name = LABELS.get(item.get("check", ""), item.get("check", "未知检查"))
        state = STATUS.get(item.get("status", ""), "❔ 未知")
        table.append("| %s | %s | %s |" % (
            escape_cell(name), escape_cell(state), escape_cell(item.get("detail", "无详情"))))

    issues = [c for c in checks if c.get("status") in {"fail", "warn"}]
    issue_lines = ["### 需要处理 / 留意", ""]
    if not issues:
        issue_lines.append("没有异常项。")
    else:
        for item in issues:
            key = item.get("check", "")
            name = LABELS.get(key, key or "未知检查")
            state = STATUS.get(item.get("status", ""), "❔ 未知")
            detail = str(item.get("detail", "无详情")).strip()
            issue_lines.append("**%s %s**  " % (state, name))
            # detail 来自日志/异常文本,可能含 | 或换行,不转义会破坏卡片版式。
            issue_lines.append(escape_cell(detail))
            issue_lines.append("建议：" + guidance(key, detail))
            issue_lines.append("")

    elements = [
        {"tag": "markdown", "content": "**%s**\n\n检查时间：%s\n\n结果：%d 项正常 · %d 项关注 · %d 项失败" % (
            summary, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), len(passed), len(warned), len(failed))},
        {"tag": "hr"},
        {"tag": "markdown", "content": "\n".join(table)},
        {"tag": "hr"},
        {"tag": "markdown", "content": "\n".join(issue_lines)},
    ]
    errors = report.get("errors") or []
    if errors:
        elements.extend([
            {"tag": "hr"},
            {"tag": "markdown", "content": "<font color='grey'>检查码：%s</font>" % escape_cell(", ".join(map(str, errors)))},
        ])
    return {"config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "elements": elements}


def main() -> int:
    raw = sys.stdin.read()
    try:
        report = json.loads(raw)
        if not isinstance(report, dict):
            raise ValueError("检查结果不是 JSON 对象")
    except Exception as exc:
        report = {"status": "fail", "errors": ["health_check_output_invalid"], "checks": [
            {"check": "检查器输出", "status": "fail", "detail": "%s；输出：%s" % (exc, raw[:1200])}
        ]}
    card = render(report)
    if "--text" in sys.argv:
        for element in card["elements"]:
            if element.get("tag") == "markdown":
                print(element["content"])
    else:
        print(json.dumps(card, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
