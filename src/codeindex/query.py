"""Lookup API over the AST index.

Every result is de-duplicated across checkouts. A symbol is stored once per
content blob, but that blob may appear in 48 sibling worktrees; callers want
"app/agents/tools/registry.py:121", not the same line 48 times. We therefore
partition by (symbol, repo, rel_path) and prefer the canonical checkout.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict

# Shared join + de-dup preamble. `rn=1` keeps one row per logical location,
# preferring the canonical clone over task worktrees.
_LOC_JOIN = """
  JOIN file f     ON f.blob_sha = s.blob_sha
  JOIN checkout c ON c.id = f.checkout_id
  JOIN repo rp    ON rp.id = c.repo_id
"""

# Partition by the LOGICAL location, not symbol.id. Sibling worktrees sit on
# different branches, so the same file exists as several distinct blobs -- e.g.
# one file can have one version in 30 checkouts and another in 15. Both carry
# their own symbol row for the same class at the same line, so partitioning by id
# surfaced the identical location twice.
_ROWNUM = """
  ROW_NUMBER() OVER (
    PARTITION BY s.name, s.kind, rp.id, f.rel_path, s.start_line
    ORDER BY c.is_canonical DESC, length(c.path), c.path
  ) AS rn
"""


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


def _clean(d: dict) -> dict:
    d.pop("rn", None)
    if d.get("decorators"):
        try:
            d["decorators"] = json.loads(d["decorators"])
        except Exception:
            pass
    return d


def find_def(con, name: str, kind: str | None = None, repo: str | None = None,
             limit: int = 40, fuzzy: bool = True) -> list[dict]:
    """Where is `name` defined?  Exact match first, then case-insensitive, then prefix."""
    base = f"""
    SELECT * FROM (
      SELECT s.name, s.kind, s.start_line, s.end_line, s.parent, s.signature,
             s.doc, s.decorators, s.is_async, s.exported,
             rp.name AS repo, f.rel_path, s.blob_sha,
             c.is_canonical, c.branch, {_ROWNUM}
      FROM symbol s {_LOC_JOIN}
      WHERE {{pred}}
        {{kindf}} {{repof}}
    ) WHERE rn = 1
    ORDER BY CASE kind WHEN 'class' THEN 0 WHEN 'component' THEN 0
                       WHEN 'function' THEN 1 WHEN 'interface' THEN 1
                       WHEN 'method' THEN 2 ELSE 3 END, repo, rel_path
    LIMIT ?
    """
    kindf = "AND s.kind = ?" if kind else ""
    repof = "AND rp.name LIKE ?" if repo else ""

    def run(pred, params):
        sql = base.format(pred=pred, kindf=kindf, repof=repof)
        p = list(params)
        if kind:
            p.append(kind)
        if repo:
            p.append(f"%{repo}%")
        p.append(limit)
        return _rows(con.execute(sql, p))

    out = run("s.name = ?", [name])
    if not out:
        out = run("s.name = ? COLLATE NOCASE", [name])
    if not out and fuzzy and len(name) >= 3:
        out = run("s.name LIKE ? COLLATE NOCASE", [f"%{name}%"])
    return [_clean(r) for r in out]


def find_refs(con, name: str, kind: str | None = None, repo: str | None = None,
              limit: int = 100) -> list[dict]:
    """Who references `name`?  kind: call | jsx"""
    sql = f"""
    SELECT * FROM (
      SELECT r.name, r.kind, r.line, r.context,
             rp.name AS repo, f.rel_path, r.blob_sha,
             c.is_canonical, c.branch,
             ROW_NUMBER() OVER (
               PARTITION BY r.name, r.kind, rp.id, f.rel_path, r.line
               ORDER BY c.is_canonical DESC, length(c.path), c.path
             ) AS rn
      FROM ref r
      JOIN file f     ON f.blob_sha = r.blob_sha
      JOIN checkout c ON c.id = f.checkout_id
      JOIN repo rp    ON rp.id = c.repo_id
      WHERE r.name = ? {"AND r.kind = ?" if kind else ""}
        {"AND rp.name LIKE ?" if repo else ""}
    ) WHERE rn = 1
    ORDER BY repo, rel_path, line
    LIMIT ?
    """
    p: list = [name]
    if kind:
        p.append(kind)
    if repo:
        p.append(f"%{repo}%")
    p.append(limit)
    return [_clean(r) for r in _rows(con.execute(sql, p))]


def callers(con, name: str, repo: str | None = None, limit: int = 100) -> list[dict]:
    """Call sites of `name`, attributed to the enclosing symbol."""
    return find_refs(con, name, kind="call", repo=repo, limit=limit)


def outline(con, path_fragment: str, limit: int = 400) -> list[dict]:
    """Symbol outline for the file(s) matching a path fragment."""
    sql = f"""
    SELECT * FROM (
      SELECT s.name, s.kind, s.start_line, s.end_line, s.parent, s.signature,
             s.doc, s.exported, s.is_async, rp.name AS repo, f.rel_path,
             c.is_canonical, c.branch, {_ROWNUM}
      FROM symbol s {_LOC_JOIN}
      WHERE f.rel_path LIKE ?
    ) WHERE rn = 1
    ORDER BY repo, rel_path, start_line
    LIMIT ?
    """
    return [_clean(r) for r in _rows(con.execute(sql, [f"%{path_fragment}%", limit]))]


def search(con, text: str, repo: str | None = None,
           limit: int = 40) -> list[dict]:
    """Full-text search over symbol names, signatures and docstrings."""
    fts_q = " ".join(f'"{t}"*' for t in text.split() if t)
    if not fts_q:
        return []
    # bm25() cannot be evaluated in the same scope as a window function
    # ("unable to use function bm25 in the requested context"), so score the
    # FTS matches in their own CTE and join the ranked ids afterwards.
    sql = f"""
    WITH hits AS (
      SELECT rowid AS sid, bm25(symbol_fts) AS score
      FROM symbol_fts
      WHERE symbol_fts MATCH ?
      ORDER BY score
      LIMIT 2000
    )
    SELECT * FROM (
      SELECT s.name, s.kind, s.start_line, s.end_line, s.parent, s.signature,
             s.doc, s.decorators, s.exported, rp.name AS repo, f.rel_path,
             h.score, c.is_canonical, c.branch, {_ROWNUM}
      FROM hits h
      JOIN symbol s ON s.id = h.sid
      {_LOC_JOIN}
      {{repof}}
    ) WHERE rn = 1
    ORDER BY score
    LIMIT ?
    """
    sql = sql.replace("{repof}", "WHERE rp.name LIKE ?" if repo else "")
    params = [fts_q] + ([f"%{repo}%"] if repo else []) + [limit]
    return [_clean(r) for r in _rows(con.execute(sql, params))]


def importers(con, target: str, limit: int = 80) -> list[dict]:
    """Files that import a given module path or symbol name."""
    sql = """
    SELECT * FROM (
      SELECT i.module, i.symbol, i.alias, i.line,
             rp.name AS repo, f.rel_path,
             ROW_NUMBER() OVER (
               PARTITION BY i.module, i.symbol, i.alias, rp.id, f.rel_path, i.line
               ORDER BY c.is_canonical DESC, length(c.path), c.path
             ) AS rn
      FROM import_edge i
      JOIN file f     ON f.blob_sha = i.blob_sha
      JOIN checkout c ON c.id = f.checkout_id
      JOIN repo rp    ON rp.id = c.repo_id
      WHERE i.module LIKE ? OR i.symbol = ?
    ) WHERE rn = 1
    ORDER BY repo, rel_path, line
    LIMIT ?
    """
    return [_clean(r) for r in _rows(con.execute(sql, [f"%{target}%", target, limit]))]


def imports_of(con, path_fragment: str, limit: int = 200) -> list[dict]:
    """What a file imports."""
    sql = """
    SELECT DISTINCT i.module, i.symbol, i.alias, i.line, f.rel_path, rp.name AS repo
    FROM import_edge i
    JOIN file f     ON f.blob_sha = i.blob_sha
    JOIN checkout c ON c.id = f.checkout_id
    JOIN repo rp    ON rp.id = c.repo_id
    WHERE f.rel_path LIKE ?
    ORDER BY i.line
    LIMIT ?
    """
    return _rows(con.execute(sql, [f"%{path_fragment}%", limit]))


def stats(con) -> dict:
    g = lambda q: con.execute(q).fetchone()[0]
    langs = _rows(con.execute(
        "SELECT lang, COUNT(*) blobs, SUM(loc) loc FROM blob GROUP BY lang ORDER BY blobs DESC"
    ))
    kinds = _rows(con.execute(
        "SELECT kind, COUNT(*) n FROM symbol GROUP BY kind ORDER BY n DESC"
    ))
    repos = _rows(con.execute("""
        SELECT rp.name repo, COUNT(DISTINCT c.id) checkouts,
               SUM(c.is_canonical) canonical
        FROM repo rp JOIN checkout c ON c.repo_id = rp.id
        GROUP BY rp.id ORDER BY checkouts DESC
    """))
    return {
        "repos": g("SELECT COUNT(*) FROM repo"),
        "checkouts": g("SELECT COUNT(*) FROM checkout"),
        "file_rows": g("SELECT COUNT(*) FROM file"),
        "unique_blobs": g("SELECT COUNT(*) FROM blob"),
        "symbols": g("SELECT COUNT(*) FROM symbol"),
        "refs": g("SELECT COUNT(*) FROM ref"),
        "imports": g("SELECT COUNT(*) FROM import_edge"),
        "loc": g("SELECT COALESCE(SUM(loc),0) FROM blob"),
        "dedup_ratio": round(
            g("SELECT COUNT(*) FROM file") / max(g("SELECT COUNT(*) FROM blob"), 1), 2
        ),
        "by_lang": langs,
        "by_kind": kinds,
        "by_repo": repos,
    }


def doctor(con, limit: int = 50) -> dict:
    """Report where symbol recall is degraded by parse errors."""
    bad = _rows(con.execute(f"""
        SELECT b.lang, b.error_count, b.loc,
               (SELECT f.rel_path FROM file f WHERE f.blob_sha = b.sha LIMIT 1) rel_path,
               (SELECT rp.name FROM file f
                  JOIN checkout c ON c.id = f.checkout_id
                  JOIN repo rp ON rp.id = c.repo_id
                WHERE f.blob_sha = b.sha LIMIT 1) repo
        FROM blob b WHERE b.parse_ok = 0
        ORDER BY b.error_count DESC LIMIT {int(limit)}
    """))
    total = con.execute("SELECT COUNT(*) FROM blob").fetchone()[0]
    clean = con.execute("SELECT COUNT(*) FROM blob WHERE parse_ok=1").fetchone()[0]
    stale = _rows(con.execute("""
        SELECT c.path, rp.name repo, c.branch, c.indexed_at
        FROM checkout c JOIN repo rp ON rp.id = c.repo_id
        ORDER BY c.indexed_at LIMIT 5
    """))
    return {
        "blobs": total, "clean": clean,
        "degraded": total - clean,
        "clean_pct": round(100.0 * clean / max(total, 1), 2),
        "files_with_errors": bad,
        "oldest_checkouts": stale,
    }
