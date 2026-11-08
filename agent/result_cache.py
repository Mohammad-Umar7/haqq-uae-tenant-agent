"""Free repeats: a tiny on-disk cache of finished answers.

If a judge clicks "Load sample contract" and "Analyze" twice, the second run
should cost $0. We hash everything that can change the answer:

    contract bytes + the full user message (question, form details, today's
    date) + model + effort + a fingerprint of the prompts and tool schemas

and store the final answer and its trace as one JSON file named after the
hash. Change any of those and the key changes, so a stale answer can't be
served. (Prompt caching on the API side is different: it makes the SAME
prefix cheaper; this cache skips the API call entirely.)

The folder is configurable (HAQQ_CACHE_DIR) and git-ignored.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from agent.trace import TraceEvent

log = logging.getLogger("haqq.cache")


def make_key(*parts: bytes | str) -> str:
    """sha256 over the parts, with a separator so ("ab","c") != ("a","bc")."""
    h = hashlib.sha256()
    for part in parts:
        data = part.encode("utf-8") if isinstance(part, str) else part
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


def event_to_dict(event: TraceEvent) -> dict[str, Any]:
    return json.loads(json.dumps(dataclasses.asdict(event), ensure_ascii=False, default=str))


def event_from_dict(data: dict[str, Any]) -> TraceEvent:
    return TraceEvent(**data)


class ResultCache:
    """One JSON file per key in `directory`. Disabled caches do nothing."""

    def __init__(self, directory: Path | str, enabled: bool = True) -> None:
        self.directory = Path(directory)
        self.enabled = enabled

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        path = self._path(key)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:   # a corrupt entry is just a miss
            log.warning("Ignoring unreadable cache entry %s: %s", path.name, exc)
            return None

    def put(self, key: str, value: dict[str, Any]) -> None:
        if not self.enabled:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        # Write to a temp file then rename, so a crash never leaves half a file.
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, default=str)
        os.replace(tmp, self._path(key))
