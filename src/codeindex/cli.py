"""codeindex CLI — AST-backed code lookup across your git checkouts."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import query as Q
from .db import DEFAULT_DB, connect

# Where to look for checkouts. This has to work on a machine that is not the
# author's, so resolution is explicit and in order:
#   1. $CODEINDEX_ROOT            (colon-separated, wins outright)
#   2. ~/.config/code-index/roots   (one path per line, # comments ok)
#   3. common code dirs that actually exist and actually hold a git checkout
# A code index that quietly misses a repo is worse than one that admits it, so
# resolve_roots() reports what it picked and index_all prints it.
_CONFIG_ROOTS = Path.home() / ".config" / "code-index" / "roots"

# Checked in order; only those that exist and contain >=1 git checkout survive.
_CANDIDATE_ROOTS = [
    Path.home() / "src",
    Path.home() / "code",
    Path.home() / "repos",
    Path.home() / "Projects",
    Path.home() / "dev",
    Path.home() / "git",
    Path.home() / "workspace",
]


def _holds_checkout(d: Path) -> bool:
    """True if d is a git checkout or has one directly beneath it."""
    if not d.is_dir():
        return False
    if (d / ".git").exists():
        return True
    try:
        return any((c / ".git").exists() for c in d.iterdir() if c.is_dir())
    except PermissionError:
        return False


def resolve_roots() -> tuple[list[Path], str]:
    """Return (roots, how_we_got_them). Never raises; may return []."""
    env = os.environ.get("CODEINDEX_ROOT")
    if env:
        return [Path(p) for p in env.split(os.pathsep) if p], "$CODEINDEX_ROOT"

    if _CONFIG_ROOTS.is_file():
        lines = [
            ln.split("#", 1)[0].strip()
            for ln in _CONFIG_ROOTS.read_text().splitlines()
        ]
        picked = [Path(ln).expanduser() for ln in lines if ln]
        if picked:
            return picked, str(_CONFIG_ROOTS)

    found = [d for d in _CANDIDATE_ROOTS if _holds_checkout(d)]
    if found:
        return found, "auto-detected"

    cwd = Path.cwd()
    if _holds_checkout(cwd):
        return [cwd], "cwd"
    return [], "nothing found"


DEFAULT_ROOTS, ROOTS_SOURCE = resolve_roots()


def _prov(r: dict) -> str:
    """Mark a hit that exists only in a task worktree, not the canonical clone.

    Without this, branch-only code reads as merged. A helper showed
    up twice in one file at different lines -- it is absent from the canonical
    clone and lives on two unmerged feature branches.
    """
    if r.get("is_canonical") is None or r.get("is_canonical"):
        return ""
    br = r.get("branch") or "detached"
    return f"   <- branch only: {br} (not in canonical clone)"


def _loc(r: dict) -> str:
    return f"{r.get('repo','?')}/{r.get('rel_path','?')}:{r.get('start_line') or r.get('line')}"


def _emit(rows, args, formatter):
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        print("(no results)")
        return
    formatter(rows)


def _fmt_defs(rows):
    for r in rows:
        flags = []
        if r.get("is_async"):
            flags.append("async")
        if r.get("exported"):
            flags.append("export")
        if r.get("decorators"):
            flags.append("@" + ",".join(r["decorators"])[:40])
        tag = f"  [{' '.join(flags)}]" if flags else ""
        parent = f"  (in {r['parent']})" if r.get("parent") else ""
        print(f"{r['kind']:<10} {r['name']}{parent}{tag}")
        print(f"           {_loc(r)}{_prov(r)}")
        if r.get("signature"):
            sig = " ".join(r["signature"].split())
            print(f"           {sig[:200]}")
        if r.get("doc"):
            print(f"           — {r['doc'][:140]}")
        print()


def _fmt_refs(rows):
    cur = None
    for r in rows:
        key = f"{r['repo']}/{r['rel_path']}"
        if key != cur:
            cur = key
            print(f"\n{key}{_prov(r)}")
        ctx = f"  in {r['context']}" if r.get("context") else ""
        print(f"  L{r['line']:<6} {r['kind']:<5}{ctx}")
    print()


def _fmt_outline(rows):
    cur = None
    for r in rows:
        key = f"{r['repo']}/{r['rel_path']}"
        if key != cur:
            cur = key
            print(f"\n{key}{_prov(r)}")
        depth = 1 + (r["parent"].count(".") + 1 if r.get("parent") else 0)
        pad = "  " * depth
        flags = "*" if r.get("exported") else " "
        print(f"{pad}{flags}L{r['start_line']:<5} {r['kind']:<10} {r['name']}")
    print()


def _fmt_imports(rows):
    for r in rows:
        sym = r.get("symbol") or "*"
        alias = f" as {r['alias']}" if r.get("alias") else ""
        loc = f"{r.get('repo','?')}/{r.get('rel_path','?')}:{r['line']}"
        print(f"  {r.get('module') or '?':<55} {sym}{alias}")
        print(f"    {loc}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="codeindex",
        description="AST-backed code index over your git checkouts.",
    )
    ap.add_argument("--db", default=None, help=f"index path (default {DEFAULT_DB})")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, **kw):
        sp = sub.add_parser(name, **kw)
        # accept --json after the subcommand as well as before it
        sp.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS, help="machine-readable output")
        return sp

    p = add("index", help="build/refresh the index")
    p.add_argument("roots", nargs="*", default=None,
                   help="dirs holding checkouts (default: $CODEINDEX_ROOT)")
    p.add_argument("--reparse", action="store_true", help="re-parse even known blobs")
    p.add_argument("--no-untracked", action="store_true", help="tracked files only")
    p.add_argument("--canonical-only", action="store_true",
                   help="one checkout per repo (skip sibling task worktrees)")
    p.add_argument("--here", action="store_true",
                   help="also index the current working directory's checkout")
    p.add_argument("-q", "--quiet", action="store_true")

    p = add("def", help="where is a symbol defined")
    p.add_argument("name")
    p.add_argument("--kind")
    p.add_argument("--repo")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--exact", action="store_true")

    p = add("refs", help="who references a symbol")
    p.add_argument("name")
    p.add_argument("--kind", choices=["call", "jsx"])
    p.add_argument("--repo")
    p.add_argument("--limit", type=int, default=100)

    p = add("callers", help="call sites of a function")
    p.add_argument("name")
    p.add_argument("--repo")
    p.add_argument("--limit", type=int, default=100)

    p = add("outline", help="symbol outline of a file")
    p.add_argument("path")
    p.add_argument("--limit", type=int, default=400)

    p = add("search", help="full-text search names/signatures/docs")
    p.add_argument("text", nargs="+")
    p.add_argument("--repo")
    p.add_argument("--limit", type=int, default=40)

    p = add("importers", help="who imports a module or symbol")
    p.add_argument("target")
    p.add_argument("--limit", type=int, default=80)

    p = add("imports", help="what a file imports")
    p.add_argument("path")

    add("stats", help="index size and composition")
    p = add("doctor", help="parse health / where recall is degraded")
    p.add_argument("--limit", type=int, default=50)
    add("mcp", help="run as an MCP stdio server")

    args = ap.parse_args(argv)

    if args.cmd == "mcp":
        from .mcp_server import serve
        serve(args.db)
        return 0

    if args.cmd == "index":
        from .indexer import index_all
        roots = [Path(r) for r in args.roots] if args.roots else DEFAULT_ROOTS
        if not roots and not args.here:
            print(
                "codeindex: no repo roots found.\n"
                "  Point it at your checkouts, either way:\n"
                "    codeindex index ~/path/to/your/repos\n"
                "    export CODEINDEX_ROOT=~/path/one:~/path/two\n"
                f"  or list them one per line in {_CONFIG_ROOTS}\n"
                "  Coverage is only ever the repos you have cloned locally.",
                file=sys.stderr,
            )
            return 2
        if not args.quiet and not args.roots:
            print(f"roots ({ROOTS_SOURCE}): " + ", ".join(str(r) for r in roots))
        res = index_all(
            db_path=args.db, roots=roots,
            include_untracked=not args.no_untracked,
            reparse=args.reparse, verbose=not args.quiet,
            canonical_only=args.canonical_only,
            extra=[Path.cwd()] if args.here else None,
        )
        tot = {
            "checkouts": len(res),
            "files": sum(r["files"] for r in res),
            "new_blobs": sum(r["new_blobs"] for r in res),
            "reused": sum(r["reused"] for r in res),
            "symbols": sum(r["symbols"] for r in res),
            "seconds": round(sum(r["seconds"] for r in res), 2),
        }
        if args.json:
            print(json.dumps({"totals": tot, "checkouts": res}, indent=2))
        else:
            print(
                f"\nindexed {tot['checkouts']} checkouts · {tot['files']} files · "
                f"{tot['new_blobs']} parsed · {tot['reused']} reused from cache · "
                f"{tot['symbols']} symbols · {tot['seconds']}s"
            )
        return 0

    con = connect(args.db)

    if args.cmd == "def":
        rows = Q.find_def(con, args.name, args.kind, args.repo, args.limit,
                          fuzzy=not args.exact)
        _emit(rows, args, _fmt_defs)
    elif args.cmd == "refs":
        _emit(Q.find_refs(con, args.name, args.kind, args.repo, args.limit), args, _fmt_refs)
    elif args.cmd == "callers":
        _emit(Q.callers(con, args.name, args.repo, args.limit), args, _fmt_refs)
    elif args.cmd == "outline":
        _emit(Q.outline(con, args.path, args.limit), args, _fmt_outline)
    elif args.cmd == "search":
        _emit(Q.search(con, " ".join(args.text), args.repo, args.limit), args, _fmt_defs)
    elif args.cmd == "importers":
        _emit(Q.importers(con, args.target, args.limit), args, _fmt_imports)
    elif args.cmd == "imports":
        _emit(Q.imports_of(con, args.path), args, _fmt_imports)
    elif args.cmd == "stats":
        s = Q.stats(con)
        if args.json:
            print(json.dumps(s, indent=2))
        else:
            print(f"repos          {s['repos']}")
            print(f"checkouts      {s['checkouts']}")
            print(f"file rows      {s['file_rows']}")
            print(f"unique blobs   {s['unique_blobs']}  (dedup {s['dedup_ratio']}x)")
            print(f"symbols        {s['symbols']}")
            print(f"refs           {s['refs']}")
            print(f"imports        {s['imports']}")
            print(f"lines of code  {s['loc']}")
            print("\nby language:")
            for r in s["by_lang"]:
                print(f"  {r['lang']:<12}{r['blobs']:>6} blobs {r['loc'] or 0:>9} loc")
            print("\nby kind:")
            for r in s["by_kind"]:
                print(f"  {r['kind']:<12}{r['n']:>6}")
            print("\nby repo:")
            for r in s["by_repo"]:
                print(f"  {r['repo']:<34}{r['checkouts']:>4} checkouts")
    elif args.cmd == "doctor":
        d = Q.doctor(con, args.limit)
        if args.json:
            print(json.dumps(d, indent=2))
        else:
            print(f"blobs {d['blobs']} · clean {d['clean']} ({d['clean_pct']}%) · "
                  f"degraded {d['degraded']}")
            if d["files_with_errors"]:
                print("\nfiles whose symbol recall may be incomplete:")
                for r in d["files_with_errors"]:
                    print(f"  {r['error_count']:>3} err  {r['lang']:<11} "
                          f"{r.get('repo','?')}/{r.get('rel_path','?')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
