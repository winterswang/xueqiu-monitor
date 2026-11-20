#!/usr/bin/env python3
"""对照实验：抽取式（有没有具体信息点）vs 判断式（值不值得看）的稳定性。"""
from __future__ import annotations
import argparse, json, re, sys, time
from pathlib import Path
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))
import src.config  # noqa
from src.llm_config import resolve_model
from src.report_generator import _get_llm_client
import importlib.util
spec = importlib.util.spec_from_file_location("ar", PROJECT_DIR/"scripts"/"assistant_report2.py")
ar = importlib.util.module_from_spec(spec); spec.loader.exec_module(ar)

PROMPT = """下面是「__NAME__」在 __DATE__ 的帖子。

对每一条，只做**抽取**，不做评价：

- 如果帖子里有具体信息，输出：
  - kind：一手 / 硬数字 / 产业细节 / 可核验 / 反常
  - fact：40 字以内，陈述这条帖子说了什么，保留数字
  - nums：帖子里出现的关键数字（最多 5 个，原样字符串）
- 如果没有任何具体信息（纯情绪、口号、提问、与公司无关），输出 kind="无"

必须对每一条都输出，不能遗漏、不能合并。

输出严格 JSON：
{"items":[{"id":123,"kind":"硬数字","fact":"...","nums":["31.9亿美元"]},{"id":124,"kind":"无"}]}

帖子列表：
__POSTS__
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", required=True); ap.add_argument("--date", required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--maxtok", type=int, default=32000)
    a = ap.parse_args()
    cfg = json.loads((PROJECT_DIR/"etc"/"config.report.json").read_text(encoding="utf-8"))
    model = resolve_model(cfg); name = cfg["stocks"][a.stock]["name"]
    posts = ar.prefilter(ar.fetch_day_posts(str(PROJECT_DIR/"data"/"monitor.db"), a.date).get(a.stock, []))
    body = "\n".join(f"#{p['id']} | {p['author']} | {(p['content'] or '').strip()}" for p in posts)
    p = PROMPT.replace("__NAME__", name).replace("__DATE__", a.date).replace("__POSTS__", body)
    cl = _get_llm_client(); t0 = time.time()
    rsp = cl.chat.completions.create(model=model, max_tokens=a.maxtok, temperature=0.1,
                                     messages=[{"role":"user","content":p}])
    txt = (rsp.choices[0].message.content or "").strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
    try:
        data = json.loads(txt)
    except ValueError:
        print("JSON 失败:", txt[:200]); return
    items = {int(i["id"]): i for i in data.get("items", []) if "id" in i}
    has = sorted(k for k, v in items.items() if v.get("kind") and v.get("kind") != "无")
    res = {"sent": len(posts), "judged": len(items), "coverage": round(len(items)/max(1,len(posts)),3),
           "has_info": has, "wall": round(time.time()-t0,1), "finish": rsp.choices[0].finish_reason,
           "items": items}
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    print(f"{a.stock} 送入{len(posts)} 判出{len(items)} 有信息{len(has)} 用时{res['wall']}s")

if __name__ == "__main__":
    main()
