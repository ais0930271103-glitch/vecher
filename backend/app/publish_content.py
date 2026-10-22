"""Validate and atomically publish a local editorial snapshot, without an API restart.

Run in the backend environment as the administrator of the public editorial
directory. The API needs read-only access to that directory, never CMS access.
No user-controlled path, remote URL, or private database is consulted here.
"""
import argparse
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .content_catalogue import Catalogue, read_snapshot, validate_bytes


class PublicationConflict(ValueError):
    """The operator's reviewed previous snapshot is no longer current."""


def validate_source(path: Path) -> Catalogue:
    snapshot = validate_bytes(read_snapshot(path)[0])
    video_ids = {item.video_id for item in snapshot.items if item.kind == "video" and item.active}
    if video_ids:
        from .video_catalogue import catalogue
        available = {item.id for item in catalogue() if item.active}
        if not video_ids.issubset(available):
            raise ValueError("An active video publication refers to an unavailable reviewed video")
    return snapshot


@contextmanager
def _publication_lock(path: Path):
    # Publication runs on the Linux server. Keeping the lock file avoids a
    # second process locking a replacement inode while the first still owns
    # the old one. The directory must be owned by the publishing operator.
    import fcntl

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Publication lock must be a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def publish(source: Path, destination: Path, *, expected_revision: str | None = None,
            create: bool = False) -> Catalogue:
    """Compare-and-swap a validated snapshot; failures before replace preserve it."""
    if create == (expected_revision is not None):
        raise ValueError("Choose either create or the reviewed current revision")
    if expected_revision is not None and (len(expected_revision) != 64
            or any(ch not in "0123456789abcdef" for ch in expected_revision)):
        raise ValueError("Expected revision must be a lowercase SHA-256 digest")
    snapshot = validate_source(source)
    destination = Path(destination)
    parent = destination.parent
    if not parent.is_dir() or destination.name in {"", ".", ".."}:
        raise ValueError("Create the managed editorial directory first")
    # Canonical public data only; validation drops no unrecognised fields.
    raw = (json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False,
                      indent=2) + "\n").encode("utf-8")
    # Canonical indentation can expand the input; the installed result must
    # still meet the exact runtime size policy.
    validate_bytes(raw)
    with _publication_lock(parent / ("." + destination.name + ".publish.lock")):
        if destination.is_symlink():
            raise ValueError("Publication destination must not be a symlink")
        if destination.exists():
            if create:
                raise PublicationConflict("Editorial snapshot already exists")
            current = validate_bytes(read_snapshot(destination)[0])
            if current.revision != expected_revision:
                raise PublicationConflict("Editorial snapshot changed; review the current revision")
        elif not create:
            raise PublicationConflict("Reviewed editorial snapshot no longer exists")
        fd, name = tempfile.mkstemp(prefix="." + destination.name + ".", suffix=".tmp", dir=parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as target:
                os.fchmod(target.fileno(), 0o644)
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, destination)
            directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--expected-revision")
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if args.check_only:
        snapshot = validate_source(args.source)
        print(f"CONTENT_VALIDATED={snapshot.revision};ITEMS={len(snapshot.items)}")
        return
    if args.destination is None:
        parser.error("--destination is required for publication")
    snapshot = publish(args.source, args.destination, expected_revision=args.expected_revision, create=args.create)
    print(f"CONTENT_PUBLISHED={snapshot.revision};ITEMS={len(snapshot.items)}")


if __name__ == "__main__":
    main()
