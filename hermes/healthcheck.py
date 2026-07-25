import json
import os
import sys
from datetime import datetime, timezone


path = os.environ.get("HERMES_HEALTH_PATH", "/tmp/hermes_health.json")
try:
    with open(path, encoding="utf-8") as handle:
        health = json.load(handle)
    heartbeat = datetime.fromisoformat(health["heartbeat_at"])
    age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
    if age > 180 or health.get("status") != "ok":
        raise RuntimeError(f"unhealthy heartbeat age={age:.0f}s status={health.get('status')}")
except Exception as exc:
    print(exc)
    sys.exit(1)
