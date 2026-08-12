#!/usr/bin/env python3
"""Fail when a public package contains obvious private identifiers or secrets."""

from __future__ import annotations

import re
from pathlib import Path
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIR.parents[2]
TEXT_SUFFIXES = {
    "",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
FORBIDDEN_PATTERNS = {
    "Windows user profile path": re.compile(r"C:\\Users\\(?!<)[^\\/\s]+", re.IGNORECASE),
    "macOS user profile path": re.compile(r"/Users/(?!<)[^/\s]+"),
    "Feishu app identifier": re.compile(r"\bcli_[a-z0-9]{12,}\b", re.IGNORECASE),
    "Feishu chat identifier": re.compile(r"\boc_[a-z0-9]{12,}\b", re.IGNORECASE),
    "Feishu message identifier": re.compile(r"\bom_[a-z0-9]{12,}\b", re.IGNORECASE),
    "private key": re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
    "bearer token": re.compile(r"Bearer\s+[A-Za-z0-9._~-]{24,}"),
}


def text_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in {".git", ".venv", "__pycache__"} for part in relative.parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES:
            yield path


def audit(root: Path = REPOSITORY_ROOT) -> list[str]:
    findings: list[str] = []
    for path in text_files(root):
        if path.resolve() == Path(__file__).resolve():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(f"non-UTF-8 text file: {path.relative_to(root)}")
            continue
        relative = path.relative_to(root)
        for label, pattern in FORBIDDEN_PATTERNS.items():
            if pattern.search(content):
                findings.append(f"{label}: {relative}")
    return sorted(set(findings))


def main() -> int:
    findings = audit()
    if findings:
        print("Public package audit failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print("Public package audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
