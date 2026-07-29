#!/usr/bin/env python3
"""Reject local-only paths and sensitive values from the Git candidate set."""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Sequence


class Finding(NamedTuple):
    path: str
    rule: str
    line: int | None = None


FORBIDDEN_PARTS = {
    ".claude",
    ".playwright-cli",
    "browser-profile",
    "data",
    "logs",
    "private",
    "runtime",
    "vault",
}
FORBIDDEN_NAMES = {"config.yaml", "Cookies"}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".db-shm",
    ".db-wal",
    ".log",
    ".sqlite",
    ".sqlite3",
}

IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
CONTENT_RULES = (
    (
        "credential_value",
        re.compile(
            r"(?i)(?:\b(?:gh[pousr]_|sk-)[A-Za-z0-9_-]{16,}"
            r"|\b(?:api[_-]?key|authorization|secret|token)\b\s*[:=]\s*[\"'][^\"']{8,}[\"'])"
        ),
    ),
    ("cookie_header", re.compile(r"(?i)\bcookie\s*:\s*\S+")),
    ("personal_absolute_path", re.compile(r"/(?:home|Users)/[^/\s]+/")),
    (
        "site_numeric_identifier",
        re.compile(r"[?&][A-Za-z][A-Za-z0-9_-]*=[+-]?\d{12,}(?:[&#\s\"']|$)"),
    ),
)
LONG_IDENTIFIER_RE = re.compile(r"[+-]?\d{12,}")

DOCUMENTATION_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)


def candidate_paths(root: Path) -> list[Path]:
    """Return tracked and untracked, non-ignored Git candidates."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    return [root / value.decode("utf-8") for value in result.stdout.split(b"\0") if value]


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _forbidden_path(relative: str) -> bool:
    path = Path(relative)
    return (
        bool(FORBIDDEN_PARTS.intersection(path.parts))
        or path.name in FORBIDDEN_NAMES
        or any(path.name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES)
    )


def _public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    return (
        address.is_loopback
        or address.is_unspecified
        or any(address in network for network in DOCUMENTATION_NETWORKS)
    )


def scan_paths(paths: Sequence[Path], root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        relative = _relative(path, root)
        if _forbidden_path(relative):
            findings.append(Finding(relative, "forbidden_path"))
            continue
        if not path.is_file():
            continue
        payload = path.read_bytes()
        if b"\0" in payload:
            continue
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if relative.startswith("tests/") and "public-release: synthetic" in line:
                continue
            for match in IPV4_RE.finditer(line):
                if not _public_ip(match.group(0)):
                    findings.append(Finding(relative, "non_public_ip", line_number))
                    break
            for rule, pattern in CONTENT_RULES:
                if pattern.search(line):
                    findings.append(Finding(relative, rule, line_number))
            if relative.startswith("src/") and LONG_IDENTIFIER_RE.search(line):
                findings.append(Finding(relative, "embedded_long_identifier", line_number))
    return findings


def format_finding(finding: Finding) -> str:
    location = finding.path
    if finding.line is not None:
        location = f"{location}:{finding.line}"
    return f"{location}: {finding.rule}"


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    findings = scan_paths(candidate_paths(root), root=root)
    for finding in findings:
        print(format_finding(finding))
    if findings:
        print(f"Public release check failed with {len(findings)} finding(s).")
        return 1
    print("Public release check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
