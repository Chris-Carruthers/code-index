"""End-to-end indexing and query tests against real temporary git repos."""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codeindex import query as Q
from codeindex.db import connect, rebuild_fts
from codeindex.indexer import index_checkout, normalize_remote

SRC = '''
class Engine:
    """The engine."""

    def start(self) -> None:
        """Start it."""
        ignite()


def ignite() -> None:
    """Light the fire."""
    pass


def orphan() -> None:
    """Nobody calls this."""
    pass
'''


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


def _make_repo(path: Path, remote: str, content: str = SRC) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    _git(path, "remote", "add", "origin", remote)
    (path / "engine.py").write_text(content)
    _git(path, "add", "engine.py")
    _git(path, "commit", "-qm", "init")
    return path


@pytest.fixture
def con(tmp_path):
    return connect(tmp_path / "idx.db")


def test_index_extracts_symbols(tmp_path, con):
    repo = _make_repo(tmp_path / "enginerepo", "git@github.com:acme/enginerepo.git")
    res = index_checkout(con, repo)
    rebuild_fts(con)
    assert res["files"] == 1
    assert res["new_blobs"] == 1
    assert res["symbols"] >= 4

    defs = Q.find_def(con, "ignite")
    assert len(defs) == 1
    assert defs[0]["rel_path"] == "engine.py"
    assert defs[0]["doc"] == "Light the fire."
    assert defs[0]["repo"] == "enginerepo"


def test_second_checkout_reuses_blobs_and_dedups_results(tmp_path, con):
    """The core claim: N checkouts of identical content parse once and read as one."""
    a = _make_repo(tmp_path / "enginerepo", "git@github.com:acme/enginerepo.git")
    r1 = index_checkout(con, a)
    assert r1["new_blobs"] == 1

    b = tmp_path / "enginerepo-task"
    subprocess.run(["git", "clone", "-q", str(a), str(b)], check=True,
                   capture_output=True)
    _git(b, "remote", "set-url", "origin", "git@github.com:acme/enginerepo.git")
    r2 = index_checkout(con, b)
    assert r2["new_blobs"] == 0, "identical content must not be re-parsed"
    assert r2["reused"] == r2["files"]

    rebuild_fts(con)
    # Two checkouts on disk, but a lookup must return one location.
    defs = Q.find_def(con, "ignite")
    assert len(defs) == 1
    # ...and it must be the canonical clone, not the task worktree.
    assert defs[0]["repo"] == "enginerepo"

    raw = con.execute(
        "SELECT COUNT(*) FROM symbol s JOIN file f ON f.blob_sha=s.blob_sha "
        "WHERE s.name='ignite'"
    ).fetchone()[0]
    assert raw == 2, "both checkouts should still be recorded underneath"


def test_callers_and_dead_code(tmp_path, con):
    repo = _make_repo(tmp_path / "enginerepo", "git@github.com:acme/enginerepo.git")
    index_checkout(con, repo)
    rebuild_fts(con)

    callers = Q.callers(con, "ignite")
    assert len(callers) == 1
    assert callers[0]["context"] == "Engine.start"

    assert Q.callers(con, "orphan") == [], "uncalled function must report no callers"


def test_search_returns_results(tmp_path, con):
    # Regression: bm25() alongside a window function raised
    # "unable to use function bm25 in the requested context", and a bare
    # `except: return []` turned that into a silent empty result.
    repo = _make_repo(tmp_path / "enginerepo", "git@github.com:acme/enginerepo.git")
    index_checkout(con, repo)
    rebuild_fts(con)
    hits = Q.search(con, "fire")
    assert any(h["name"] == "ignite" for h in hits)


def test_outline_and_imports(tmp_path, con):
    repo = _make_repo(
        tmp_path / "enginerepo", "git@github.com:acme/enginerepo.git",
        "import os\nfrom pathlib import Path\n\n\ndef go():\n    pass\n",
    )
    index_checkout(con, repo)
    rebuild_fts(con)
    out = Q.outline(con, "engine.py")
    assert [o["name"] for o in out] == ["go"]
    imps = Q.imports_of(con, "engine.py")
    assert ("pathlib", "Path") in {(i["module"], i["symbol"]) for i in imps}


def test_dirty_working_tree_is_indexed_from_disk(tmp_path, con):
    """A modified file must be parsed from the working tree, not git's stale sha."""
    repo = _make_repo(tmp_path / "enginerepo", "git@github.com:acme/enginerepo.git")
    index_checkout(con, repo)
    (repo / "engine.py").write_text("def brand_new_symbol():\n    pass\n")
    index_checkout(con, repo)
    rebuild_fts(con)
    assert Q.find_def(con, "brand_new_symbol", fuzzy=False)


def test_unchanged_checkout_is_skipped(tmp_path, con):
    repo = _make_repo(tmp_path / "enginerepo", "git@github.com:acme/enginerepo.git")
    index_checkout(con, repo)
    again = index_checkout(con, repo)
    assert again.get("unchanged") is True


def test_normalize_remote_collapses_url_forms():
    a = normalize_remote("git@github.com:example-org/example-repo.git")
    b = normalize_remote("https://github.com/example-org/example-repo")
    assert a == b == "github.com/example-org/example-repo"


