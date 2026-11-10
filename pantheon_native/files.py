"""Confined local file tools. Shared-chat input never chooses filesystem authority."""
import base64
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import stat
from .api import APIError

MAX_FILE = 1_000_000
# These exclusions apply even when a session starts above a credential store.
PRIVATE_PARTS = {'.ssh', '.aws', '.azure', '.kube', '.gnupg', '.config', '.hermes',
                 '.codex', '.claude', 'keychains', 'keyrings'}
PRIVATE_NAMES = {'auth.json', 'credentials', 'credentials.json', 'credentials.db',
                 'access_tokens.db', 'application_default_credentials.json',
                 'token.json', 'tokens.json', 'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519'}
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _private(path):
    return any(part.casefold() in PRIVATE_PARTS or part.casefold() in PRIVATE_NAMES
               or part.casefold().startswith('.env') for part in path.parts)


def hermes_home():
    from hermes_constants import get_hermes_home
    return get_hermes_home().resolve()


def workspace_root(ctx):
    value = ctx.session_cwd()
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise APIError('session_workspace_required')
    root = Path(value).resolve(strict=True)
    home = Path.home().resolve()
    hermes = hermes_home()
    if root == Path(root.anchor) or root == home or root.is_relative_to(hermes) or _private(root):
        raise APIError('session_workspace_required')
    return root


def download_root():
    from plugins.plugin_storage import plugin_data_dir
    # Resolve the host-owned profile root, not an attacker-controlled child link.
    root = hermes_home() / 'plugin-data' / 'pantheon'
    declared = plugin_data_dir('pantheon')
    if declared.resolve() != root:
        raise APIError('file_path_not_allowed')
    with _directory(root) as fd:
        try:
            os.mkdir('downloads', 0o700, dir_fd=fd)
        except FileExistsError:
            pass
        child = os.open('downloads', DIRECTORY_FLAGS, dir_fd=fd)
        os.close(child)
    return root / 'downloads'


@contextmanager
def _directory(path):
    """Open each directory without following links, including racing replacements."""
    path = Path(path)
    fd = os.open(path.anchor, DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _relative(value, root, *, upload=False):
    path = Path(value).expanduser()
    if not path.is_absolute() or '..' in path.parts:
        raise APIError('file_path_not_allowed')
    resolved = path.resolve(strict=False)
    if resolved == root or not resolved.is_relative_to(root):
        raise APIError('file_path_not_allowed')
    relative = resolved.relative_to(root)
    if any(part.startswith('.') for part in relative.parts) or (upload and (
            _private(path) or _private(resolved) or resolved.is_relative_to(hermes_home()))):
        raise APIError('file_path_not_allowed')
    # Reject symlinks even when they point back inside the allowed root.
    if path.is_symlink():
        raise APIError('file_path_not_allowed')
    return relative


@contextmanager
def _parent(value, root, *, upload=False):
    root = Path(root)
    relative = _relative(value, root, upload=upload)
    with _directory(root) as root_fd:
        fd = os.dup(root_fd)
        try:
            for part in relative.parts[:-1]:
                child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd, relative.name, root / relative
        finally:
            os.close(fd)


def _read(fd, limit, *, durable=False):
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise APIError('file_limit')
        data = stream.read(limit + 1)
        if durable:
            os.fsync(stream.fileno())
    if len(data) > limit:
        raise APIError('file_limit')
    return data


def read_file(value, limit=MAX_FILE, *, root, durable=False, upload=True):
    with _parent(value, root, upload=upload) as (parent, name, _):
        # O_NONBLOCK prevents a forged FIFO from hanging before the regular-file check.
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        data = _read(fd, limit, durable=durable)
        if durable:
            os.fsync(parent)
    return data


def save_file(path, payload, metadata):
    data = base64.b64decode(payload, validate=True)
    if len(data) > MAX_FILE or len(data) != metadata['size'] or hashlib.sha256(data).hexdigest() != metadata['sha256']:
        raise APIError('transfer_integrity_failed')
    with _parent(path, download_root()) as (parent, name, target):
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        except FileExistsError:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            if _read(fd, MAX_FILE, durable=True) != data:
                raise APIError('destination_exists') from None
        else:
            with os.fdopen(fd, 'wb') as out:
                out.write(data); out.flush(); os.fsync(out.fileno())
        os.fsync(parent)
    return {'path': str(target), 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'durable': True}


def validate_download(path):
    # Check before a network fetch, not only after receiving another member's bytes.
    with _parent(path, download_root()):
        pass


def bundle(directory, *, root):
    relative = _relative(directory, root, upload=True) if Path(directory).resolve() != root else Path('.')
    selected = root / relative
    if _private(selected) or selected.is_relative_to(hermes_home()):
        raise APIError('file_path_not_allowed')
    files, size = [], 0
    # fwalk and all file opens are anchored to directory descriptors. A renamed or
    # symlink-replaced ancestor cannot redirect reads outside the workspace.
    with _directory(selected) as fd:
        for parent, directories, names, parent_fd in os.fwalk('.', dir_fd=fd, follow_symlinks=False):
            for child in directories + names:
                if (child.startswith(('.', '_')) or child == 'node_modules' or _private(Path(child))
                        or (selected / parent / child).is_relative_to(hermes_home())):
                    raise APIError('unsupported_bundle_file')
                if stat.S_ISLNK(os.stat(child, dir_fd=parent_fd, follow_symlinks=False).st_mode):
                    raise APIError('symlink_in_bundle')
            for name in sorted(names):
                data = _read(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd), 5 << 20)
                size += len(data)
                if size > 20 << 20 or len(files) >= 500:
                    raise APIError('site_limit')
                path = (Path(parent) / name).as_posix()
                files.append({'path': path, 'data': base64.b64encode(data).decode()})
    if not any(f['path'] == 'index.html' for f in files):
        raise APIError('index_html_required')
    return sorted(files, key=lambda f: f['path'])
