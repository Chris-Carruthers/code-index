"""Repo discovery and content-addressed indexing."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .db import connect, drop_blob_data
from .extract import EXT_LANG, extract

# Directories git already ignores are skipped for free because we enumerate via
# `git ls-files`. These are belt-and-braces for untracked scans.
SKIP_DIRS = {
    "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next",
    ".git", "coverage", ".pytest_cache", "site-packages", ".mypy_cache",
    "vendor", ".turbo", "out",
}
MAX_BYTES = 1_500_000
MAX_LINE_FOR_MINIFIED = 5000


def _git(path: Path | str, *args: str, check: bool = False) -> str:
    r = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True, text=True, check=check,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def blob_sha(data: bytes) -> str:
    """Git's blob hash, so a file unmodified across N checkouts hashes once."""
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def normalize_remote(remote: str) -> str:
    r = remote.strip()
    r = r.removesuffix(".git")
    r = r.replace("git@github.com:", "github.com/")
    r = r.replace("https://github.com/", "github.com/")
    r = r.replace("ssh://git@github.com/", "github.com/")
    return r.lower()


def discover_checkouts(roots: list[Path], depth: int = 1) -> list[Path]:
    """Find git checkouts under the given roots (non-recursive by default)."""
    found: list[Path] = []
    for root in roots:
        root = Path(root).expanduser()
        if not root.is_dir():
            continue
        if (root / ".git").exists():
            found.append(root)
        if depth >= 1:
            for child in sorted(root.iterdir()):
                if child.is_dir() and child.name not in SKIP_DIRS \
                        and (child / ".git").exists():
                    found.append(child)
    # de-dup, preserve order
    seen, out = set(), []
    for p in found:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def _wanted(rel: str) -> bool:
    suffix = Path(rel).suffix.lower()
    if suffix not in EXT_LANG:
        return False
    if set(Path(rel).parts) & SKIP_DIRS:
        return False
    name = Path(rel).name
    if ".min." in name or name.endswith(".d.ts"):
        return False
    return True


def _index_entries(path: Path, include_untracked: bool) -> dict[str, str | None]:
    """Map rel_path -> git blob sha, or None when we must hash the file ourselves.

    Taking the sha straight from git's index is the difference between reading
    91k files and reading only the handful whose content we have never parsed.
    A sha of None means "working tree differs from the index (or is untracked),
    so git's sha is not the content we want".
    """
    entries: dict[str, str | None] = {}

    ls = _git(path, "ls-files", "-s", "-z")
    for entry in ls.split("\0"):
        if not entry:
            continue
        meta, _, rel = entry.partition("\t")
        parts = meta.split()
        if len(parts) < 3 or not rel:
            continue
        if _wanted(rel):
            entries[rel] = parts[1]

    # Files modified in the working tree: git's indexed sha is stale for these.
    for rel in _git(path, "diff", "--name-only", "-z").split("\0"):
        if rel and rel in entries:
            entries[rel] = None

    if include_untracked:
        for rel in _git(path, "ls-files", "-o", "--exclude-standard", "-z").split("\0"):
            if rel and _wanted(rel):
                entries[rel] = None

    return entries


def _looks_minified(data: bytes) -> bool:
    if b"\n" not in data:
        return len(data) > 2000
    longest = max((len(l) for l in data.split(b"\n")), default=0)
    return longest > MAX_LINE_FOR_MINIFIED