def test_migration_adds_columns_to_an_older_index(tmp_path):
    """A pre-existing index must gain new columns, not silently lack them."""
    p = tmp_path / "old.db"
    old = sqlite3.connect(p)
    old.executescript(
        """CREATE TABLE blob(sha TEXT PRIMARY KEY, lang TEXT NOT NULL,
             loc INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
             parsed_at TEXT, parse_ok INTEGER NOT NULL DEFAULT 1,
             error_count INTEGER NOT NULL DEFAULT 0);
           CREATE TABLE repo(id INTEGER PRIMARY KEY, remote TEXT UNIQUE NOT NULL,
             name TEXT NOT NULL);
           CREATE TABLE checkout(id INTEGER PRIMARY KEY,
             repo_id INTEGER NOT NULL REFERENCES repo(id), path TEXT UNIQUE NOT NULL,
             branch TEXT, head TEXT, is_canonical INTEGER NOT NULL DEFAULT 0,
             indexed_at TEXT);"""
    )
    old.commit()
    old.close()

    con = connect(p)
    assert "skipped" in {r["name"] for r in con.execute("PRAGMA table_info(blob)")}
    assert "fingerprint" in {
        r["name"] for r in con.execute("PRAGMA table_info(checkout)")
    }


def test_same_path_on_two_branches_yields_one_result(tmp_path, con):
    """Regression: divergent worktrees produce several blobs for one path.

    Two checkouts on different branches hold different versions of engine.py, so
    there are two distinct `symbol` rows for `Engine` at the same line. Keying
    the de-dup on symbol.id treated them as separate hits and reported the
    identical location twice.
    """
    a = _make_repo(tmp_path / "enginerepo", "git@github.com:acme/enginerepo.git")
    index_checkout(con, a)

    b = tmp_path / "enginerepo-task"
    subprocess.run(["git", "clone", "-q", str(a), str(b)], check=True,
                   capture_output=True)
    _git(b, "remote", "set-url", "origin", "git@github.com:acme/enginerepo.git")
    # Change the tail of the file: `Engine` stays at the same line, but the
    # file's blob sha differs, so it parses to a second symbol row.
    (b / "engine.py").write_text(SRC + "\n\ndef only_on_branch() -> None:\n    pass\n")
    _git(b, "add", "engine.py")
    _git(b, "commit", "-qm", "branch work")
    index_checkout(con, b)
    rebuild_fts(con)

    engines = Q.find_def(con, "Engine", fuzzy=False)
    assert len(engines) == 1, f"expected one location, got {engines}"
    assert engines[0]["repo"] == "enginerepo"

    # Two symbol rows really do exist underneath.
    assert con.execute(
        "SELECT COUNT(*) FROM symbol WHERE name='Engine'"
    ).fetchone()[0] == 2

    # A symbol that exists only on the branch is still findable.
    assert Q.find_def(con, "only_on_branch", fuzzy=False)

    # And references de-dup the same way.
    calls = Q.callers(con, "ignite")
    assert len(calls) == 1, f"expected one call site, got {calls}"


def test_branch_only_hit_is_marked_not_canonical(tmp_path, con):
    """A symbol that exists only in a task worktree must be distinguishable.

    Regression: a helper appeared twice in one file at different
    lines and read as a duplicate definition. It is absent from the canonical
    clone entirely and lives on two unmerged branches -- the result was correct
    but indistinguishable from shipped code.
    """
    a = _make_repo(tmp_path / "enginerepo", "git@github.com:acme/enginerepo.git")
    index_checkout(con, a)

    b = tmp_path / "enginerepo-feature"
    subprocess.run(["git", "clone", "-q", str(a), str(b)], check=True,
                   capture_output=True)
    _git(b, "remote", "set-url", "origin", "git@github.com:acme/enginerepo.git")
    _git(b, "checkout", "-q", "-b", "chris/feat/new-thing")
    (b / "engine.py").write_text(SRC + "\n\ndef branch_only_fn() -> None:\n    pass\n")
    _git(b, "add", "engine.py")
    _git(b, "commit", "-qm", "feature")
    index_checkout(con, b)
    rebuild_fts(con)

    hits = Q.find_def(con, "branch_only_fn", fuzzy=False)
    assert len(hits) == 1
    assert hits[0]["is_canonical"] == 0
    assert hits[0]["branch"] == "chris/feat/new-thing"

    # a symbol present in the canonical clone must be marked canonical
    canon = Q.find_def(con, "ignite", fuzzy=False)
    assert canon[0]["is_canonical"] == 1


def test_search_can_be_scoped_to_one_repo(tmp_path, con):
    _make_repo(tmp_path / "alpha", "git@github.com:acme/alpha.git")
    _make_repo(tmp_path / "beta", "git@github.com:acme/beta.git")
    index_checkout(con, tmp_path / "alpha")
    index_checkout(con, tmp_path / "beta")
    rebuild_fts(con)

    assert {h["repo"] for h in Q.search(con, "fire")} == {"alpha", "beta"}
    scoped = Q.search(con, "fire", repo="alpha")
    assert scoped and {h["repo"] for h in scoped} == {"alpha"}
