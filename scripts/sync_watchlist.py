#!/usr/bin/env python3
"""Sync watchlist from Longbridge to data/watchlist.json.

Usage:
    python scripts/sync_watchlist.py

Requires LONG api key (set in .env or env):
    LONGBRIDGE_APP_KEY     — App Key from Longbridge OpenAPI
    LONGBRIDGE_APP_SECRET  — App Secret

If the SDK fails to authenticate, the script exits without overwriting
the existing watchlist.json.
"""

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env if available
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

OUTPUT_PATH = PROJECT_ROOT / "data" / "watchlist.json"


def _to_dot_code(symbol: str) -> str:
    """Convert Longbridge symbol (e.g. 700.HK) to dot-separated format.
    
    Longbridge uses formats like:
      - 700.HK, 9988.HK
      - 600519.SH, 000858.SZ  
      - AAPL.US
    
    These are already in the format we need.
    """
    return symbol


def main():
    app_key = os.environ.get("LONGBRIDGE_APP_KEY", "")
    app_secret = os.environ.get("LONGBRIDGE_APP_SECRET", "")

    if not app_key:
        print("❌ LONGBRIDGE_APP_KEY 未设置，跳过同步")
        sys.exit(0)

    try:
        from longbridge.openapi import HttpClient, TradeContext
    except ImportError:
        print("❌ longbridge SDK 未安装 (pip install longbridge)")
        sys.exit(1)

    try:
        client = HttpClient(
            app_key=app_key,
            app_secret=app_secret,
            access_token="",
        )
        ctx = TradeContext(client)
        resp = ctx.user_watchlist()
    except Exception as e:
        print(f"❌ 获取自选股失败: {e}")
        print("  当前 watchlist.json 未被修改")
        sys.exit(1)

    stocks = []
    for group in resp:
        for sec in group.securities:
            code = _to_dot_code(sec.symbol)
            name = sec.name_cn or sec.name_en or ""
            stocks.append({"stock_code": code, "stock_name": name})

    if not stocks:
        print("❌ 自选股列表为空，跳过写入")
        sys.exit(1)

    # Deduplicate by stock_code
    seen = set()
    unique = []
    for s in stocks:
        if s["stock_code"] not in seen:
            seen.add(s["stock_code"])
            unique.append(s)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(unique, ensure_ascii=False, indent=2) + "\n"
    )
    print(f"✅ 自选股同步完成: {len(unique)} 只 → {OUTPUT_PATH}")
    for s in unique[:5]:
        print(f"   {s['stock_code']:20s} {s['stock_name']}")
    if len(unique) > 5:
        print(f"   ... 等 {len(unique)} 只")


if __name__ == "__main__":
    main()
