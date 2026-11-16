"""Finding records shared by every check and every output format."""
from __future__ import annotations

from dataclasses import dataclass

ERROR = "error"
WARNING = "warning"
INFO = "info"
# Most severe first; the index doubles as the sort key.
SEVERITIES = (ERROR, WARNING, INFO)
RANK = {severity: index for index, severity in enumerate(SEVERITIES)}


@dataclass(frozen=True)
class Finding:
    """One problem (or notice) about one file of the repository.

    ``file`` is the repository-relative display path of the file the finding is
    about (a Compose file, a Dockerfile, a devcontainer.json); it is ``None``
    only for run-level notices. ``line`` is 1-based. ``pointer`` names a
    location inside the file that has no line (a JSON Pointer or a Kubernetes
    field path). ``subject`` names what the finding is about: a variable, a
    service, a task, a recipe. Messages never quote values from ``.env`` or
    ``env_file`` files.
    """

    check: str
    rule: str
    severity: str
    message: str
    file: str | None = None
    line: int | None = None
    pointer: str | None = None
    subject: str | None = None
    hint: str | None = None

    def __post_init__(self):
        if self.severity not in RANK:
            raise ValueError(f"unknown severity {self.severity!r}")

    @property
    def rule_id(self) -> str:
        """Stable, globally unique rule identifier, e.g. ``compose-env/inherited-unset``."""
        return f"{self.check}/{self.rule}"

    def sort_key(self):
        return (self.file is None, self.file or "", RANK[self.severity], self.check, self.rule,
                self.line or 0, self.pointer or "", self.subject or "", self.message)

    def to_dict(self) -> dict:
        return {
            "check": self.check, "rule": self.rule, "ruleId": self.rule_id,
            "severity": self.severity, "message": self.message, "file": self.file,
            "line": self.line, "pointer": self.pointer, "subject": self.subject, "hint": self.hint,
        }


def counts(findings) -> dict[str, int]:
    result = dict.fromkeys(SEVERITIES, 0)
    for finding in findings:
        result[finding.severity] += 1
    return result


def at_or_above(findings, threshold: str | None) -> bool:
    """True if any finding is at least as severe as ``threshold`` (None: never)."""
    if threshold is None:
        return False
    limit = RANK[threshold]
    return any(RANK[finding.severity] <= limit for finding in findings)
