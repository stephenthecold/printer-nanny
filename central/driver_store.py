"""Where uploaded driver packages live on disk, and the rules for getting there.

The bytes are deliberately NOT in the database. A driver package is 5-200 MB,
and ``/admin/backup`` streams a ``pg_dump`` through the API -- a handful of
packages would turn a fast backup into a slow multi-gigabyte one, which is how
operators stop taking backups.

The cost of that choice is real and must not be hidden: **a database restore
does not bring driver packages back**. ``missing()`` exists so the UI can say so
plainly rather than leaving an operator to discover it when a workstation fails
to provision.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import IO

#: Overridable so tests and dev installs do not need the container's volume.
_ENV_DIR = "PN_DRIVER_DIR"
_DEFAULT_DIR = "/var/lib/printer-nanny/drivers"

#: A driver archive is a big file, and an upload is an unauthenticated-shaped
#: act even from an admin -- bound it rather than letting one fill the volume.
MAX_UPLOAD_BYTES = 512 * 1024 * 1024


def root() -> Path:
    return Path(os.environ.get(_ENV_DIR) or _DEFAULT_DIR)


def path_for(client_id: int, package_id: int) -> Path:
    """Where one package's bytes live.

    Built from **integers only**. The uploaded filename never appears in a path:
    it is operator-supplied, and a path assembled from one is a directory
    traversal (``../../etc/...``) with extra steps. The original name is not
    needed to serve the file -- the download endpoint sets its own.
    """
    return root() / str(int(client_id)) / f"{int(package_id)}.pkg"


def save(client_id: int, package_id: int, source: IO[bytes]) -> tuple:
    """Stream an upload to disk. Returns ``(sha256, size)``.

    Streamed rather than read into memory: a 200 MB package read whole would be
    200 MB of the API worker's RSS per concurrent upload. The digest is computed
    on the way past, so the bytes are hashed exactly as stored -- hashing the
    request body separately would leave room for the two to disagree.

    Written to a temp name and renamed, so a half-written package is never
    visible under its final path. Over-size uploads are aborted mid-stream and
    cleaned up rather than after the disk is already full.
    """
    dest = path_for(client_id, package_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")

    digest = hashlib.sha256()
    size = 0
    try:
        with open(tmp, "wb") as fp:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise ValueError(
                        f"driver package exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"
                    )
                digest.update(chunk)
                fp.write(chunk)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    return digest.hexdigest(), size


def missing(stored_at: str) -> bool:
    """True when the row exists but the bytes do not.

    The expected case after restoring a database onto a fresh host, and the
    reason this is surfaced rather than inferred: a package that silently
    resolves to nothing would make every affected workstation report "needs a
    vendor driver" with no hint that the fix is a re-upload.
    """
    return not (stored_at and Path(stored_at).is_file())


def delete(stored_at: str) -> None:
    """Remove a package's bytes. Missing is not an error -- deleting a row whose
    file is already gone (a restored database) must still work."""
    if not stored_at:
        return
    try:
        os.unlink(stored_at)
    except FileNotFoundError:
        pass
    except OSError:  # pragma: no cover - permissions on the volume
        raise


def purge_client(client_id: int) -> None:
    """Drop a whole client's packages. Used when a client is deleted; the DB
    rows cascade, and this stops the bytes outliving them."""
    shutil.rmtree(root() / str(int(client_id)), ignore_errors=True)
