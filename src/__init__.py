# xueqiu-monitor package
# ── .env loading: always runs first, before any other module ──
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except (ImportError, FileNotFoundError):
    pass
