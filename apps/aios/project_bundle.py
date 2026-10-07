"""Structured multi-file project bundles for isolated managed runtimes.

Validates application sources as data before execution:
- Bounded file count, path length, and total/per-file byte budgets.
- Strict POSIX relative paths; no escaping, no absolute paths, no symlinks.
- Deterministic canonical SHA-256 digest across file order.
- Exact entrypoint existence check within the bundle.
"""
from __future__ import annotations

import hashlib
import posixpath
import re
from typing import Any

MAX_FILES = 64
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_PATH_LENGTH = 128
ALLOWED_RUNTIMES = ('node-web',)

# Strict relative path segment: alphanumeric, hyphen, underscore, dot.
# No control characters, spaces, or backslashes.
_SEGMENT_RE = re.compile(r'^[a-zA-Z0-9_.-]+$')


def _validate_path(path: Any) -> str:
    if not isinstance(path, str) or not path:
        raise ValueError('Invalid file path: must be non-empty string')
    if len(path) > MAX_PATH_LENGTH:
        raise ValueError(f'File path exceeds {MAX_PATH_LENGTH} characters')
    if '\x00' in path or '\\' in path:
        raise ValueError('Path contains illegal characters')
    if path.startswith('/') or path.startswith('//'):
        raise ValueError('Path must be relative, not absolute')

    normalized = posixpath.normpath(path)
    if normalized.startswith('../') or normalized == '..' or normalized.startswith('/'):
        raise ValueError(f'Path traversal outside project root is forbidden: {path!r}')
    if normalized != path:
        raise ValueError(f'Path must be normalized POSIX relative path: {path!r}')

    segments = normalized.split('/')
    for segment in segments:
        if not segment or segment in ('.', '..'):
            raise ValueError(f'Invalid path segment in {path!r}')
        if segment.startswith('.'):
            raise ValueError(f'Hidden paths are forbidden: {path!r}')
        if not _SEGMENT_RE.fullmatch(segment):
            raise ValueError(f'Invalid characters in path segment {segment!r}')

    return normalized


def validate(source: Any) -> dict[str, Any]:
    """Validate a project source dictionary and return a canonical bundle.

    Raises ValueError if the source violates structural or security rules.
    """
    if not isinstance(source, dict):
        raise ValueError('Project source must be a dictionary')

    allowed_keys = {'runtime', 'entry', 'files'}
    extra_keys = set(source.keys()) - allowed_keys
    if extra_keys:
        raise ValueError(f'Unexpected keys in project source: {sorted(str(k) for k in extra_keys)}')

    runtime = source.get('runtime')
    if runtime not in ALLOWED_RUNTIMES:
        raise ValueError(f'Unsupported runtime: {runtime!r}; expected one of {ALLOWED_RUNTIMES}')

    entry = _validate_path(source.get('entry'))

    files_val = source.get('files')
    if not isinstance(files_val, dict) or not files_val:
        raise ValueError('files must be a non-empty dictionary of path -> content')
    if len(files_val) > MAX_FILES:
        raise ValueError(f'File count exceeds maximum of {MAX_FILES}')

    canonical_files: dict[str, str] = {}
    lower_map: dict[str, str] = {}
    ancestor_map: dict[str, str] = {}
    total_bytes = 0

    for raw_path, content in files_val.items():
        clean_path = _validate_path(raw_path)
        lower = clean_path.lower()
        if lower in lower_map:
            raise ValueError(f'Case-collision in file paths: {clean_path!r} and {lower_map[lower]!r}')
        lower_map[lower] = clean_path

        # Reject path collisions where one file is an ancestor directory of another
        parts = clean_path.split('/')
        for i in range(1, len(parts)):
            prefix_lower = '/'.join(parts[:i]).lower()
            if prefix_lower in lower_map:
                raise ValueError(f'File {lower_map[prefix_lower]!r} collides with directory in {clean_path!r}')
            ancestor_map[prefix_lower] = clean_path
        if lower in ancestor_map:
            raise ValueError(f'File {clean_path!r} collides with ancestor of {ancestor_map[lower]!r}')

        if not isinstance(content, str):
            raise ValueError(f'File content for {clean_path!r} must be a unicode string')
        try:
            encoded = content.encode('utf-8')
        except UnicodeEncodeError as exc:
            raise ValueError(f'File content for {clean_path!r} is not valid UTF-8: {exc}') from exc

        if len(encoded) > MAX_FILE_BYTES:
            raise ValueError(f'File {clean_path!r} exceeds {MAX_FILE_BYTES} bytes')
        total_bytes += len(encoded)
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValueError(f'Total project size exceeds {MAX_TOTAL_BYTES} bytes')

        canonical_files[clean_path] = content

    if entry not in canonical_files:
        raise ValueError(f'Declared entrypoint {entry!r} is not among bundle files')

    # Compute deterministic canonical digest: sort by normalized path.
    # Length-prefix each segment so content cannot forge a path boundary.
    hasher = hashlib.sha256()
    hasher.update(runtime.encode('utf-8') + b'\n')
    hasher.update(entry.encode('utf-8') + b'\n')
    for clean_path in sorted(canonical_files.keys()):
        path_bytes = clean_path.encode('utf-8')
        content_bytes = canonical_files[clean_path].encode('utf-8')
        hasher.update(f'{len(path_bytes)}:'.encode('ascii'))
        hasher.update(path_bytes)
        hasher.update(f':{len(content_bytes)}:'.encode('ascii'))
        hasher.update(content_bytes)

    return {
        'runtime': runtime,
        'entry': entry,
        'files': canonical_files,
        'sha256': hasher.hexdigest(),
        'total_bytes': total_bytes,
        'file_count': len(canonical_files),
    }
