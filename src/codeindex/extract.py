"""AST extraction: source bytes -> symbols, refs, imports.

We walk the tree and dispatch on `node.type` rather than using tree-sitter
S-expression queries. Node type names are stable across grammar releases;
query syntax is not, and a query that fails to compile takes out a whole
language silently. Walking also gives us parent nesting for free, which is
what makes `parent` (e.g. "SearchInput.validate") accurate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from tree_sitter_language_pack import get_parser

EXT_LANG = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "tsx",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".sql": "sql",
    ".sh": "bash",
    ".bash": "bash",
}

_PARSERS: dict[str, Any] = {}


def parser_for(lang: str):
    if lang not in _PARSERS:
        _PARSERS[lang] = get_parser(lang)
    return _PARSERS[lang]


@dataclass
class Symbol:
    name: str
    kind: str
    start_line: int
    end_line: int
    parent: str | None = None
    signature: str | None = None
    doc: str | None = None
    decorators: list[str] = field(default_factory=list)
    is_async: bool = False
    exported: bool = False


@dataclass
class Ref:
    name: str
    kind: str  # call | jsx | attr
    line: int
    context: str | None = None


@dataclass
class Import:
    module: str | None
    symbol: str | None
    alias: str | None
    line: int


@dataclass
class Extraction:
    symbols: list[Symbol] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    loc: int = 0
    error_count: int = 0

    @property
    def parse_ok(self) -> bool:
        return self.error_count == 0


def _txt(src: bytes, node) -> str:
    return src[node.start_byte : node.end_byte].decode("utf8", "replace")


def _line(node) -> int:
    return node.start_point[0] + 1


def _child_field(node, name):
    try:
        return node.child_by_field_name(name)
    except Exception:
        return None


def _count_errors(root) -> int:
    if not root.has_error:
        return 0
    n = 0
    stack = [root]
    while stack:
        cur = stack.pop()
        if cur.type == "ERROR" or cur.is_missing:
            n += 1
            continue
        if cur.has_error:
            stack.extend(cur.children)
    return n


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------

def _py_signature(src: bytes, node) -> str:
    body = _child_field(node, "body")
    end = body.start_byte if body else node.end_byte
    return src[node.start_byte : end].decode("utf8", "replace").rstrip().rstrip(":").strip()


def _py_docstring(src: bytes, node) -> str | None:
    body = _child_field(node, "body")
    if not body or not body.named_child_count:
        return None
    first = body.named_children[0]
    # Grammar note: this python grammar exposes a bare `string` as the block's
    # first named child. Other versions wrap it in `expression_statement`.
    # Accept both so a grammar bump doesn't silently drop every docstring.
    if first.type == "expression_statement" and first.named_child_count:
        first = first.named_children[0]
    if first.type != "string":
        return None
    content = next(
        (c for c in first.named_children if c.type == "string_content"), None
    )
    if content is not None:
        raw = _txt(src, content)
    else:
        raw = _txt(src, first).strip().strip("rbuf")
        for q in ('"' * 3, "'" * 3, '"', "'"):
            if raw.startswith(q) and raw.endswith(q) and len(raw) >= 2 * len(q):
                raw = raw[len(q) : -len(q)]
                break
    line = raw.strip().split("\n", 1)[0].strip()
    return line[:400] or None


def _walk_python(src: bytes, node, out: Extraction, parents: list[str], decorators: list[str]) -> None:
    t = node.type

    if t == "decorated_definition":
        decs = [
            _txt(src, c).lstrip("@").strip()
            for c in node.named_children
            if c.type == "decorator"
        ]
        inner = _child_field(node, "definition")
        if inner is not None:
            _walk_python(src, inner, out, parents, decs)
        return

    if t in ("function_definition", "class_definition"):
        name_n = _child_field(node, "name")
        name = _txt(src, name_n) if name_n else "<anonymous>"
        if t == "class_definition":
            kind = "class"
        else:
            kind = "method" if parents else "function"
        out.symbols.append(
            Symbol(
                name=name,
                kind=kind,
                start_line=_line(node),
                end_line=node.end_point[0] + 1,
                parent=".".join(parents) or None,
                signature=_py_signature(src, node),
                doc=_py_docstring(src, node),
                decorators=decorators,
                is_async=src[node.start_byte : node.start_byte + 6] == b"async ",
            )
        )
        for c in node.children:
            _walk_python(src, c, out, parents + [name], [])
        return

    if t == "import_statement":
        for c in node.named_children:
            if c.type == "dotted_name":
                out.imports.append(Import(_txt(src, c), None, None, _line(node)))
            elif c.type == "aliased_import":
                nm = _child_field(c, "name")
                al = _child_field(c, "alias")
                out.imports.append(
                    Import(_txt(src, nm) if nm else None, None,
                           _txt(src, al) if al else None, _line(node))
                )
        return

    if t == "import_from_statement":
        mod_n = _child_field(node, "module_name")
        mod = _txt(src, mod_n) if mod_n else None
        # py-tree-sitter returns a fresh object per accessor, so `c is mod_n`
        # is never true and the module name leaks in as an imported symbol.
        # Compare byte offsets instead.
        mod_start = mod_n.start_byte if mod_n is not None else -1
        for c in node.named_children:
            if c.start_byte == mod_start:
                continue
            if c.type == "dotted_name":
                out.imports.append(Import(mod, _txt(src, c), None, _line(node)))
            elif c.type == "aliased_import":
                nm = _child_field(c, "name")
                al = _child_field(c, "alias")
                out.imports.append(
                    Import(mod, _txt(src, nm) if nm else None,
                           _txt(src, al) if al else None, _line(node))
                )
        if not any(i.line == _line(node) and i.symbol for i in out.imports):
            out.imports.append(Import(mod, None, None, _line(node)))
        return

    if t == "call":
        fn = _child_field(node, "function")
        if fn is not None:
            if fn.type == "identifier":
                out.refs.append(Ref(_txt(src, fn), "call", _line(node), ".".join(parents) or None))
            elif fn.type == "attribute":
                attr = _child_field(fn, "attribute")
                if attr is not None:
                    out.refs.append(
                        Ref(_txt(src, attr), "call", _line(node), ".".join(parents) or None)
                    )

    # module-level constants
    if t == "assignment" and not parents:
        left = _child_field(node, "left")
        if left is not None and left.type == "identifier":
            nm = _txt(src, left)
            if nm.isupper() or nm.startswith("_"):
                out.symbols.append(
                    Symbol(nm, "const", _line(node), node.end_point[0] + 1, None,
                           _txt(src, node).split("\n")[0][:200])
                )

    for c in node.children:
        _walk_python(src, c, out, parents, [])


# --------------------------------------------------------------------------
# TypeScript / TSX / JavaScript
# --------------------------------------------------------------------------

_TS_DECL_KIND = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
    "module": "namespace",
}


def _ts_signature(src: bytes, node) -> str:
    body = _child_field(node, "body")
    end = body.start_byte if body else node.end_byte
    sig = src[node.start_byte : end].decode("utf8", "replace")
    return " ".join(sig.split())[:400]


def _jsdoc_before(src: bytes, node) -> str | None:
    prev = node.prev_sibling
    if prev is not None and prev.type == "comment":
        raw = _txt(src, prev)
        if raw.startswith("/**"):
            for ln in raw.splitlines():
                ln = ln.strip().lstrip("/*").lstrip("*").strip()
                if ln and not ln.startswith("@"):
                    return ln[:400]
    return None


def _walk_ts(src: bytes, node, out: Extraction, parents: list[str],
             lang: str, exported: bool = False) -> None:
    t = node.type

    if t == "export_statement":
        for c in node.children:
            _walk_ts(src, c, out, parents, lang, True)
        return

    if t in _TS_DECL_KIND:
        name_n = _child_field(node, "name")
        name = _txt(src, name_n) if name_n else "<anonymous>"
        kind = _TS_DECL_KIND[t]
        if kind == "function" and lang == "tsx" and name[:1].isupper():
            kind = "component"
        out.symbols.append(
            Symbol(name, kind, _line(node), node.end_point[0] + 1,
                   ".".join(parents) or None, _ts_signature(src, node),
                   _jsdoc_before(src, node), [], "async" in _ts_signature(src, node)[:20],
                   exported)
        )
        for c in node.children:
            _walk_ts(src, c, out, parents + [name], lang, False)
        return

    if t in ("method_definition", "public_field_definition"):
        name_n = _child_field(node, "name")
        if name_n is not None:
            nm = _txt(src, name_n)
            val = _child_field(node, "value")
            kind = "method"
            if t == "public_field_definition":
                kind = "method" if val is not None and val.type in (
                    "arrow_function", "function_expression") else "property"
            out.symbols.append(
                Symbol(nm, kind, _line(node), node.end_point[0] + 1,
                       ".".join(parents) or None, _ts_signature(src, node),
                       _jsdoc_before(src, node), [], False, exported)
            )
            for c in node.children:
                _walk_ts(src, c, out, parents + [nm], lang, False)
            return

    if t == "variable_declarator":
        name_n = _child_field(node, "name")
        val = _child_field(node, "value")
        if name_n is not None and name_n.type == "identifier":
            nm = _txt(src, name_n)
            if val is not None and val.type in ("arrow_function", "function_expression"):
                kind = "component" if (lang == "tsx" and nm[:1].isupper()) else "function"
            else:
                kind = "const"
            sig = " ".join(
                src[node.start_byte : (val.start_byte if val is not None else node.end_byte)]
                .decode("utf8", "replace").split()
            )[:300]
            out.symbols.append(
                Symbol(nm, kind, _line(node), node.end_point[0] + 1,
                       ".".join(parents) or None, sig,
                       _jsdoc_before(src, node.parent or node), [], False, exported)
            )
            for c in node.children:
                _walk_ts(src, c, out, parents + [nm], lang, False)
            return

    if t == "lexical_declaration" or t == "variable_declaration":
        for c in node.children:
            _walk_ts(src, c, out, parents, lang, exported)
        return

    if t == "import_statement":
        srcn = _child_field(node, "source")
        mod = _txt(src, srcn).strip("'\"`") if srcn else None
        named: list[Import] = []
        stack = [node]
        while stack:
            cur = stack.pop()
            if cur.type == "import_specifier":
                nm = _child_field(cur, "name")
                al = _child_field(cur, "alias")
                named.append(Import(mod, _txt(src, nm) if nm else None,
                                    _txt(src, al) if al else None, _line(node)))
            elif cur.type in ("namespace_import",):
                named.append(Import(mod, "*", None, _line(node)))
            elif cur.type == "identifier" and cur.parent is not None \
                    and cur.parent.type == "import_clause":
                named.append(Import(mod, "default", _txt(src, cur), _line(node)))
            else:
                stack.extend(cur.children)
        out.imports.extend(named or [Import(mod, None, None, _line(node))])
        return

    if t == "call_expression":
        fn = _child_field(node, "function")
        if fn is not None:
            if fn.type == "identifier":
                out.refs.append(Ref(_txt(src, fn), "call", _line(node), ".".join(parents) or None))
            elif fn.type == "member_expression":
                prop = _child_field(fn, "property")
                if prop is not None:
                    out.refs.append(
                        Ref(_txt(src, prop), "call", _line(node), ".".join(parents) or None)
                    )

    if t in ("jsx_opening_element", "jsx_self_closing_element"):
        name_n = _child_field(node, "name")
        if name_n is not None:
            nm = _txt(src, name_n)
            if nm[:1].isupper() or "." in nm:
                out.refs.append(Ref(nm, "jsx", _line(node), ".".join(parents) or None))

    for c in node.children:
        _walk_ts(src, c, out, parents, lang, False)


# --------------------------------------------------------------------------
# SQL / bash (light: top-level objects only)
# --------------------------------------------------------------------------

def _walk_generic(src: bytes, node, out: Extraction, lang: str) -> None:
    stack = [node]
    while stack:
        cur = stack.pop()
        if lang == "bash" and cur.type == "function_definition":
            nm = _child_field(cur, "name")
            if nm is not None:
                out.symbols.append(
                    Symbol(_txt(src, nm), "function", _line(cur), cur.end_point[0] + 1)
                )
        elif lang == "sql" and cur.type in (
            "create_table", "create_view", "create_function", "create_index"
        ):
            ident = next(
                (c for c in cur.named_children if "identifier" in c.type or c.type == "object_reference"),
                None,
            )
            out.symbols.append(
                Symbol(_txt(src, ident) if ident is not None else cur.type,
                       cur.type.replace("create_", ""), _line(cur), cur.end_point[0] + 1)
            )
        stack.extend(cur.children)


def extract(source: bytes, lang: str) -> Extraction:
    out = Extraction()
    out.loc = source.count(b"\n") + (1 if source and not source.endswith(b"\n") else 0)
    tree = parser_for(lang).parse(source)
    root = tree.root_node
    out.error_count = _count_errors(root)

    if lang == "python":
        _walk_python(source, root, out, [], [])
    elif lang in ("typescript", "tsx", "javascript"):
        _walk_ts(source, root, out, [], lang)
    else:
        _walk_generic(source, root, out, lang)

    # de-dup refs (same name+line)
    seen = set()
    uniq = []
    for r in out.refs:
        k = (r.name, r.line, r.kind)
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    out.refs = uniq
    return out
