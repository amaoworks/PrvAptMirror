"""One-line structured events on stderr. Never log secrets."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone


def emit(name: str, **fields: object) -> None:
    payload = {"event": name, "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    for key, value in fields.items():
        if value is None:
            continue
        payload[key] = value
    print(json.dumps(payload, ensure_ascii=False, default=str), file=sys.stderr, flush=True)
