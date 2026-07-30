"""Phase 1.2 port sanity — mỗi target py_compiles + 0 ONFA reference.

py_compile chỉ check syntax, KHÔNG resolve imports → target-by-target port
không bị chặn bởi cross-module deps (openai_client → llm_client, pgvector → db.models).
Runtime import verify để lại cho Phase 3+ (khi app/, db/ tenant-first sẵn sàng).
"""

from __future__ import annotations

import py_compile
import re
from pathlib import Path

REPO = Path(__file__).parent.parent
ONFA_PATTERN = re.compile(
    r"onfa|wallet|pending_action|ConfirmEvent|2fa|balance|commission|transaction|deposit|withdraw",
    re.IGNORECASE,
)


def _compile_and_grep(paths: list[Path]) -> None:
    assert paths, "target has no python files — package not yet ported or empty"
    for p in paths:
        assert p.exists(), f"target file missing: {p.relative_to(REPO)}"
        py_compile.compile(str(p), doraise=True)
        text = p.read_text(encoding="utf-8")
        m = ONFA_PATTERN.search(text)
        if m is not None:
            ctx = text[max(0, m.start() - 40) : m.end() + 40]
            raise AssertionError(
                f"ONFA reference in {p.relative_to(REPO)}: {m.group(0)!r} (context: {ctx!r})"
            )


def test_agent_llm_client_imports_clean() -> None:
    _compile_and_grep([REPO / "agent" / "llm_client.py"])


def test_agent_embedder_imports_clean() -> None:
    _compile_and_grep([REPO / "agent" / "embedder.py"])


def test_agent_providers_imports_clean() -> None:
    _compile_and_grep(sorted((REPO / "agent" / "providers").glob("*.py")))


def test_retrieval_imports_clean() -> None:
    _compile_and_grep(sorted((REPO / "retrieval").glob("*.py")))


def test_parsing_imports_clean() -> None:
    _compile_and_grep(sorted((REPO / "parsing").glob("*.py")))


# Phase 1.2 port sanity for `storage/` retired: spec 15 P1 deleted the package
# (0 reader — DI drafter took over). See tests/test_runtime_wiring.py for the
# invariant that keeps it gone.
