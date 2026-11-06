"""The `live` service in vercel.json: makes backend/ importable and hands over the
application. Everything else is in backend/app/main.py."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))
# Outside /tmp a Vercel function's filesystem is read-only.
if os.environ.get("VERCEL"):
    os.environ.setdefault("ESSA_STATE_DIR", "/tmp/warexx-live")
    os.makedirs(os.environ["ESSA_STATE_DIR"], exist_ok=True)

from app.main import app  # noqa: E402,F401
