"""MCP stdio server exposing the AST index to coding agents.

Results are returned as dense text rather than JSON: an agent pays for every
token, and `repo/path.py:121 function register_tool(...)` carries the same
information as ~8 lines of pretty-printed JSON.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from . import query as Q
from .db import connect

INSTRUCTIONS = """AST-backed index of the local git checkouts (tree-sitter symbols,
references and imports across every checkout it has indexed).

Prefer these tools over grep/glob when you need to know:
  - where something is defined            -> code_def
  - who calls or renders something        -> code_callers / code_refs
  - what is in a file, before reading it  -> code_outline
  - which module a name lives in          -> code_search
  - who depends on a module               -> code_importers

They return exact `repo/path:line` locations, so cite those directly. The index
is content-addressed by git blob, so results are de-duplicated across the many
sibling task worktrees on this machine and point at the canonical clone.

Caveats: the index reflects the last `code_reindex`, not unsaved edits; and a
small number of files have partial parses (see code_doctor)."""

_CON = {"db": None, "con": None}


def _con(db=None):
    if _CON["con"] is None:
        _CON["db"] = db
        _CON["con"] = connect(db)
    return _CON["con"]


def _prov(r: dict) -> str:
    """Flag branch-only hits so an agent does not cite unmerged code as shipped."""
    if r.get("is_canonical") is None or r.get("is_canonical"):
        return ""
    return f"  [BRANCH ONLY: {r.get('branch') or 'detached'}, not in canonical clone]"


def _sig(r: dict, width: int = 170) -> str:
    s = r.get("signature") or ""
    return " ".join(s.split())[:width]


def _fmt_defs(rows: list[dict], limit: int) -> str:
    if not rows:
        return "no definitions found"
    out = []
    for r in rows[:limit]:
        head = (f"{r['repo']}/{r['rel_path']}:{r['start_line']}-{r['end_line']}"
                f"{_prov(r)}")
        bits = [r["kind"]]
        if r.get("is_async"):
            bits.append("async")
        if r.get("exported"):
            bits.append("export")
        if r.get("parent"):
            bits.append(f"in {r['parent']}")
        if r.get("decorators"):
            bits.append("@" + ",".join(r["decorators"])[:50])
        line = f"{head}\n  [{' '.join(bits)}] {_sig(r) or r['name']}"
        if r.get("doc"):
            line += f"\n  -- {r['doc'][:150]}"
        out.append(line)
    extra = len(rows) - limit
    if extra > 0:
        out.append(f"... and {extra} more")
    return "\n".join(out)


def _fmt_refs(rows: list[dict], limit: int) -> str:
    if not rows:
        return "no references found"
    by_file: dict[str, list[dict]] = {}
    for r in rows[:limit]:
        by_file.setdefault(f"{r['repo']}/{r['rel_path']}{_prov(r)}", []).append(r)
    out = []
    for f, rs in by_file.items():
        hits = ", ".join(
            f"L{r['line']}" + (f" ({r['context']})" if r.get("context") else "")
            for r in rs
        )
        out.append(f"{f}: {hits}")
    n = len({f"{r['repo']}/{r['rel_path']}" for r in rows})
    out.insert(0, f"{len(rows)} reference(s) across {n} file(s)")
    if len(rows) > limit:
        out.append(f"... {len(rows) - limit} more not shown")
    return "\n".join(out)


def build(db=None) -> MCPServer:
    server = MCPServer(name="code-index", instructions=INSTRUCTIONS)

    @server.tool(
        description="Find where a symbol is defined. Returns exact repo/path:line, "
        "signature and docstring. Use instead of grepping for 'def name' / 'class name'."
    )
    def code_def(name: str, kind: str = "", repo: str = "", limit: int = 15) -> str:
        rows = Q.find_def(_con(db), name, kind or None, repo or None, limit=max(limit, 1) * 3)
        return _fmt_defs(rows, limit)

    @server.tool(
        description="Find call sites of a function/method, attributed to the enclosing "
        "symbol. Use to check whether something is actually wired up or is dead code."
    )
    def code_callers(name: str, repo: str = "", limit: int = 40) -> str:
        return _fmt_refs(Q.callers(_con(db), name, repo or None, limit=400), limit)

    @server.tool(
        description="Find all references to a symbol. kind='call' for calls, "
        "kind='jsx' for React element usage, empty for both."
    )
    def code_refs(name: str, kind: str = "", repo: str = "", limit: int = 40) -> str:
        return _fmt_refs(
            Q.find_refs(_con(db), name, kind or None, repo or None, limit=400), limit
        )

    @server.tool(
        description="Symbol outline of a file (path or fragment) with line numbers. "
        "Read this before opening a large file to find the part you need."
    )
    def code_outline(path: str, limit: int = 200) -> str:
        rows = Q.outline(_con(db), path, limit)
        if not rows:
            return f"no indexed file matching {path!r}"
        out, cur = [], None
        for r in rows:
            key = f"{r['repo']}/{r['rel_path']}"
            if key != cur:
                cur = key
                out.append(key)
            depth = 1 + (r["parent"].count(".") + 1 if r.get("parent") else 0)
            mark = "*" if r.get("exported") else ""
            out.append(
                f"{'  ' * depth}L{r['start_line']}-{r['end_line']} "
                f"{r['kind']} {mark}{r['name']}"
            )
        return "\n".join(out)

    @server.tool(
        description="Full-text search over symbol names, signatures and docstrings. "
        "Use when you know what a thing does but not what it is called."
    )
    def code_search(text: str, repo: str = "", limit: int = 15) -> str:
        return _fmt_defs(
            Q.search(_con(db), text, repo or None, limit=max(limit, 1) * 2), limit
        )

    @server.tool(
        description="Find files that import a given module path or imported symbol name. "
        "Use to gauge the blast radius of changing a module."
    )
    def code_importers(target: str, limit: int = 40) -> str:
        rows = Q.importers(_con(db), target, limit=400)
        if not rows:
            return "no importers found"
        out = [f"{len(rows)} import edge(s)"]
        for r in rows[:limit]:
            sym = r.get("symbol") or "*"
            out.append(f"{r['repo']}/{r['rel_path']}:{r['line']}  imports {sym} from {r.get('module')}")
        if len(rows) > limit:
            out.append(f"... {len(rows) - limit} more")
        return "\n".join(out)

    @server.tool(description="List what a given file imports.")
    def code_imports(path: str, limit: int = 100) -> str:
        rows = Q.imports_of(_con(db), path)[:limit]
        if not rows:
            return f"no indexed file matching {path!r}"
        return "\n".join(
            f"L{r['line']}: {r['module']} -> {r['symbol'] or '*'}"
            + (f" as {r['alias']}" if r["alias"] else "")
            for r in rows
        )

    @server.tool(description="Index size, languages, symbol kinds and indexed repos.")
    def code_stats() -> str:
        s = Q.stats(_con(db))
        lines = [
            f"repos={s['repos']} checkouts={s['checkouts']} "
            f"unique_blobs={s['unique_blobs']} (dedup {s['dedup_ratio']}x)",
            f"symbols={s['symbols']} refs={s['refs']} imports={s['imports']} loc={s['loc']}",
            "languages: " + ", ".join(f"{r['lang']}={r['blobs']}" for r in s["by_lang"]),
            "kinds: " + ", ".join(f"{r['kind']}={r['n']}" for r in s["by_kind"]),
            "repos indexed: " + ", ".join(
                f"{r['repo']}({r['checkouts']})" for r in s["by_repo"]
            ),
        ]
        return "\n".join(lines)

    @server.tool(
        description="Parse health. Lists files whose symbol recall may be incomplete, "
        "so you know where to fall back to reading the file directly."
    )
    def code_doctor(limit: int = 25) -> str:
        d = Q.doctor(_con(db), limit)
        out = [
            f"blobs={d['blobs']} clean={d['clean']} ({d['clean_pct']}%) "
            f"degraded={d['degraded']}"
        ]
        for r in d["files_with_errors"]:
            out.append(f"  {r['error_count']} err  {r.get('repo')}/{r.get('rel_path')}")
        return "\n".join(out)

    @server.tool(
        description="Re-index the repos to pick up code changes. Fast: only content "
        "not already indexed is parsed. Run after edits if lookups look stale."
    )
    def code_reindex(root: str = "", reparse: bool = False) -> str:
        from pathlib import Path

        from .cli import DEFAULT_ROOTS
        from .indexer import index_all

        roots = [Path(root)] if root else DEFAULT_ROOTS
        res = index_all(db_path=_CON["db"], roots=roots, reparse=reparse, verbose=False)
        # the shared connection may hold a stale snapshot after a bulk write
        if _CON["con"] is not None:
            _CON["con"].commit()
        return (
            f"indexed {len(res)} checkouts, "
            f"{sum(r['files'] for r in res)} files, "
            f"{sum(r['new_blobs'] for r in res)} newly parsed, "
            f"{sum(r['symbols'] for r in res)} symbols, "
            f"{round(sum(r['seconds'] for r in res), 1)}s"
        )

    return server


def serve(db=None) -> None:
    build(db).run(transport="stdio")
