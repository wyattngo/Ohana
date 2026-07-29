"""Shared profile loader for the AI-coder scripts.

The profile is the only place a project fact is allowed to live. Nothing in this
skill knows a package name, a gate command, or a file path until it reads one.
"""

from __future__ import annotations

import re
from pathlib import Path

CONFIG_NAME = ".ai-coder.conf"
REPEATABLE = {"pair", "registry", "invariant"}

# Whitespace followed by '#' ends the value. A '#' glued to text (an anchor in
# a regex, say) is kept — only ' # like this' reads as a comment.
_INLINE_COMMENT = re.compile(r"\s+#.*$")


class ProfileError(Exception):
    pass


class Profile:
    def __init__(self, root: Path, data: dict[str, object]):
        self.root = root
        self._data = data

    def get(self, key: str, default: str = "") -> str:
        val = self._data.get(key, default)
        return val if isinstance(val, str) else default

    def list(self, key: str) -> list[str]:
        val = self._data.get(key, [])
        if isinstance(val, list):
            return val
        return [val] if val else []

    def words(self, key: str) -> list[str]:
        return self.get(key).split()

    @property
    def packages(self) -> list[str]:
        pkgs = self.words("packages")
        if not pkgs:
            raise ProfileError(f"{CONFIG_NAME}: 'packages' is empty — nothing to check")
        bad = [p for p in pkgs if not p.isidentifier()]
        if bad:
            # Refuse rather than skip: a polluted list silently narrows every
            # downstream check while still printing "ok".
            raise ProfileError(
                f"{CONFIG_NAME}: 'packages' contains non-package token(s): "
                f"{' '.join(bad)!r} — fix the line before trusting any check"
            )
        return pkgs

    def path(self, key: str) -> Path | None:
        raw = self.get(key)
        return (self.root / raw) if raw else None


def find_root(start: Path) -> Path:
    """Locate the project root. Never guesses: a repo with no profile is a repo
    this tooling has not been told anything about."""
    for d in [start, *start.parents]:
        if (d / CONFIG_NAME).is_file():
            return d
    raise ProfileError(
        f"no {CONFIG_NAME} found in {start} or any parent.\n"
        f"Run:  python3 <skill>/scripts/gen_codebase_map.py --init\n"
        f"Refusing to guess the layout — a wrong guess produces confident, wrong checks."
    )


def load(root: Path | None = None) -> Profile:
    root = root or find_root(Path.cwd().resolve())
    cfg = root / CONFIG_NAME
    if not cfg.is_file():
        raise ProfileError(f"{root}: no {CONFIG_NAME}")
    data: dict[str, object] = {}
    for lineno, raw in enumerate(cfg.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = _INLINE_COMMENT.sub("", line).rstrip()
        key, sep, val = line.partition("=")
        if not sep:
            raise ProfileError(f"{cfg}:{lineno}: expected 'key = value', got {line!r}")
        key, val = key.strip(), val.strip()
        if key in REPEATABLE:
            bucket = data.get(key)
            if not isinstance(bucket, list):
                bucket = []
                data[key] = bucket
            bucket.append(val)
        else:
            data[key] = val
    return Profile(root, data)
