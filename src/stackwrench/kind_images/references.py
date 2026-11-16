"""Conservative Docker-style names; never equate tags with digests or IDs."""

from dataclasses import dataclass
import re

from .inputs import InputError, string


_COMPONENT = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*\Z")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")


@dataclass(frozen=True)
class Reference:
    repository: str
    tag: str | None
    digest: str | None

    @property
    def canonical(self) -> str:
        if self.digest:
            return f"{self.repository}@{self.digest}"
        return f"{self.repository}:{self.tag or 'latest'}"

    @property
    def default_policy(self) -> str:
        if self.digest:
            return "IfNotPresent"
        return "Always" if self.tag in (None, "latest") else "IfNotPresent"


def parse_reference(value, where: str = "image") -> Reference:
    value = string(value, where)

    def fail(reason):
        raise InputError(f"{where}: invalid/unsupported image reference {value!r}: {reason}")

    if len(value) > 512 or any(char.isspace() for char in value):
        fail("too long or contains whitespace")
    if value.count("@") > 1:
        fail("multiple digest separators")
    name, sep, digest = value.partition("@")
    if sep:
        algo, colon, encoded = digest.partition(":")
        length = {"sha256": 64, "sha512": 128}.get(algo)
        if not colon or length is None or not re.fullmatch(r"[0-9a-f]{%d}" % length, encoded):
            fail("only lowercase sha256 (64 hex) and sha512 (128 hex) digests are supported")
    tag = None
    if name.rfind(":") > name.rfind("/"):
        name, tag = name.rsplit(":", 1)
        if not _TAG.fullmatch(tag):
            fail("invalid tag")
    parts = name.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0].lower() == "localhost"):
        host = parts.pop(0).lower()
        if host.count(":") > 1:
            fail("IPv6 registry literals are outside the supported subset")
        hostname, port_sep, port = host.partition(":")
        if port_sep and (not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535):
            fail("invalid registry port")
        if len(hostname) > 253 or any(not _HOST_LABEL.fullmatch(label) or len(label) > 63
                                      for label in hostname.split(".")):
            fail("invalid registry hostname")
        if host == "index.docker.io":
            host = "docker.io"
    else:
        host = "docker.io"
    if not parts or any(not _COMPONENT.fullmatch(part) for part in parts):
        fail("repository paths must be lowercase Docker-style components")
    if host == "docker.io" and len(parts) == 1:
        parts.insert(0, "library")
    repository = "/".join([host, *parts])
    if len(repository) > 255:
        fail("repository exceeds 255 characters")
    return Reference(repository, tag, digest if sep else None)
