"""Diagnostic: capture raw LLM response for sentiment analysis."""
import json, sys, os, logging
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)
    print(f"Loaded .env from {env_path}")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.chdir(str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# Load today's 300750.SZ posts
import sqlite3
db = sqlite3.connect("data/monitor.db")
row = db.execute("SELECT posts_data FROM crawl_snapshots WHERE id=661").fetchone()
posts = json.loads(row[0])
print(f"Loaded {len(posts)} posts for 300750.SZ")

import sentiment

# Re-init client (force fresh)
sentiment._client = None
client = sentiment._get_client()

if not client:
    print("ERROR: client not available")
    sys.exit(1)

print(f"Client ready. Running sentiment analysis...")

# Monkey-patch to capture raw LLM response BEFORE _extract_text
original_extract = sentiment._extract_text
def debug_extract(response):
    print(f"\n=== RESPONSE ===")
    print(f"  stop_reason: {response.stop_reason}")
    print(f"  content blocks: {len(response.content)}")
    for i, block in enumerate(response.content):
        btype = type(block).__name__
        print(f"  block[{i}]: {btype}")
        if hasattr(block, 'text'):
            t = block.text or ""
            print(f"    text: {len(t)} chars → {repr(t[:500])}")
        if hasattr(block, 'thinking'):
            th = block.thinking or ""
            print(f"    thinking: {len(th)} chars → {repr(th[:300])}")
    
    if hasattr(response, 'usage'):
        u = response.usage
        print(f"  usage: input={getattr(u, 'input_tokens', '?')}, output={getattr(u, 'output_tokens', '?')}")

    result = original_extract(response)
    print(f"  _extract_text → {len(result)} chars: {repr(result[:300])}")
    return result

sentiment._extract_text = debug_extract

# Run
scores = sentiment.analyze_sentiment_batch(posts)

pos = sum(1 for s in scores if abs(s) > 0.05)
zero = len(scores) - pos
print(f"\nFinal: non-zero={pos}/{len(scores)}")
print(f"Scores: {[round(s,1) for s in scores]}")
