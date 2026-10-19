"""Metadata-only JSONL trace. No prompts, tool arguments, HTTP bodies or keys."""
import json
import threading
import sys
from datetime import datetime, timezone
from pathlib import Path


class Trace:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self.failed = False
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **metadata):
        row = {"at": datetime.now(timezone.utc).isoformat(), "event": event, **metadata}
        if self.path:
            try:
                with self._lock, self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError:
                if not self.failed:
                    print("Warning: trace write failed; the run will continue without a complete trace.", file=sys.stderr)
                self.failed = True
