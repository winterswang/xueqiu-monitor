#!/usr/bin/env python3
"""判定策略对比实验：整票一次 vs 分块；gist 长短；覆盖率与一致性。"""
from __future__ import annotations
import argparse, json, re, sys, time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))
import src.config  # noqa
from src.llm_config import resolve_model
from src.report_generator import _get_llm_client
sys.path.insert(0, str(PROJECT_DIR / "scripts"))
import importlib.util
spec = importlib.util.spec_from_file_location("ar", PROJECT_DIR / "scripts" / "assistant_report2.py")
ar = importlib.util.module_from_spec(spec); spec.loader.exec_module(ar)


def prompt_for(gist_len: int) -> str:
    return """你是投研助理，正在为一位自己会读公告和研报的投资者筛选「__NAME__（__CODE__）」在 __DATE__ 的帖子。

对每条帖子，只回答一个问题：**这条帖子里有没有可以提取出来的具体信息？**

具体信息指：一手观察（实地/从业/亲历且有细节）、具体数字（财报/公告/回购/产能/交易）、
产业细节（工艺/交期/供应链/竞争）、可核验引用（媒体/会议/公告）、有论据的反向推理。

判定：
- read：有具体信息
- maybe：相关，但与前面某条 read 说的是同一件事
- skip：没有任何具体信息（纯情绪、口号、求助、提问、与公司无关）

对 read / maybe 给 tag（一手/硬数字/产业细节/可核验/反常）和 gist（不超过 __GIST__ 字，只陈述不评价）。

**必须对输入的每一条帖子都给出判定，不能遗漏，不能合并。**

输出严格 JSON（无代码块标记）：
{"items":[{"id":123,"verdict":"read","tag":"硬数字","gist":"..."}]}

帖子列表：
__POSTS__
""".replace("__GIST__", str(gist_len))


def run(client, model, name, code, posts, date, chunk, gist_len, maxtok=12000, retries=1):
    P = prompt_for(gist_len)
    chunks = [posts] if chunk <= 0 else [posts[i:i+chunk] for i in range(0, len(posts), chunk)]
    allv, calls = {}, []
    t0 = time.time()
    for ch in chunks:
        body = "\n".join(
            f"#{p['id']} | {p['author']} | 赞{p['like_count']} 评{p['comment_count']} | {(p['content'] or '').strip()}"
            for p in ch)
        t1 = time.time()
        txt, finish = "", "none"
        for _ in range(retries):
            rsp = client.chat.completions.create(
                model=model, max_tokens=maxtok, temperature=0.1,
                messages=[{"role": "user", "content": P.replace("__NAME__", name).replace("__CODE__", code).replace("__DATE__", date).replace("__POSTS__", body)}])
            txt = (rsp.choices[0].message.content or "").strip()
            finish = rsp.choices[0].finish_reason
            if txt:
                break
        calls.append({"sent": len(ch), "sec": round(time.time()-t1, 1), "finish": finish,
                      "out_chars": len(txt)})
        if not txt:
            continue
        txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
        try:
            data = json.loads(txt) if txt else {"items": []}
        except ValueError:
            logger = __import__("logging").getLogger("exp")
            logger.warning(f"JSON 解析失败，片段: {txt[:120]}")
            continue
        for it in data.get("items", []):
            if "id" in it:
                allv[int(it["id"])] = it
    return allv, calls, round(time.time()-t0, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--chunk", type=int, default=0)
    ap.add_argument("--gist", type=int, default=40)
    ap.add_argument("--out", required=True)
    ap.add_argument("--maxtok", type=int, default=12000)
    ap.add_argument("--retries", type=int, default=1)
    a = ap.parse_args()

    cfg = json.loads((PROJECT_DIR / "etc" / "config.report.json").read_text(encoding="utf-8"))
    model = resolve_model(cfg)
    name = cfg["stocks"][a.stock]["name"]
    posts = ar.prefilter(ar.fetch_day_posts(str(PROJECT_DIR/"data"/"monitor.db"), a.date).get(a.stock, []))
    client = _get_llm_client()
    v, calls, secs = run(client, model, name, a.stock, posts, a.date, a.chunk, a.gist, a.maxtok, a.retries)

    sent = {p["id"] for p in posts}
    got = set(v)
    res = {
        "stock": a.stock, "date": a.date, "chunk": a.chunk, "gist": a.gist,
        "posts_sent": len(posts), "judged": len(got), "coverage": round(len(got)/max(1,len(posts)), 3),
        "missing": sorted(sent - got),
        "read": sorted([i for i, x in v.items() if x.get("verdict") == "read"]),
        "maybe": sorted([i for i, x in v.items() if x.get("verdict") == "maybe"]),
        "calls": calls, "wall_sec": secs, "verdicts": v,
    }
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{a.stock} chunk={a.chunk} gist={a.gist} | 送入{len(posts)} 判出{len(got)} "
          f"覆盖{res['coverage']:.0%} read={len(res['read'])} | 调用{len(calls)}次 {secs}s")


if __name__ == "__main__":
    main()
