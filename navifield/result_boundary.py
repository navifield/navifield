"""Shared local result envelope, path validation and redaction."""
import os
from pathlib import Path, PurePosixPath
import re
from .path_safety import assert_safe_components


def envelope(data=None, *, status='ok', code=None, message=None):
    return {'status': status, 'findings': [], 'artifacts': [], 'job_id': None,
            'decision': None, 'failure': None if code is None else {
                'code': code, 'stage': 'api', 'message': message or 'The request could not complete.',
                'measured': None, 'limit': None, 'log_path': None, 'next_actions': []}, 'data': data or {}}


class BoundaryError(Exception):
    def __init__(self, code='INPUT_INVALID', status=400, message=None):
        self.code, self.status, self.message = code, status, message


def safe_path(path):
    path = Path(os.path.abspath(path))
    try:
        assert_safe_components(path)
    except (OSError, ValueError):
        raise BoundaryError('ACCESS_DENIED', 403) from None
    return path


def relative(root, value):
    if not isinstance(value, str) or not value or len(value) > 120:
        raise BoundaryError()
    parts = PurePosixPath(value)
    if parts.is_absolute() or '\\' in value or ':' in value or any(x in ('.', '..') for x in value.split('/')):
        raise BoundaryError('ACCESS_DENIED', 403)
    target = safe_path(Path(root) / value)
    if not target.is_relative_to(root):
        raise BoundaryError('ACCESS_DENIED', 403)
    return target


def public_result(value, *, root=None, secrets_to_redact=()):
    """Narrow redaction preserves native error wording and project references."""
    service = str(Path(__file__).resolve().parents[len(__package__.split('.'))])
    def clean(item):
        if isinstance(item, dict): return {key: clean(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)): return [clean(child) for child in item]
        if isinstance(item, str):
            for secret in secrets_to_redact:
                if secret: item = item.replace(secret, '<redacted>')
            for path, replacement in ((service, '<service>'), (str(root), '.') if root else ('', '')):
                if path: item = item.replace(path, replacement).replace(path.replace('\\', '/'), replacement)
            item = re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+', r'\1<redacted>', item)
            item = re.sub(r'(?i)((?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s,;]+', r'\1<redacted>', item)
        return item
    return clean(value)
