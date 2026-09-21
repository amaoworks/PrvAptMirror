import hashlib
import json
from pathlib import Path

from prvaptmirror.byhash import HISTORY_FILE, RETENTION_SECONDS, prepare_by_hash


def _generation(root: Path, body: bytes) -> Path:
    index = root / "stable/main/binary-amd64/Packages"
    index.parent.mkdir(parents=True)
    index.write_bytes(body)
    return index


def _hash_path(body: bytes) -> str:
    return "stable/main/binary-amd64/by-hash/SHA256/" + hashlib.sha256(body).hexdigest()


def test_by_hash_retains_week_and_at_least_two_previous_versions(tmp_path):
    live = tmp_path / "absent"
    bodies = [str(number).encode() for number in range(5)]
    for number, timestamp in enumerate([0, 1, 2, 3, RETENTION_SECONDS + 4]):
        staged = tmp_path / str(number)
        _generation(staged, bodies[number])
        prepare_by_hash(staged, live, now=timestamp)
        if number == 3:
            # A client with the first Release still succeeds after three publishes.
            assert (staged / _hash_path(bodies[0])).read_bytes() == bodies[0]
        live = staged
    assert not (live / _hash_path(bodies[0])).exists()
    assert not (live / _hash_path(bodies[1])).exists()
    for body in bodies[2:]:
        assert (live / _hash_path(body)).read_bytes() == body


def test_unchanged_indices_renew_retention_without_mutating_live(tmp_path):
    first = tmp_path / "first"
    _generation(first, b"same")
    prepare_by_hash(first, tmp_path / "absent", now=0)
    second = tmp_path / "second"
    _generation(second, b"same")
    prepare_by_hash(second, first, now=RETENTION_SECONDS)
    assert (first / _hash_path(b"same")).stat().st_mtime == 0
    assert (second / _hash_path(b"same")).stat().st_mtime == RETENTION_SECONDS
    history = json.loads((second / HISTORY_FILE).read_text())
    assert history["stable/main/binary-amd64/Packages"] == [hashlib.sha256(b"same").hexdigest()]


def test_long_idle_then_burst_keeps_previously_current_index_for_a_full_week(tmp_path):
    live = tmp_path / "first"
    _generation(live, b"old")
    prepare_by_hash(live, tmp_path / "absent", now=0)
    initial = live
    for number in range(4):
        staged = tmp_path / f"new-{number}"
        _generation(staged, str(number).encode())
        prepare_by_hash(staged, live, now=2 * RETENTION_SECONDS + number)
        live = staged
    assert (live / _hash_path(b"old")).read_bytes() == b"old"
    assert (initial / _hash_path(b"old")).stat().st_mtime == 0
    assert (live / _hash_path(b"old")).stat().st_mtime == 2 * RETENTION_SECONDS
