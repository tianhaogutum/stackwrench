"""Read-only, exact-byte checks of saved analysis fingerprints."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat

MAX_REPORT_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTE_LIMIT = 2**63 - 1
EXIT_CODES = {'matched': 0, 'mismatch': 1, 'unavailable': 2, 'error': 3}
FIELDS = {'source': 'sourceSha256', 'taskfile': 'renderedTaskfileSha256'}


class _FileLimitExceeded(Exception):
    """The complete file cannot be hashed within the requested byte budget."""


@contextmanager
def _open_regular(path: Path):
    # Nonblocking open avoids hanging on a FIFO, including one replacing a
    # regular file just before open. Check the opened descriptor, not a prior
    # stat of a potentially different entry. Symlinks to regular files work.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    # No buffered read-ahead beyond a caller's explicit byte budget.
    with os.fdopen(fd, 'rb', buffering=0) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError('input must be a regular file')
        yield stream


def _sha256(path: Path, max_file_bytes: int | None = None) -> str:
    digest = hashlib.sha256()
    total = 0
    with _open_regular(path) as stream:
        while True:
            size = 1024 * 1024
            if max_file_bytes is not None:
                size = min(size, max_file_bytes - total + 1)
            chunk = stream.read(size)
            if not chunk:
                break
            total += len(chunk)
            if max_file_bytes is not None and total > max_file_bytes:
                raise _FileLimitExceeded(f'file exceeds --max-file-bytes {max_file_bytes}')
            digest.update(chunk)
    return digest.hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key!r}')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f'nonstandard JSON constant: {value}')


def _expected(path: Path, max_report_bytes: int | None = None) -> dict:
    limit = MAX_REPORT_BYTES if max_report_bytes is None else max_report_bytes
    raw = bytearray()
    with _open_regular(path) as stream:
        # A short raw read is not EOF: require EOF or the overflow byte before
        # trusting even a complete-looking JSON prefix. Keep every read bounded.
        while len(raw) <= limit:
            chunk = stream.read(min(1024 * 1024, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
    if len(raw) > limit:
        message = ('report exceeds 64 MiB' if max_report_bytes is None else
                   f'report exceeds --max-report-bytes {limit}')
        raise ValueError(message)
    report = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                        parse_constant=_reject_constant)
    if not isinstance(report, dict):
        raise ValueError('analysis report must be a JSON object')
    if type(report.get('formatVersion')) is not int or report['formatVersion'] != 1:
        raise ValueError('analysis formatVersion must be integer 1')
    if type(report.get('supported')) is not bool:
        raise ValueError('analysis supported must be a boolean')
    for field in FIELDS.values():
        if field not in report:
            raise ValueError(f'missing {field}')
        value = report[field]
        if value is not None and (not isinstance(value, str) or
                                  re.fullmatch(r'[0-9a-f]{64}', value) is None):
            raise ValueError(f'{field} must be null or 64 lowercase hexadecimal digits')
    if not report['supported'] and report['renderedTaskfileSha256'] is not None:
        raise ValueError('unsupported analysis cannot have a rendered Taskfile fingerprint')
    return {subject: report[field] for subject, field in FIELDS.items()}


def check_report(source: Path, taskfile: Path, report: Path, *,
                 max_file_bytes: int | None = None,
                 max_report_bytes: int | None = None) -> dict:
    """Compare supplied file bytes; never follow paths embedded in the report."""
    if max_report_bytes is not None and (type(max_report_bytes) is not int or
                                         not 1 <= max_report_bytes <= MAX_REPORT_BYTES):
        raise ValueError(f'max_report_bytes must be an integer from 1 to {MAX_REPORT_BYTES}')
    if max_file_bytes is not None and (type(max_file_bytes) is not int or
                                       not 1 <= max_file_bytes <= MAX_FILE_BYTE_LIMIT):
        raise ValueError(f'max_file_bytes must be an integer from 1 to {MAX_FILE_BYTE_LIMIT}')
    result = {'formatVersion': 1, 'status': 'unavailable', 'report': str(report),
              'checks': {}, 'diagnostics': []}
    if max_file_bytes is not None:
        result['maxFileBytes'] = max_file_bytes
    if max_report_bytes is not None:
        result['maxReportBytes'] = max_report_bytes
    diagnostics = result['diagnostics']
    expected = {}
    invalid = False
    io_error = False
    try:
        expected = _expected(report, max_report_bytes)
    except OSError as exc:
        io_error = True
        diagnostics.append({'subject': 'report', 'code': 'input-error', 'message': str(exc)})
    except (ValueError, RecursionError) as exc:
        invalid = True
        diagnostics.append({'subject': 'report', 'code': 'invalid-report', 'message': str(exc)})
    for subject, path in (('source', source), ('taskfile', taskfile)):
        wanted = expected.get(subject)
        check = {'path': str(path), 'expectedSha256': wanted,
                 'actualSha256': None, 'matches': None}
        result['checks'][subject] = check
        try:
            check['actualSha256'] = _sha256(path, max_file_bytes)
        except _FileLimitExceeded as exc:
            diagnostics.append({'subject': subject, 'code': 'file-limit', 'message': str(exc)})
        except OSError as exc:
            io_error = True
            diagnostics.append({'subject': subject, 'code': 'input-error', 'message': str(exc)})
        if expected and wanted is None:
            diagnostics.append({'subject': subject, 'code': 'fingerprint-unavailable',
                                'message': f'analysis {FIELDS[subject]} is null'})
        if wanted is not None and check['actualSha256'] is not None:
            check['matches'] = wanted == check['actualSha256']
    matches = [check['matches'] for check in result['checks'].values()]
    if io_error:
        result['status'] = 'error'
    elif invalid or None in matches:
        result['status'] = 'unavailable'
    elif False in matches:
        result['status'] = 'mismatch'
    else:
        result['status'] = 'matched'
    return result
