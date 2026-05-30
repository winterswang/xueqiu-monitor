#!/usr/bin/env python3
"""Sync watchlist from Longbridge to data/watchlist.json.

Reads cached OAuth token (~/.longbridge/openapi/tokens/*), refreshes
if expired, then fetches the user's watchlist via Longbridge OpenAPI.

Requires LONGBRIDGE_APP_KEY + LONGBRIDGE_APP_SECRET in .env or env.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

OUTPUT_PATH = PROJECT_ROOT / "data" / "watchlist.json"
TOKEN_DIR = Path.home() / ".longbridge" / "openapi" / "tokens"
BASE_URL = os.environ.get("LONGBRIDGE_HTTP_URL", "https://openapi.longbridge.com")


def _load_cached_token() -> dict | None:
    """Read the cached OAuth token file."""
    if not TOKEN_DIR.exists():
        return None
    token_files = list(TOKEN_DIR.iterdir())
    if not token_files:
        return None
    with open(token_files[0]) as f:
        return json.load(f)


def _save_cached_token(data: dict) -> None:
    """Update cached token file after refresh."""
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    token_files = list(TOKEN_DIR.iterdir())
    target = token_files[0] if token_files else TOKEN_DIR / "token.json"
    with open(target, "w") as f:
        json.dump(data, f)


def _refresh_token(refresh_token: str, app_key: str, app_secret: str) -> dict | None:
    """Exchange refresh_token for a new access_token via Longbridge OAuth API."""
    try:
        resp = requests.post(
            f"{BASE_URL}/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": app_key,
                "client_secret": app_secret,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            print("🔄 Token refreshed")
            return data
        print(f"⚠️  Token refresh failed: {resp.status_code} {resp.text[:200]}")
        return None
    except requests.RequestException as e:
        print(f"⚠️  Token refresh error: {e}")
        return None


def _fetch_watchlist(access_token: str) -> list[dict] | None:
    """Fetch the user's watchlist via Longbridge OpenAPI."""
    try:
        resp = requests.get(
            f"{BASE_URL}/v1/watchlist/groups",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        if resp.status_code == 401:
            return None  # Token expired
        if resp.status_code != 200:
            print(f"⚠️  API error: {resp.status_code} {resp.text[:200]}")
            return None

        data = resp.json()
        stocks = []
        for group in data.get("groups", []):
            for sec in group.get("securities", []):
                symbol = sec.get("symbol", "")
                name_cn = sec.get("name_cn", "") or sec.get("name_en", "")
                stocks.append({"stock_code": symbol, "stock_name": name_cn})
        return stocks

    except requests.RequestException as e:
        print(f"⚠️  API request error: {e}")
        return None


def main():
    app_key = os.environ.get("LONGBRIDGE_APP_KEY", "")
    app_secret = os.environ.get("LONGBRIDGE_APP_SECRET", "")
    if not app_key or not app_secret:
        print("❌ LONGBRIDGE_APP_KEY / LONGBRIDGE_APP_SECRET 未设置")
        sys.exit(1)

    # Load cached token
    token = _load_cached_token()
    if not token:
        print("❌ 未找到缓存的 token (~/.longbridge/openapi/tokens/)")
        print("  请先通过 longbridge CLI 或 SDK 完成 OAuth 登录")
        sys.exit(1)

    # Refresh token if expired
    access_token = token.get("access_token", "")
    if token.get("expires_at", 0) < time.time():
        refresh_token = token.get("refresh_token", "")
        if refresh_token:
            new_token = _refresh_token(refresh_token, app_key, app_secret)
            if new_token:
                merged = {**token, **new_token}
                merged["expires_at"] = int(time.time()) + new_token.get(
                    "expires_in", 86400
                )
                _save_cached_token(merged)
                access_token = merged["access_token"]
            else:
                print("❌ Token 刷新失败，尝试用旧 token...")
                # Still try with old token (will get 401)
        else:
            print("⚠️  无 refresh_token，尝试用旧 token...")

    # Fetch watchlist
    stocks = _fetch_watchlist(access_token)
    if stocks is None:
        print("❌ API 返回 401，token 无效")
        print("  请重新登录: lark-cli 或 longbridge CLI")
        sys.exit(1)

    if not stocks:
        print("⚠️  watchlist 为空")
        sys.exit(1)

    # Deduplicate
    seen = set()
    unique = []
    for s in stocks:
        if s["stock_code"] not in seen:
            seen.add(s["stock_code"])
            unique.append(s)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(unique, ensure_ascii=False, indent=2) + "\n")
    print(f"✅ 自选股同步完成: {len(unique)} 只 → {OUTPUT_PATH}")
    for s in unique[:20]:
        print(f"   {s['stock_code']:20s} {s['stock_name']}")
    if len(unique) > 20:
        print(f"   ... 等 {len(unique)} 只")


if __name__ == "__main__":
    main()