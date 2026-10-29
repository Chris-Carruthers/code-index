"""SQLite store for the AST index.

Design note: symbols and refs are keyed by *git blob SHA*, not by file path.
A machine running many agents in parallel can hold dozens of worktrees of one repo,
so path-keyed storage would parse the same content dozens of times. Blob-keyed
storage means the 2nd..Nth checkout of a repo is almost free: we only parse
blobs we have never seen.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(
    os.environ.get(
        "CODEINDEX_DB",
        Path.home() / ".cache" / "code-index" / "index.db",
    )
)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS repo (
  id      INTEGER PRIMARY KEY,
  remote  TEXT UNIQUE NOT NULL,
  name    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkout (
  id           INTEGER PRIMARY KEY,
  repo_id      INTEGER NOT NULL REFERENCES repo(id) ON DELETE CASCADE,
  path         TEXT UNIQUE NOT NULL,
  branch       TEXT,
  head         TEXT,
  is_canonical INTEGER NOT NULL DEFAULT 0,
  indexed_at   TEXT,
  fingerprint  TEXT
);

-- One row per unique content blob. The unit of parsing.
CREATE TABLE IF NOT EXISTS blob (
  sha         TEXT PRIMARY KEY,
  lang        TEXT NOT NULL,
  loc         INTEGER NOT NULL DEFAULT 0,
  bytes       INTEGER NOT NULL DEFAULT 0,
  parsed_at   TEXT,
  parse_ok    INTEGER NOT NULL DEFAULT 1,
  error_count INTEGER NOT NULL DEFAULT 0,
  skipped     INTEGER NOT NULL DEFAULT 0
);

-- Maps a checkout's path to the blob it currently holds.
CREATE TABLE IF NOT EXISTS file (
  id          INTEGER PRIMARY KEY,
  checkout_id INTEGER NOT NULL REFERENCES checkout(id) ON DELETE CASCADE,
  rel_path    TEXT NOT NULL,
  blob_sha    TEXT NOT NULL,
  UNIQUE(checkout_id, rel_path)
);

CREATE TABLE IF NOT EXISTS symbol (
  id         INTEGER PRIMARY KEY,
  blob_sha   TEXT NOT NULL,
  name       TEXT NOT NULL,
  kind       TEXT NOT NULL,
  start_line INTEGER NOT NULL,
  end_line   INTEGER NOT NULL,
  parent     TEXT,
  signature  TEXT,
  doc        TEXT,
  decorators TEXT,
  is_async   INTEGER NOT NULL DEFAULT 0,
  exported   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ref (
  id       INTEGER PRIMARY KEY,
  blob_sha TEXT NOT NULL,
  name     TEXT NOT NULL,
  kind     TEXT NOT NULL,
  line     INTEGER NOT NULL,
  context  TEXT
);

CREATE TABLE IF NOT EXISTS import_edge (
  id       INTEGER PRIMARY KEY,
  blob_sha TEXT NOT NULL,
  module   TEXT,
  symbol   TEXT,
  alias    TEXT,
  line     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_symbol_name  ON symbol(name);
CREATE INDEX IF NOT EXISTS idx_symbol_blob  ON symbol(blob_sha);
CREATE INDEX IF NOT EXISTS idx_symbol_kind  ON symbol(kind);
CREATE INDEX IF NOT EXISTS idx_ref_name     ON ref(name);
CREATE INDEX IF NOT EXISTS idx_ref_blob     ON ref(blob_sha);
CREATE INDEX IF NOT EXISTS idx_imp_module   ON import_edge(module);
CREATE INDEX IF NOT EXISTS idx_imp_symbol   ON import_edge(symbol);
CREATE INDEX IF NOT EXISTS idx_imp_blob     ON import_edge(blob_sha);
CREATE INDEX IF NOT EXISTS idx_file_blob    ON file(blob_sha);
CREATE INDEX IF NOT EXISTS idx_file_relpath ON file(rel_path);

CREATE VIRTUAL TABLE IF NOT EXISTS symbol_fts USING fts5(
  name, signature, doc,
  content='symbol', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
"""


# CREATE TABLE IF NOT EXISTS silently does nothing to an existing table, so a
# new column would never appear on an already-built index. Add them explicitly.
ADDED_COLUMNS = [
    ("blob", "skipped", "INTEGER NOT NULL DEFAULT 0"),
    ("checkout", "fingerprint", "TEXT"),
]


def _migrate(con: sqlite3.Connection) -> None:
    for table, col, decl in ADDED_COLUMNS:
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        if have and col not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    con.commit()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else DEFAULT_DB
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def rebuild_fts(con: sqlite3.Connection) -> None:
    con.execute("INSERT INTO symbol_fts(symbol_fts) VALUES('rebuild')")


def drop_blob_data(con: sqlite3.Connection, shas: list[str]) -> None:
    """Remove parsed rows for blobs we are about to re-parse."""
    if not shas:
        return
    for i in range(0, len(shas), 500):
        chunk = shas[i : i + 500]
        marks = ",".join("?" * len(chunk))
        con.execute(
            f"DELETE FROM symbol_fts WHERE rowid IN "
            f"(SELECT id FROM symbol WHERE blob_sha IN ({marks}))",
            chunk,
        )
        for tbl in ("symbol", "ref", "import_edge"):
            con.execute(f"DELETE FROM {tbl} WHERE blob_sha IN ({marks})", chunk)
