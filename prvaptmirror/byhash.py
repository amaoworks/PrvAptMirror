"""Keep immutable index URLs available across atomic dists replacements."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

from prvaptmirror.indexer import write_atomic

HISTORY_FILE = ".by-hash-history.json"
RETENTION_SECONDS = 7 * 86400


def prepare_by_hash(staged: Path, live: Path, *, now: float | None = None) -> None:
    """Retain seven days of hashes, plus two older versions of each active index.

    The history travels with dists so a failed publish cannot advance it. Hard
    links keep retained index data immutable without copying it at every publish.
    Call only while holding publish.lock, before signing and swapping dists.
    """
    now = time.time() if now is None else now
    old_history_path = live / HISTORY_FILE
    old_history = json.loads(old_history_path.read_text()) if old_history_path.is_file() else {}
    history: dict[str, list[str]] = {}
    protected: set[Path] = set()
    previous_current: set[Path] = set()
    for index in live.glob("*/*/binary-*/*"):
        if index.name in {"Packages", "Packages.gz", "Release"}:
            digest = hashlib.sha256(index.read_bytes()).hexdigest()
            previous_current.add(index.relative_to(live).parent / "by-hash" / "SHA256" / digest)
    for index in sorted(staged.glob("*/*/binary-*/*")):
        if index.name not in {"Packages", "Packages.gz", "Release"}:
            continue
        relative = index.relative_to(staged)
        digest = hashlib.sha256(index.read_bytes()).hexdigest()
        previous = old_history.get(relative.as_posix(), [])
        if not isinstance(previous, list) or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in previous
        ):
            raise ValueError("invalid by-hash history")
        versions = list(dict.fromkeys([digest, *previous]))[:3]
        history[relative.as_posix()] = versions
        hash_dir = relative.parent / "by-hash" / "SHA256"
        protected.update(hash_dir / value for value in versions)
        destination = staged / hash_dir / digest
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            os.link(index, destination)
            # A current digest renews its retention even when its bytes did not change.
            os.utime(destination, (now, now))

    for old in live.glob("*/*/binary-*/by-hash/SHA256/*"):
        relative = old.relative_to(live)
        retiring = relative in previous_current
        if not retiring and relative not in protected and old.stat().st_mtime < now - RETENTION_SECONDS:
            continue
        destination = staged / relative
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            if retiring:
                # Start the grace period when an index stops being current, not
                # when it was created (the repository may have been idle for months).
                # Copy before touching timestamps so the live inode stays unchanged.
                shutil.copyfile(old, destination)
                os.utime(destination, (now, now))
            else:
                os.link(old, destination)
    write_atomic(staged / HISTORY_FILE, json.dumps(history, sort_keys=True).encode())
