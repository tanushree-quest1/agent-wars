import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
DATA = ROOT / "data"
(DATA / "uploads").mkdir(parents=True, exist_ok=True)

DB_PATH = os.getenv("D2D_DB", str(DATA / "d2d.sqlite"))
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
MAPS_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.6"))
AUTO_COST_LIMIT_INR = float(os.getenv("AUTO_COST_LIMIT_INR", "15000"))
LEARN_MIN_SAMPLES = int(os.getenv("LEARN_MIN_SAMPLES", "2"))
# Temporal: empty = disabled (REST falls back to direct calls, worker not started)
TEMPORAL_URL = os.getenv("TEMPORAL_URL", "")
TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE", "dock-to-door")
WORKFLOW_PREFIX = os.getenv("TEMPORAL_WF_PREFIX", "incident-")   # unique per deployment: open workflows can't share ids
# ClickHouse: empty = disabled (append sink no-ops, reads fall back to SQLite)
CLICKHOUSE_URL = os.getenv("CLICKHOUSE_URL", "")
CLICKHOUSE_DB = os.getenv("CLICKHOUSE_DB", "d2d")
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER", "default")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "")
NIGHTLY_REVIEW_MIN = int(os.getenv("NIGHTLY_REVIEW_MIN", "1440"))   # feedback loop interval
HANDLING_MIN = 25      # cargo transfer time
COST_PER_KM = 38       # INR
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
