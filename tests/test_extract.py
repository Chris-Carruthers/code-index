"""Extraction tests. Several are regressions for bugs found while building this."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codeindex.extract import extract
from codeindex.indexer import blob_sha

PY = '''
import logging
from typing import Dict, Any

MAX_RETRIES = 3


class Widget:
    """A widget."""

    def spin(self, n: int) -> None:
        """Spin it."""
        helper(n)


@decorated(x=1)
async def fetch(url: str) -> Dict[str, Any]:
    """Fetch a thing."""
    return await get(url)
'''


def _by_name(ex):
    return {s.name: s for s in ex.symbols}


def test_python_symbol_kinds_and_nesting():
    ex = extract(PY.encode(), "python")
    s = _by_name(ex)
    assert s["Widget"].kind == "class"
    assert s["spin"].kind == "method"
    assert s["spin"].parent == "Widget"
    assert s["fetch"].kind == "function"
    assert s["fetch"].parent is None


def test_python_docstrings_are_extracted():
    # Regression: this grammar exposes a bare `string` as the block's first
    # named child, not an `expression_statement`, so every docstring was lost.
    s = _by_name(extract(PY.encode(), "python"))
    assert s["Widget"].doc == "A widget."
    assert s["spin"].doc == "Spin it."
    assert s["fetch"].doc == "Fetch a thing."


def test_python_decorator_and_async():
    s = _by_name(extract(PY.encode(), "python"))
    assert s["fetch"].decorators == ["decorated(x=1)"]
    assert s["fetch"].is_async is True
    assert s["spin"].is_async is False


def test_python_module_constant():
    assert _by_name(extract(PY.encode(), "python"))["MAX_RETRIES"].kind == "const"


def test_python_import_module_is_not_an_imported_symbol():
    # Regression: py-tree-sitter returns a fresh node per accessor, so the
    # `c is mod_n` identity check never matched and "typing" was recorded as a
    # symbol imported from typing.
    ex = extract(b"from typing import Dict, Any\n", "python")
    pairs = {(i.module, i.symbol) for i in ex.imports}
    assert ("typing", "Dict") in pairs
    assert ("typing", "Any") in pairs
    assert ("typing", "typing") not in pairs


def test_python_call_refs_carry_enclosing_context():
    ex = extract(PY.encode(), "python")
    helper = [r for r in ex.refs if r.name == "helper"]
    assert helper and helper[0].context == "Widget.spin"


TSX = '''
import React, { useEffect } from "react";
import { Thing } from "@/lib/thing";

export interface Props { id: string }

export const Panel = ({ id }: Props) => {
  useEffect(() => {}, []);
  return <Thing id={id} />;
};

export function helper(a: number): number { return a; }
'''


def test_tsx_component_interface_and_export():
    ex = extract(TSX.encode(), "tsx")
    s = _by_name(ex)
    assert s["Panel"].kind == "component"
    assert s["Panel"].exported is True
    assert s["Props"].kind == "interface"
    assert s["helper"].kind == "component" or s["helper"].kind == "function"


def test_tsx_imports_and_jsx_refs():
    ex = extract(TSX.encode(), "tsx")
    mods = {i.module for i in ex.imports}
    assert "react" in mods and "@/lib/thing" in mods
    assert any(r.name == "Thing" and r.kind == "jsx" for r in ex.refs)


def test_blob_sha_matches_git(tmp_path):
    # The whole dedup story rests on this hash equalling git's.
    f = tmp_path / "x.txt"
    f.write_bytes(b"hello index\n")
    out = subprocess.run(
        ["git", "hash-object", str(f)], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert blob_sha(b"hello index\n") == out


def test_parse_errors_are_counted_not_raised():
    ex = extract(b"def broken(:\n  pass\n", "python")
    assert ex.error_count > 0
    assert ex.parse_ok is False