def index_checkout(
    con,
    path: Path,
    include_untracked: bool = True,
    reparse: bool = False,
    verbose: bool = False,
) -> dict:
    t0 = time.time()
    path = Path(path).resolve()
    remote_raw = _git(path, "remote", "get-url", "origin")
    remote = normalize_remote(remote_raw) if remote_raw else f"local:{path}"
    repo_name = remote.rsplit("/", 1)[-1] if "/" in remote else path.name
    branch = _git(path, "branch", "--show-current")
    head = _git(path, "rev-parse", "HEAD")
    # Cheap change detector: HEAD plus the porcelain status. With 100+
    # checkouts on this machine, most are untouched between runs and
    # re-walking them dominates an incremental index.
    status = _git(path, "status", "--porcelain")
    fingerprint = hashlib.sha1(
        f"{head}|{len(status)}|{status}".encode("utf8", "replace")
    ).hexdigest()
    prev = con.execute(
        "SELECT id, fingerprint FROM checkout WHERE path=?", (str(path),)
    ).fetchone()
    if prev and prev["fingerprint"] == fingerprint and not reparse:
        nfiles = con.execute(
            "SELECT COUNT(*) c FROM file WHERE checkout_id=?", (prev["id"],)
        ).fetchone()["c"]
        return {
            "repo": repo_name, "path": str(path), "branch": branch,
            "files": nfiles, "new_blobs": 0, "reused": nfiles,
            "symbols": 0, "refs": 0, "imports": 0, "parse_errors": 0,
            "skipped": 0, "unchanged": True,
            "seconds": round(time.time() - t0, 2),
        }

    con.execute(
        "INSERT INTO repo(remote, name) VALUES(?,?) ON CONFLICT(remote) DO NOTHING",
        (remote, repo_name),
    )
    repo_id = con.execute("SELECT id FROM repo WHERE remote=?", (remote,)).fetchone()["id"]
    is_canonical = 1 if path.name == repo_name else 0
    con.execute(
        """INSERT INTO checkout(repo_id, path, branch, head, is_canonical,
                             indexed_at, fingerprint)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(path) DO UPDATE SET
             branch=excluded.branch, head=excluded.head,
             is_canonical=excluded.is_canonical, indexed_at=excluded.indexed_at,
             fingerprint=excluded.fingerprint""",
        (repo_id, str(path), branch, head, is_canonical,
         datetime.now(timezone.utc).isoformat(timespec="seconds"), fingerprint),
    )
    checkout_id = con.execute(
        "SELECT id FROM checkout WHERE path=?", (str(path),)
    ).fetchone()["id"]

    entries = _index_entries(path, include_untracked)
    known = {r["sha"] for r in con.execute("SELECT sha FROM blob")}

    file_rows: list[tuple] = []
    to_parse: dict[str, tuple[bytes, str]] = {}
    skipped = 0

    def _read(rel: str) -> bytes | None:
        fp = path / rel
        try:
            if not fp.is_file() or fp.is_symlink() or fp.stat().st_size > MAX_BYTES:
                return None
            return fp.read_bytes()
        except OSError:
            return None

    for rel, sha in entries.items():
        data: bytes | None = None
        if sha is None:  # dirty or untracked: hash the working tree content
            data = _read(rel)
            if data is None:
                skipped += 1
                continue
            sha = blob_sha(data)

        file_rows.append((checkout_id, rel, sha))

        need_parse = reparse or sha not in known
        if not need_parse or sha in to_parse:
            continue

        if data is None:
            data = _read(rel)
            if data is None:
                skipped += 1
                continue
        if b"\0" in data[:2048] or _looks_minified(data):
            # Record it so later checkouts treat the sha as known and skip the read.
            con.execute(
                """INSERT INTO blob(sha, lang, loc, bytes, parsed_at, parse_ok,
                                    error_count, skipped)
                   VALUES(?,?,?,?,?,1,0,1) ON CONFLICT(sha) DO NOTHING""",
                (sha, EXT_LANG[Path(rel).suffix.lower()], 0, len(data),
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            known.add(sha)
            skipped += 1
            continue
        to_parse[sha] = (data, EXT_LANG[Path(rel).suffix.lower()])

    if reparse and to_parse:
        drop_blob_data(con, list(to_parse.keys()))

    parsed = errors = sym_n = ref_n = imp_n = 0

    for sha, (data, lang) in to_parse.items():
        try:
            ex = extract(data, lang)
        except Exception as exc:  # a bad parse must not abort the whole run
            if verbose:
                print(f"  ! parse failed {sha[:8]} ({lang}): {exc}")
            errors += 1
            continue
        con.execute(
            """INSERT INTO blob(sha, lang, loc, bytes, parsed_at, parse_ok,
                                error_count, skipped)
               VALUES(?,?,?,?,?,?,?,0)
               ON CONFLICT(sha) DO UPDATE SET
                 lang=excluded.lang, loc=excluded.loc, bytes=excluded.bytes,
                 parsed_at=excluded.parsed_at, parse_ok=excluded.parse_ok,
                 error_count=excluded.error_count, skipped=0""",
            (sha, lang, ex.loc, len(data),
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             1 if ex.parse_ok else 0, ex.error_count),
        )
        if ex.symbols:
            con.executemany(
                """INSERT INTO symbol(blob_sha, name, kind, start_line, end_line,
                                      parent, signature, doc, decorators, is_async, exported)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                [(sha, s.name, s.kind, s.start_line, s.end_line, s.parent,
                  s.signature, s.doc, json.dumps(s.decorators) if s.decorators else None,
                  int(s.is_async), int(s.exported)) for s in ex.symbols],
            )
            sym_n += len(ex.symbols)
        if ex.refs:
            con.executemany(
                "INSERT INTO ref(blob_sha, name, kind, line, context) VALUES(?,?,?,?,?)",
                [(sha, r.name, r.kind, r.line, r.context) for r in ex.refs],
            )
            ref_n += len(ex.refs)
        if ex.imports:
            con.executemany(
                """INSERT INTO import_edge(blob_sha, module, symbol, alias, line)
                   VALUES(?,?,?,?,?)""",
                [(sha, i.module, i.symbol, i.alias, i.line) for i in ex.imports],
            )
            imp_n += len(ex.imports)
        parsed += 1

    con.execute("DELETE FROM file WHERE checkout_id=?", (checkout_id,))
    con.executemany(
        "INSERT OR REPLACE INTO file(checkout_id, rel_path, blob_sha) VALUES(?,?,?)",
        file_rows,
    )
    con.commit()

    return {
        "repo": repo_name, "path": str(path), "branch": branch,
        "files": len(file_rows), "new_blobs": parsed, "reused": len(file_rows) - parsed,
        "symbols": sym_n, "refs": ref_n, "imports": imp_n,
        "parse_errors": errors, "skipped": skipped,
        "seconds": round(time.time() - t0, 2),
    }


def index_all(db_path=None, roots=None, include_untracked=True,
              reparse=False, verbose=True, canonical_only=False,
              extra=None) -> list[dict]:
    from .db import rebuild_fts

    roots = [Path(r) for r in (roots or [Path.cwd()])]
    con = connect(db_path)
    checkouts = discover_checkouts(roots)
    if canonical_only:
        # keep one checkout per repo: the clone whose dir name == repo name
        keep, seen = [], set()
        for c in checkouts:
            remote_raw = _git(c, "remote", "get-url", "origin")
            remote = normalize_remote(remote_raw) if remote_raw else f"local:{c}"
            name = remote.rsplit("/", 1)[-1] if "/" in remote else c.name
            if c.name == name and remote not in seen:
                seen.add(remote)
                keep.append(c)
        checkouts = keep
    for e in (extra or []):
        ep = Path(e).resolve()
        if ep not in checkouts and (ep / ".git").exists():
            checkouts.append(ep)
    results = []
    for i, c in enumerate(checkouts, 1):
        res = index_checkout(con, c, include_untracked, reparse, verbose)
        results.append(res)
        if verbose and not res.get("unchanged"):
            print(
                f"[{i}/{len(checkouts)}] {res['repo']:<32} "
                f"files={res['files']:<5} new={res['new_blobs']:<5} "
                f"reused={res['reused']:<5} syms={res['symbols']:<6} "
                f"{res['seconds']}s  {Path(res['path']).name}"
            )
    rebuild_fts(con)
    con.commit()
    return results
