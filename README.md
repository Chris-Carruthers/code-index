# code-index

An AST-backed code index over every git checkout on your machine, built for
coding agents. Ships as a CLI and an MCP server.

Parses Python, TypeScript, TSX, JavaScript, SQL and bash with tree-sitter and stores **symbols, references and imports** in SQLite. Answers
"where is this defined", "who calls this", "what's in this file" with exact
`repo/path:line` citations in ~0.2s, instead of grepping and guessing.

## Why it exists

Agents burn tokens grepping for definitions and still
mis-cite lines. Text search can't tell a definition from a mention, can't
attribute a call site to its enclosing function, and can't tell you a registry
entry has no consumer.

## The core design decision: content addressing

Run several agents in parallel and each task wants its own worktree, so one
machine ends up holding dozens of checkouts of the same repo.

Symbols are therefore keyed by **git blob SHA, not file path**. Consequences:

- The 2nd..Nth checkout of a repo is nearly free — only content never seen
  before is parsed. Measured: **~90k files → ~7k unique blobs (13x)**.
- Blob SHAs come from `git ls-files -s`, so a full pass reads only the files it
  will actually parse, not all ~90k. This cut a cold index from **~5m to ~80s**.
- Queries de-duplicate by `(symbol, repo, rel_path)` and prefer the canonical
  clone, so `code_def` returns **one** line, not the same line once per worktree.

## Install

Requires **Python 3.11+** and `git`. macOS and Linux.

```sh
git clone https://github.com/Chris-Carruthers/code-index.git
cd code-index
./install.sh
```

`install.sh` builds the venv, installs the package, indexes whatever checkouts it
can find, and prints the MCP registration command. Re-run it any time.

Then wire it into Claude Code (the command `install.sh` prints):

```sh
claude mcp add code-index --scope user -- /abs/path/to/code-index/bin/codeindex mcp
```

The index lives at `~/.cache/code-index/index.db` (~200 MB for ~3M
lines of code); override with `$CODEINDEX_DB`.

### Coverage is only the repos you have cloned

This is the thing to understand before trusting a result. The index is built from
**git checkouts on your own disk** — there is no server and nothing is fetched. If
you have three repos cloned, you get a three-repo index, and `code_callers` will
report zero callers for things that are called all day in a repo you don't have.

So tell it where your repos are. Resolution order:

1. `$CODEINDEX_ROOT` — colon-separated, wins outright
2. `~/.config/code-index/roots` — one path per line, `#` comments allowed
3. auto-detection — common code dirs (`~/src`, `~/code`, `~/repos`, `~/Projects`,
   `~/dev`, `~/git`, `~/workspace`) that exist
   *and* contain at least one git checkout
4. the current directory, if it holds a checkout

`codeindex index` prints the roots it chose and where they came from, so
under-coverage is visible rather than silent. Point it anywhere explicitly with
`codeindex index ~/path/to/repos`, or add just the repo you're in with
`codeindex index --here`.

### Keeping it fresh

The index is a snapshot of the last run — it does not watch the filesystem. Either
re-run on demand (`code_reindex` from an agent, or `codeindex index`), or wire a
git hook in each repo you care about:

```sh
printf '#!/bin/sh\nexec /abs/path/to/code-index/bin/codeindex index --here -q &\n' \
  > .git/hooks/post-merge && chmod +x .git/hooks/post-merge
```

Incremental runs are cheap — content already seen is never re-parsed.

## CLI

```sh
codeindex def parse_config             # where is it defined
codeindex callers load_plugins          # who calls it (empty => suspect dead code)
codeindex refs Button --kind jsx        # where a component is rendered
codeindex outline app/routes.py         # symbol map with line ranges
codeindex search "retry backoff"        # FTS over names, signatures, docstrings
codeindex importers app.plugins         # blast radius of a change
codeindex imports app/routes.py         # what a file imports
codeindex stats                         # index composition
codeindex doctor                        # where symbol recall is degraded
codeindex index --canonical-only        # one checkout per repo
codeindex index --here                  # add the current worktree
```

Add `--json` (before or after the subcommand) for machine-readable output.

## MCP

A **stdio** server — the agent spawns it as a child process. Nothing listens on a
port, nothing is hosted, no credentials are involved. Exposes `code_def`,
`code_callers`, `code_refs`, `code_outline`, `code_search`, `code_importers`,
`code_imports`, `code_stats`, `code_doctor`, `code_reindex`. Results are returned
as dense text rather than JSON, because an agent pays per token.

Run standalone: `bin/codeindex mcp` (stdio).

## Measured

On the author's machine: ~20 repos, ~135 checkouts, ~3M lines of code.

| | |
|---|---|
| Files → unique blobs | ~96k → ~8k (11.6x dedup) |
| Cold index | ~90s |
| Incremental (unchanged) | ~15s |
| Single lookup | ~0.16s |
| Clean parses | 98% of blobs |

## Known limitations

Stated plainly, because a code index that overstates its recall is worse than none.

1. **Static only.** Dynamic dispatch is invisible: `getattr`, string-keyed
   registries, decorator-driven routing, dependency injection. Zero callers means
   "no *static* call sites" — verify before calling something dead.
2. **Name-based, not type-resolved.** `code_callers("start")` matches every
   `start()` call, not only calls to the one you meant. There is no type checker
   here; filter with `--repo` and read the context field.
3. **~2% of blobs parse partially.** Chiefly TypeScript using `import('...')`
   types in generic positions, which the TS grammar mishandles, plus
   Postgres-specific DDL under the generic SQL grammar. `codeindex doctor` lists
   every affected file. Symbols inside the broken span are missing.
4. **Snapshot, not live.** Reflects the last index run. Re-run after edits.
5. **`.d.ts` and minified files are skipped** by design.
6. **Branch-only hits are labelled, not hidden.** Sibling worktrees sit on
   different branches, so a symbol may exist only on unmerged work. Those results
   carry `BRANCH ONLY: <branch>, not in canonical clone` — treat them as
   unmerged, and do not cite them as shipped behaviour.

## Layout

```
src/codeindex/
  db.py          schema, connection, additive column migrations
  extract.py     tree-sitter walk -> symbols / refs / imports
  indexer.py     git-based discovery, content-addressed indexing
  query.py       lookup API with cross-checkout de-duplication
  cli.py         command line, root resolution
  mcp_server.py  MCP stdio server
tests/           incl. a regression test for every bug found building this
```

Node types are matched by walking the tree rather than with tree-sitter query
strings: node type names are stable across grammar releases, query syntax is not,
and a query that fails to compile silently takes out a whole language.

## Tests

```sh
.venv/bin/python -m pytest tests/ -q
```

## Use it from canon

[canon](https://github.com/Chris-Carruthers/canon) ships a `code-index` skill
that tells agents when to reach for these tools instead of grep. Install this,
register the MCP server, and the skill picks it up.

## License

MIT
