"""Bounded, explicit local file tools; never execute uploaded content."""
import base64
import hashlib
import os
from pathlib import Path
import stat
from .api import APIError

MAX_FILE = 1_000_000


def read_file(value, limit=MAX_FILE, *, durable=False):
    path = Path(value).expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise APIError("absolute_regular_file_required")
    # O_NOFOLLOW prevents a leaf replacement between validation and opening.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise APIError("file_limit")
        data = stream.read(limit + 1)
        if durable:
            os.fsync(stream.fileno())
    if len(data) > limit:
        raise APIError("file_limit")
    if durable:
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    return data


def save_file(path, payload, metadata):
    data = base64.b64decode(payload, validate=True)
    if len(data) > MAX_FILE or len(data) != metadata["size"] or hashlib.sha256(data).hexdigest() != metadata["sha256"]:
        raise APIError("transfer_integrity_failed")
    target = Path(path).expanduser()
    if not target.is_absolute():
        raise APIError("absolute_destination_required")
    # Never overwrite; an existing exact durable file is safe retry recovery.
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if read_file(str(target), durable=True) != data:
            raise APIError("destination_exists") from None
    else:
        with os.fdopen(fd, "wb") as out:
            out.write(data); out.flush(); os.fsync(out.fileno())
    parent = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    return {"path": str(target), "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "durable": True}


def bundle(directory):
    root = Path(directory).expanduser()
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise APIError("absolute_directory_required")
    files, size = [], 0
    for parent, directories, names in os.walk(root, followlinks=False):
        if any((Path(parent) / d).is_symlink() for d in directories):
            raise APIError("symlink_in_bundle")
        for name in sorted(names):
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            if any(part.startswith((".", "_")) or part == "node_modules" for part in path.relative_to(root).parts):
                raise APIError("unsupported_bundle_file")
            data = read_file(str(path), 5 << 20)
            size += len(data)
            if size > 20 << 20 or len(files) >= 500:
                raise APIError("site_limit")
            files.append({"path": relative, "data": base64.b64encode(data).decode()})
    if not any(f["path"] == "index.html" for f in files):
        raise APIError("index_html_required")
    return sorted(files, key=lambda f: f["path"])
