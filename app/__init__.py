"""Dentify demo. Loads a local .env file (KEY=value lines) into the environment at import time,
without overriding variables that are already set. No extra dependency needed."""
import os


def _load_dotenv(path=None):
    path = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'")
        if k and v:
            os.environ.setdefault(k, v)


if not os.environ.get("PYTEST_CURRENT_TEST") and "pytest" not in os.environ.get("_", ""):
    _load_dotenv()
