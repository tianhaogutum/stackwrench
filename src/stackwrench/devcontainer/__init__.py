"""Offline, source-located Dev Container and Compose wiring checks.

``stackwrench devcontainer`` (formerly the Node.js ``devcontainer-wirecheck``).
The public API mirrors the original ``lib/index.js`` exports: :func:`check`
returns the JSON report and :func:`to_sarif` (alias ``toSarif``) converts it to
SARIF 2.1.0.
"""

from stackwrench import __version__

from .checks import check
from .documents import InputError, pointer, read_document
from .mounts import compose_mount, dev_mount
from .sarif import to_sarif, toSarif

__all__ = [
    "__version__",
    "check",
    "to_sarif",
    "toSarif",
    "InputError",
    "pointer",
    "read_document",
    "dev_mount",
    "compose_mount",
]
