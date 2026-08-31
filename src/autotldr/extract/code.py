"""Syntax-tree-backed Tier-0 source extraction.

Python uses the stdlib AST and remains dependency-free.  Other supported
languages lazy-load the pinned, offline-bundled tree-sitter pack only after the
router selects a source suffix.  Both paths make narrow claims from native node
kinds and exact source slices; neither turns arbitrary lines into declarations.

Suffixes without a matching grammar, and ambiguous suffixes without strong
content evidence, are declined by name.  A missed format is honest; a SQL table
labeled as a function or MATLAB handed to an Objective-C grammar is not.
"""

from __future__ import annotations

import ast
import io
import tokenize
import warnings
from bisect import bisect_right
from pathlib import Path
from typing import Any

from ..unit import Extraction, Modality, Origin, Relation, RelationKind, Role, Unit

_PYTHON_SUFFIXES = {".py", ".pyi"}

# Grammar names are the public lookup keys bundled by
# tree-sitter-languages==1.10.2.  Keep this map explicit: a suffix is never
# silently handed to a vaguely similar grammar.
_TREE_SITTER_GRAMMARS = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".java": "java",
    ".rs": "rust",
    ".go": "go",
    ".rb": "ruby",
    ".php": "php",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".cs": "c_sharp",
    ".sh": "bash",
    ".bash": "bash",
    ".sql": "sql",
    ".scala": "scala",
    ".pl": "perl",
    ".r": "r",
    ".ex": "elixir",
    ".exs": "elixir",
    ".hs": "haskell",
    ".m": "objc",
}

# These are source suffixes advertised by the router for which the pinned,
# offline-bundled pack has no native grammar.  Similar-looking grammars are not
# substitutes: Bash is not Zsh, Common Lisp is not Clojure, and HTML is not Vue.
_UNAVAILABLE_LANGUAGE_NAMES = {
    ".swift": "Swift",
    ".zsh": "Zsh",
    ".fish": "Fish",
    ".dart": "Dart",
    ".clj": "Clojure",
    ".cljs": "ClojureScript",
    ".fs": "F#",
    ".fsx": "F#",
    ".vb": "Visual Basic",
    ".groovy": "Groovy",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".lua": "Lua",
    ".mm": "Objective-C++",
}

_LANGUAGE_NAMES = {
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "tsx": "TSX",
    "c": "C",
    "cpp": "C++",
    "java": "Java",
    "rust": "Rust",
    "go": "Go",
    "ruby": "Ruby",
    "php": "PHP",
    "kotlin": "Kotlin",
    "c_sharp": "C#",
    "bash": "Bash",
    "sql": "SQL",
    "scala": "Scala",
    "lua": "Lua",
    "perl": "Perl",
    "r": "R",
    "elixir": "Elixir",
    "haskell": "Haskell",
    "objc": "Objective-C",
}


class InvalidSourceCode(ValueError):
    """A recognized source whose bytes cannot support exact claims."""

    def __init__(self, path: Path, detail: str) -> None:
        self.path = path
        suffix = path.suffix.lower()
        if suffix in _PYTHON_SUFFIXES:
            language = "Python"
        elif suffix in _TREE_SITTER_GRAMMARS:
            language = _LANGUAGE_NAMES[_TREE_SITTER_GRAMMARS[suffix]]
        else:
            language = suffix.lstrip(".") or "unknown"
        self.kind = f"{language} source"
        self.detail = detail
        super().__init__(f"{path.name}: invalid {self.kind}: {detail}")


class UnsupportedSourceLanguage(ValueError):
    """A source suffix for which Stage 3 has no syntax-aware native parser."""

    def __init__(
        self,
        path: Path,
        *,
        language: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.path = path
        self.suffix = path.suffix.lower()
        self.language = language or _UNAVAILABLE_LANGUAGE_NAMES.get(
            self.suffix, self.suffix.lstrip(".") or "extensionless"
        )
        self.tier = 0
        if detail is not None:
            reason = detail
        elif self.suffix == ".lua":
            reason = (
                "the pinned bundled Lua grammar rejects ordinary valid "
                "non-empty function bodies under its current ABI"
            )
        elif self.suffix in _UNAVAILABLE_LANGUAGE_NAMES:
            reason = (
                "the pinned offline tree-sitter language pack has no native "
                f"{self.language} grammar"
            )
        else:
            reason = "no native tree-sitter grammar is configured for this suffix"
        super().__init__(
            f"{path.name}: {self.language} source is tier 0, but {reason}; "
            "refusing conservative regex extraction"
        )


class MissingCodeParser(ImportError):
    """The optional, lazy non-Python parser dependency is unavailable."""

    def __init__(self, path: Path, language: str, detail: BaseException) -> None:
        super().__init__(
            f"{path.name}: {language} source parsing is not installed ({detail}); "
            "install it with: pip install 'autotldr[code]'"
        )
        # ImportError initializes its own ``path`` attribute, so our actionable
        # source path must be assigned after the base initializer.
        self.path = path
        self.language = language
        self.detail = detail


def _looks_like_objective_c(text: str) -> bool:
    markers = ("@interface", "@implementation", "@protocol", "#import")
    return any(line.lstrip().startswith(markers) for line in text.splitlines())


def _looks_like_perl(text: str) -> bool:
    lines = text.splitlines()
    first_line = lines[0].casefold() if lines else ""
    if first_line.startswith("#!") and "perl" in first_line:
        return True
    meaningful = [line.strip() for line in lines if line.strip()]
    lowered = [line.casefold() for line in meaningful]
    if any(line.startswith(("use strict;", "use warnings;")) for line in lowered):
        return True
    if any(
        marker in line and not line.startswith(("#", "%"))
        for line in meaningful
        for marker in ("my $", "my @", "my %", "our $")
    ):
        return True
    return any(line.startswith("package ") and ";" in line for line in lowered) and any(
        line.startswith("sub ") for line in lowered
    )


def extract(path: Path) -> Extraction:
    """Extract addressable symbols with a native syntax tree for the suffix."""
    suffix = path.suffix.lower()
    grammar = _TREE_SITTER_GRAMMARS.get(suffix)
    if suffix not in _PYTHON_SUFFIXES and grammar is None:
        raise UnsupportedSourceLanguage(path)

    data = path.read_bytes()
    try:
        # Decode bytes directly instead of using TextIO's universal-newline
        # translation.  Character offsets must address the exact decoded source,
        # including CRLF and mixed newline sequences.
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise InvalidSourceCode(
            path,
            f"input is not strict UTF-8 at byte {exc.start}",
        ) from exc

    source = str(path)
    index = _SourceIndex(text)
    if suffix in _PYTHON_SUFFIXES:
        return _extract_python(text, source, index)

    if suffix == ".m" and not _looks_like_objective_c(text):
        raise UnsupportedSourceLanguage(
            path,
            language="Objective-C or MATLAB",
            detail=(
                "the .m suffix is ambiguous and the source has no strong "
                "Objective-C marker (@interface, @implementation, @protocol, or #import)"
            ),
        )
    if suffix == ".pl" and not _looks_like_perl(text):
        raise UnsupportedSourceLanguage(
            path,
            language="Perl or Prolog",
            detail=(
                "the .pl suffix is ambiguous and the source has no strong Perl "
                "marker or Perl shebang"
            ),
        )

    # This is intentionally the first non-stdlib import on the non-Python path.
    # Python extraction and CLI startup do not import or initialize tree-sitter.
    try:
        from tree_sitter_languages import get_parser
    except (ImportError, OSError) as exc:  # pragma: no cover - install dependent
        raise MissingCodeParser(path, _LANGUAGE_NAMES[grammar], exc) from exc

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"Language\(path, name\) is deprecated\..*",
                category=FutureWarning,
            )
            parser = get_parser(grammar)
    except (ImportError, OSError) as exc:  # pragma: no cover - install dependent
        raise MissingCodeParser(path, _LANGUAGE_NAMES[grammar], exc) from exc

    return _extract_tree_sitter(data, source, index, grammar, parser)


class _SourceIndex:
    """Translate 1-indexed line/column coordinates into character offsets."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.lines = text.splitlines(keepends=True)
        self.starts: list[int] = []
        offset = 0
        for line in self.lines:
            self.starts.append(offset)
            offset += len(line)
        self.end = len(text)
        self.byte_starts: list[int] = []
        byte_offset = 0
        for line in self.lines:
            self.byte_starts.append(byte_offset)
            byte_offset += len(line.encode("utf-8"))
        self.byte_end = byte_offset

    def line(self, number: int) -> str:
        if not 1 <= number <= len(self.lines):
            return ""
        return self.lines[number - 1].rstrip("\r\n")

    def offset(self, line: int, column: int) -> int:
        if not self.starts:
            return 0
        line = min(max(line, 1), len(self.starts))
        return min(self.starts[line - 1] + max(column, 0), self.end)

    def ast_offset(self, line: int, byte_column: int) -> int:
        """AST columns are UTF-8 byte offsets; convert them to characters."""
        raw = self.line(line)
        prefix = raw.encode("utf-8")[:byte_column].decode("utf-8", errors="ignore")
        return self.offset(line, len(prefix))

    def line_span(self, start: int, end: int) -> tuple[int, int]:
        lo = self.offset(start, 0)
        if end < len(self.starts):
            hi = self.starts[end]
        else:
            hi = self.end
        return lo, max(lo, hi)

    def byte_offset(self, offset: int) -> int:
        """Translate a tree-sitter global UTF-8 byte offset to characters."""
        if not self.byte_starts:
            if offset == 0:
                return 0
            raise ValueError(f"tree-sitter returned out-of-range byte offset {offset}")
        if not 0 <= offset <= self.byte_end:
            raise ValueError(f"tree-sitter returned out-of-range byte offset {offset}")
        line_index = min(
            bisect_right(self.byte_starts, offset) - 1,
            len(self.byte_starts) - 1,
        )
        relative = offset - self.byte_starts[line_index]
        encoded_line = self.lines[line_index].encode("utf-8")
        try:
            prefix = encoded_line[:relative].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"tree-sitter returned non-character byte offset {offset}"
            ) from exc
        return self.starts[line_index] + len(prefix)

    def line_number(self, offset: int) -> int:
        """Return the 1-indexed source line containing a character offset."""
        if not self.starts:
            return 1
        position = min(max(offset, 0), self.end)
        return min(bisect_right(self.starts, position), len(self.starts))


def _ref(start: int, end: int) -> str:
    return f"line:{start}" if start == end else f"line:{start}-{end}"


def _origin(
    source: str,
    start_line: int,
    end_line: int,
    span: tuple[int, int],
) -> Origin:
    return Origin(source, _ref(start_line, end_line), span)


# ---------------------------------------------------------------------------
# Non-Python: grammar-backed tree-sitter symbols
# ---------------------------------------------------------------------------


_SIMPLE_DECLARATIONS: dict[str, dict[str, str]] = {
    "javascript": {
        "class_declaration": "class",
        "function_declaration": "function",
        "generator_function_declaration": "generator-function",
        "method_definition": "method",
    },
    "typescript": {
        "class_declaration": "class",
        "abstract_class_declaration": "class",
        "interface_declaration": "interface",
        "type_alias_declaration": "type-alias",
        "enum_declaration": "enum",
        "function_declaration": "function",
        "generator_function_declaration": "generator-function",
        "method_definition": "method",
        "method_signature": "method-signature",
        "abstract_method_signature": "method-signature",
        "function_signature": "function-signature",
    },
    "c": {
        "function_definition": "function",
        "struct_specifier": "struct",
        "union_specifier": "union",
        "enum_specifier": "enum",
    },
    "cpp": {
        "function_definition": "function",
        "class_specifier": "class",
        "struct_specifier": "struct",
        "union_specifier": "union",
        "enum_specifier": "enum",
        "namespace_definition": "namespace",
        "alias_declaration": "type-alias",
        "concept_definition": "concept",
    },
    "java": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "record_declaration": "record",
        "annotation_type_declaration": "annotation",
        "method_declaration": "method",
        "constructor_declaration": "constructor",
    },
    "rust": {
        "function_item": "function",
        "struct_item": "struct",
        "enum_item": "enum",
        "trait_item": "trait",
        "impl_item": "implementation",
        "type_item": "type-alias",
        "const_item": "constant",
        "static_item": "static",
        "mod_item": "module",
        "macro_definition": "macro",
    },
    "go": {
        "function_declaration": "function",
        "method_declaration": "method",
        "type_declaration": "type",
    },
    "ruby": {
        "class": "class",
        "module": "module",
        "method": "method",
        "singleton_method": "class-method",
    },
    "php": {
        "namespace_definition": "namespace",
        "class_declaration": "class",
        "interface_declaration": "interface",
        "trait_declaration": "trait",
        "enum_declaration": "enum",
        "function_definition": "function",
        "method_declaration": "method",
    },
    "kotlin": {
        "class_declaration": "class",
        "object_declaration": "object",
        "function_declaration": "function",
        "type_alias": "type-alias",
    },
    "c_sharp": {
        "namespace_declaration": "namespace",
        "file_scoped_namespace_declaration": "namespace",
        "class_declaration": "class",
        "interface_declaration": "interface",
        "struct_declaration": "struct",
        "record_declaration": "record",
        "enum_declaration": "enum",
        "delegate_declaration": "delegate",
        "method_declaration": "method",
        "constructor_declaration": "constructor",
    },
    "bash": {"function_definition": "function"},
    "sql": {
        "create_table_statement": "table",
        "create_view_statement": "view",
        "create_materialized_view_statement": "materialized-view",
        "create_function_statement": "function",
        "create_procedure_statement": "procedure",
        "create_schema_statement": "schema",
        "create_type_statement": "type",
        "create_domain_statement": "domain",
        "create_index_statement": "index",
        "create_trigger_statement": "trigger",
    },
    "scala": {
        "class_definition": "class",
        "object_definition": "object",
        "trait_definition": "trait",
        "enum_definition": "enum",
        "function_definition": "function",
        "function_declaration": "function-signature",
        "type_definition": "type-alias",
    },
    "lua": {
        "local_function_definition_statement": "function",
        "function_definition_statement": "function",
    },
    "perl": {
        "package_statement": "package",
        "function_definition": "function",
        "method_declaration": "method",
    },
    "haskell": {
        "adt": "data-type",
        "newtype": "newtype",
        "type_synonym": "type-alias",
        "class": "class",
        "signature": "signature",
        "function": "function",
    },
    "objc": {
        "class_interface": "class-interface",
        "class_implementation": "class-implementation",
        "category_interface": "category-interface",
        "category_implementation": "category-implementation",
        "protocol_declaration": "protocol",
        "method_declaration": "method",
        "method_definition": "method",
        "function_definition": "function",
    },
}
_SIMPLE_DECLARATIONS["tsx"] = _SIMPLE_DECLARATIONS["typescript"]

_IMPORT_NODES: dict[str, frozenset[str]] = {
    "javascript": frozenset({"import_statement"}),
    "typescript": frozenset({"import_statement"}),
    "tsx": frozenset({"import_statement"}),
    "c": frozenset({"preproc_include"}),
    "cpp": frozenset({"preproc_include", "import_declaration"}),
    "java": frozenset({"import_declaration"}),
    "rust": frozenset({"use_declaration", "extern_crate_declaration"}),
    "go": frozenset({"import_declaration"}),
    "php": frozenset(
        {
            "namespace_use_declaration",
            "require_expression",
            "require_once_expression",
            "include_expression",
            "include_once_expression",
        }
    ),
    "kotlin": frozenset({"import_header"}),
    "c_sharp": frozenset({"using_directive", "extern_alias_directive"}),
    "sql": frozenset(),
    "scala": frozenset({"import_declaration"}),
    "perl": frozenset({"use_no_statement", "require_statement"}),
    "haskell": frozenset({"import"}),
    "objc": frozenset({"preproc_import", "preproc_include"}),
}

_CALL_IMPORT_NAMES = {
    "ruby": frozenset({"require", "require_relative", "load"}),
    "r": frozenset({"library", "require", "require_namespace", "source"}),
    "lua": frozenset({"require", "dofile", "loadfile"}),
    "elixir": frozenset({"alias", "import", "require", "use"}),
    "bash": frozenset({"source", "."}),
}

_ELIXIR_DECLARATIONS = {
    "defmodule": "module",
    "defprotocol": "protocol",
    "defimpl": "implementation",
    "def": "function",
    "defp": "private-function",
    "defmacro": "macro",
    "defmacrop": "private-macro",
    "defguard": "guard",
    "defguardp": "private-guard",
}

_NAME_NODE_TYPES = frozenset(
    {
        "identifier",
        "field_identifier",
        "namespace_identifier",
        "package_identifier",
        "property_identifier",
        "simple_identifier",
        "type_identifier",
        "name",
        "constant",
        "variable",
        "class_name",
        "constructor",
    }
)


def _extract_tree_sitter(
    data: bytes,
    source: str,
    index: _SourceIndex,
    grammar: str,
    parser: Any,
) -> Extraction:
    tree = parser.parse(data)
    root = tree.root_node
    result = Extraction(source=source, kind="source")
    result.meta.update(
        {
            "language": grammar,
            "language_name": _LANGUAGE_NAMES[grammar],
            "parser": "tree-sitter-languages",
            "grammar": grammar,
            "lines": len(index.lines),
            "bytes": len(data),
        }
    )

    parse_errors = _append_tree_errors(result, source, index, root, grammar)

    def visit(
        node: Any,
        structure: tuple[str, ...],
        parent_id: str | None,
    ) -> None:
        child_structure = structure
        child_parent = parent_id
        declaration_kind = _declaration_kind(grammar, node, data)
        if declaration_kind is not None and not node.has_error:
            name = _symbol_name(grammar, node, data)
            bounds = _declaration_bounds(grammar, node, data)
            if name and bounds is not None:
                lo_byte, hi_byte = bounds
                content = data[lo_byte:hi_byte].decode("utf-8", errors="strict")
                span = (index.byte_offset(lo_byte), index.byte_offset(hi_byte))
                qualified = ".".join((*structure, name))
                unit = Unit(
                    source=source,
                    modality=Modality.CODE,
                    content=content,
                    origin=_tree_origin(source, index, span),
                    role=Role.UNKNOWN,
                    structure=(*structure, name),
                    salience=(
                        0.85
                        if declaration_kind
                        in {
                            "class",
                            "interface",
                            "module",
                            "namespace",
                            "struct",
                            "trait",
                            "table",
                        }
                        else 0.8
                    ),
                    meta={
                        "language": grammar,
                        "signature": True,
                        "symbol": name,
                        "qualified_name": qualified,
                        "symbol_kind": declaration_kind,
                        "native_kind": node.type,
                    },
                )
                result.units.append(unit)
                if parent_id:
                    result.relations.append(
                        Relation(
                            src=parent_id,
                            dst=unit.id,
                            kind=RelationKind.DESCRIBES,
                            evidence=f"tree-sitter {grammar} lexical nesting",
                        )
                    )
                child_structure = (*structure, name)
                child_parent = unit.id

        if _is_import_node(grammar, node, data) and not node.has_error:
            lo_byte, hi_byte = node.start_byte, node.end_byte
            if hi_byte > lo_byte:
                content = data[lo_byte:hi_byte].decode("utf-8", errors="strict")
                span = (index.byte_offset(lo_byte), index.byte_offset(hi_byte))
                reference = Unit(
                    source=source,
                    modality=Modality.REFERENCE,
                    content=content,
                    origin=_tree_origin(source, index, span),
                    role=Role.UNKNOWN,
                    structure=structure,
                    salience=0.35,
                    meta={
                        "language": grammar,
                        "ref_kind": "import",
                        "native_kind": node.type,
                    },
                )
                result.units.append(reference)
                if parent_id:
                    result.relations.append(
                        Relation(
                            src=parent_id,
                            dst=reference.id,
                            kind=RelationKind.REFERENCES,
                            evidence=f"tree-sitter {grammar} import syntax",
                        )
                    )

        for child in node.named_children:
            visit(child, child_structure, child_parent)

    try:
        visit(root, (), None)
    except RecursionError:
        result.add_gap(
            f"{_LANGUAGE_NAMES[grammar]} syntax tree nesting is too deep to walk safely"
        )

    if not result.units:
        result.add_gap(
            f"no addressable {_LANGUAGE_NAMES[grammar]} API declarations or imports were found"
        )
    result.meta["parse_errors"] = parse_errors
    result.meta["symbols"] = sum(
        1 for unit in result.units if unit.meta.get("signature") is True
    )
    result.meta["imports"] = sum(
        1 for unit in result.units if unit.meta.get("ref_kind") == "import"
    )
    return result


def _append_tree_errors(
    result: Extraction,
    source: str,
    index: _SourceIndex,
    root: Any,
    grammar: str,
) -> int:
    if not root.has_error:
        return 0
    count = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            span = (
                index.byte_offset(node.start_byte),
                index.byte_offset(node.end_byte),
            )
            line = index.line_number(span[0])
            detail = f"missing {node.type}" if node.is_missing else "ERROR node"
            result.add_gap(
                f"tree-sitter {_LANGUAGE_NAMES[grammar]} parse gap at line {line}: {detail}",
                origin=_tree_origin(source, index, span),
            )
            count += 1
            if node.type == "ERROR":
                continue
        # Missing punctuation nodes are often anonymous, so diagnostics must
        # walk every child even though semantic extraction walks named nodes.
        stack.extend(reversed(node.children))
    if not count:
        result.add_gap(
            f"tree-sitter {_LANGUAGE_NAMES[grammar]} reported an unlocated parse gap"
        )
        return 1
    return count


def _tree_origin(
    source: str,
    index: _SourceIndex,
    span: tuple[int, int],
) -> Origin:
    lo, hi = span
    start_line = index.line_number(lo)
    end_line = index.line_number(max(lo, hi - 1))
    line_ref = _ref(start_line, end_line)
    # Global character offsets disambiguate multiple declarations on one line
    # and make the exact slice visible in the native reference as well as in
    # Origin.char_span.
    return Origin(source, f"{line_ref}#char:{lo}-{hi}", span)


def _declaration_kind(grammar: str, node: Any, data: bytes) -> str | None:
    if grammar in {"c", "cpp"} and node.type in {
        "class_specifier",
        "struct_specifier",
        "union_specifier",
        "enum_specifier",
    }:
        body = node.child_by_field_name("body")
        parent_type = node.parent.type if node.parent is not None else ""
        if body is None and parent_type not in {"declaration", "type_definition"}:
            # A type mention such as ``struct Relay *value`` is not a new
            # declaration merely because its grammar node is a specifier.
            return None

    kind = _SIMPLE_DECLARATIONS.get(grammar, {}).get(node.type)
    if kind is not None:
        return kind

    if grammar in {"javascript", "typescript", "tsx"} and node.type in {
        "lexical_declaration",
        "variable_declaration",
    }:
        declarators = [
            child for child in node.named_children if child.type == "variable_declarator"
        ]
        if len(declarators) == 1:
            value = declarators[0].child_by_field_name("value")
            if value is not None and value.type in {
                "arrow_function",
                "function_expression",
                "generator_function",
            }:
                return "function"

    if grammar in {"c", "cpp", "objc"} and node.type in {
        "declaration",
        "field_declaration",
    }:
        if _first_descendant(node, {"function_declarator"}) is not None:
            return "function-signature"

    if grammar == "r" and node.type in {
        "left_assignment",
        "right_assignment",
        "equals_assignment",
    }:
        value = node.child_by_field_name("value")
        if value is not None and value.type == "function_definition":
            return "function"

    if grammar == "elixir" and node.type == "call":
        return _ELIXIR_DECLARATIONS.get(_call_target(node, data))
    return None


def _is_import_node(grammar: str, node: Any, data: bytes) -> bool:
    if node.type in _IMPORT_NODES.get(grammar, frozenset()):
        return True
    names = _CALL_IMPORT_NAMES.get(grammar)
    return names is not None and _call_target(node, data) in names


def _call_target(node: Any, data: bytes) -> str:
    for field in ("function", "method", "target", "name"):
        child = node.child_by_field_name(field)
        if child is not None:
            return _node_text(child, data).strip().casefold()
    if node.type == "command":
        child = _first_descendant(node, {"command_name"})
        if child is not None:
            return _node_text(child, data).strip().casefold()
    return ""


def _symbol_name(grammar: str, node: Any, data: bytes) -> str | None:
    if grammar in {"javascript", "typescript", "tsx"} and node.type in {
        "lexical_declaration",
        "variable_declaration",
    }:
        declarator = _first_descendant(node, {"variable_declarator"})
        name = declarator.child_by_field_name("name") if declarator is not None else None
        return _clean_name(name, data)

    if grammar in {"c", "cpp", "objc"} and node.type in {
        "declaration",
        "field_declaration",
        "function_definition",
    }:
        declarator = node.child_by_field_name("declarator")
        name = _first_descendant(declarator, _NAME_NODE_TYPES)
        return _clean_name(name, data)

    if grammar == "go" and node.type == "type_declaration":
        spec = _first_descendant(node, {"type_spec", "type_alias"})
        name = spec.child_by_field_name("name") if spec is not None else None
        return _clean_name(name, data)

    if grammar == "rust" and node.type == "impl_item":
        implemented = node.child_by_field_name("type")
        value = _clean_name(implemented, data)
        return f"impl {value}" if value else None

    if grammar == "r" and node.type in {
        "left_assignment",
        "right_assignment",
        "equals_assignment",
    }:
        return _clean_name(node.child_by_field_name("name"), data)

    if grammar == "elixir" and node.type == "call":
        arguments = _first_descendant(node, {"arguments"})
        if arguments is None or not arguments.named_children:
            return None
        head = arguments.named_children[0]
        if head.type == "call":
            target = head.child_by_field_name("target")
            return _clean_name(target, data)
        return _clean_name(head, data)

    direct = node.child_by_field_name("name")
    if direct is not None:
        return _clean_name(direct, data)

    if grammar == "objc" and node.type in {
        "method_declaration",
        "method_definition",
    }:
        selector = node.child_by_field_name("selector")
        keyword = (
            selector
            if selector is not None and selector.type == "identifier"
            else _first_descendant(selector, {"identifier"})
        )
        return _clean_name(keyword, data)

    if grammar == "sql":
        return _clean_name(_first_descendant(node, {"identifier"}), data)

    declarator = node.child_by_field_name("declarator")
    if declarator is not None:
        return _clean_name(_first_descendant(declarator, _NAME_NODE_TYPES), data)

    return _clean_name(_first_descendant(node, _NAME_NODE_TYPES), data)


def _declaration_bounds(
    grammar: str,
    node: Any,
    data: bytes,
) -> tuple[int, int] | None:
    lo = node.start_byte
    parent = node.parent
    if (
        grammar in {"javascript", "typescript", "tsx"}
        and parent is not None
        and parent.type == "export_statement"
    ):
        lo = parent.start_byte

    body = _declaration_body(grammar, node)
    hi = body.start_byte if body is not None else node.end_byte
    while hi > lo and data[hi - 1] in b" \t\r\n":
        hi -= 1
    if grammar == "elixir" and hi > lo and data[hi - 1 : hi] == b",":
        hi -= 1
        while hi > lo and data[hi - 1] in b" \t":
            hi -= 1
    return (lo, hi) if hi > lo else None


def _declaration_body(grammar: str, node: Any) -> Any | None:
    direct = node.child_by_field_name("body")
    if direct is not None:
        return direct

    if grammar in {"javascript", "typescript", "tsx"} and node.type in {
        "lexical_declaration",
        "variable_declaration",
    }:
        declarator = _first_descendant(node, {"variable_declarator"})
        value = declarator.child_by_field_name("value") if declarator is not None else None
        return value.child_by_field_name("body") if value is not None else None

    if grammar == "r":
        function = node.child_by_field_name("value")
        if function is not None and function.type == "function_definition":
            body = function.child_by_field_name("body")
            return body or _first_descendant(function, {"brace_list"})

    if grammar == "haskell":
        if node.type == "function":
            return node.child_by_field_name("rhs")
        if node.type == "class":
            return _first_descendant(node, {"class_body"})
        if node.type in {"adt", "newtype"}:
            return node.child_by_field_name("constructors")

    if grammar == "elixir" and node.type == "call":
        for child_type in ("do_block", "keywords"):
            child = _first_descendant(node, {child_type})
            if child is not None:
                return child
    return None


def _first_descendant(node: Any | None, types: set[str] | frozenset[str]) -> Any | None:
    if node is None:
        return None
    stack = list(reversed(node.named_children))
    while stack:
        child = stack.pop()
        if child.type in types:
            return child
        stack.extend(reversed(child.named_children))
    return None


def _node_text(node: Any, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8", errors="strict")


def _clean_name(node: Any | None, data: bytes) -> str | None:
    if node is None:
        return None
    value = _node_text(node, data).strip()
    return value or None


# ---------------------------------------------------------------------------
# Python: syntax-tree-backed symbols
# ---------------------------------------------------------------------------


def _extract_python(text: str, source: str, index: _SourceIndex) -> Extraction:
    result = Extraction(source=source, kind="python")
    result.meta.update(
        {
            "language": "python",
            "parser": "stdlib-ast",
            "lines": len(index.lines),
            "bytes": len(text.encode("utf-8")),
        }
    )

    try:
        tree = ast.parse(text, filename=source, type_comments=True)
    except (SyntaxError, ValueError) as exc:
        line = getattr(exc, "lineno", None)
        where = f" at line {line}" if line else ""
        detail = exc.msg if isinstance(exc, SyntaxError) else exc
        if isinstance(line, int) and line > 0:
            result.add_gap(
                f"python syntax error{where}: {detail}",
                origin=_origin(source, line, line, index.line_span(line, line)),
            )
        else:
            result.add_gap(f"python syntax error{where}: {detail}")
        result.meta["parse_error"] = str(exc)
        return result

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError) as exc:
        # A valid AST should normally imply a valid token stream.  Keep the AST
        # result safe even for parser edge cases: docstrings and imports remain
        # provable, while signatures whose colon cannot be located are skipped.
        tokens = []
        result.add_gap(f"could not locate exact Python signature spans: {exc}")

    module_doc = _docstring_unit(tree, source, index, (), "module", "<module>")
    if module_doc is not None:
        result.units.append(module_doc)

    visitor = _PythonVisitor(result, text, source, index, tokens)
    for statement in tree.body:
        visitor.visit(statement)

    if not result.units:
        result.add_gap(
            "no addressable Python symbols, docstrings, or imports were found"
        )
    result.meta["symbols"] = sum(
        1 for unit in result.units if unit.meta.get("signature") is True
    )
    return result


class _PythonVisitor(ast.NodeVisitor):
    def __init__(
        self,
        result: Extraction,
        text: str,
        source: str,
        index: _SourceIndex,
        tokens: list[tokenize.TokenInfo],
    ) -> None:
        self.result = result
        self.text = text
        self.source = source
        self.index = index
        self.tokens = tokens
        self.names: list[str] = []
        self.parent_units: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._visit_symbol(node, "class")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_symbol(node, "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_symbol(node, "async-function")

    def _visit_symbol(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        symbol_kind: str,
    ) -> None:
        structure = tuple((*self.names, node.name))
        qualified = ".".join(structure)
        signature = _signature_span(node, self.text, self.index, self.tokens)
        signature_unit: Unit | None = None

        if signature is None:
            self.result.add_gap(
                f"could not address the exact signature for {qualified} at line {node.lineno}",
                origin=_origin(
                    self.source,
                    node.lineno,
                    node.lineno,
                    self.index.line_span(node.lineno, node.lineno),
                ),
            )
        else:
            content, start_line, end_line, char_span = signature
            signature_unit = Unit(
                source=self.source,
                modality=Modality.CODE,
                content=content,
                origin=_origin(self.source, start_line, end_line, char_span),
                role=Role.UNKNOWN,
                structure=structure,
                salience=0.85 if symbol_kind == "class" else 0.8,
                meta={
                    "language": "python",
                    "signature": True,
                    "symbol": node.name,
                    "qualified_name": qualified,
                    "symbol_kind": symbol_kind,
                },
            )
            self.result.units.append(signature_unit)
            if self.parent_units:
                self.result.relations.append(
                    Relation(
                        src=self.parent_units[-1],
                        dst=signature_unit.id,
                        kind=RelationKind.DESCRIBES,
                        evidence="AST lexical nesting",
                    )
                )

        doc = _docstring_unit(
            node, self.source, self.index, structure, symbol_kind, qualified
        )
        if doc is not None:
            self.result.units.append(doc)
            if signature_unit is not None:
                self.result.relations.append(
                    Relation(
                        src=signature_unit.id,
                        dst=doc.id,
                        kind=RelationKind.DESCRIBES,
                        evidence="AST docstring ownership",
                    )
                )

        self.names.append(node.name)
        if signature_unit is not None:
            self.parent_units.append(signature_unit.id)
        for statement in node.body:
            self.visit(statement)
        if signature_unit is not None:
            self.parent_units.pop()
        self.names.pop()

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            self._add_import(node, alias.name, alias.asname, "import")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        target = f"{'.' * node.level}{node.module or ''}"
        imported = [alias.name for alias in node.names]
        self._add_import(node, target, None, "from-import", imported)

    def _add_import(
        self,
        node: ast.Import | ast.ImportFrom,
        target: str,
        alias: str | None,
        import_kind: str,
        imported: list[str] | None = None,
    ) -> None:
        if not target:
            return
        lo = self.index.ast_offset(node.lineno, node.col_offset)
        hi = self.index.ast_offset(node.end_lineno or node.lineno, node.end_col_offset or 0)
        unit = Unit(
            source=self.source,
            modality=Modality.REFERENCE,
            content=target,
            origin=_origin(
                self.source,
                node.lineno,
                node.end_lineno or node.lineno,
                (lo, hi),
            ),
            role=Role.UNKNOWN,
            structure=tuple(self.names),
            salience=0.35,
            meta={
                "target": target,
                "alias": alias,
                "imported": imported,
                "ref_kind": import_kind,
                "language": "python",
            },
        )
        self.result.units.append(unit)


def _signature_span(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    text: str,
    index: _SourceIndex,
    tokens: list[tokenize.TokenInfo],
) -> tuple[str, int, int, tuple[int, int]] | None:
    if not tokens:
        return None

    keyword = "async" if isinstance(node, ast.AsyncFunctionDef) else (
        "class" if isinstance(node, ast.ClassDef) else "def"
    )
    ast_column = len(
        index.line(node.lineno)
        .encode("utf-8")[: node.col_offset]
        .decode("utf-8", errors="ignore")
    )
    floor = (node.lineno, ast_column)
    start_index: int | None = None
    for pos, token in enumerate(tokens):
        if token.start < floor:
            continue
        if token.type == tokenize.NAME and token.string == keyword:
            start_index = pos
            break
        if token.start[0] > node.lineno:
            break
    if start_index is None:
        return None

    depth = 0
    start_token = tokens[start_index]
    for token in tokens[start_index:]:
        if token.type != tokenize.OP:
            continue
        if token.string in "([{":
            depth += 1
        elif token.string in ")]}":
            depth = max(0, depth - 1)
        elif token.string == ":" and depth == 0:
            lo = index.offset(*start_token.start)
            hi = index.offset(*token.end)
            return text[lo:hi], start_token.start[0], token.end[0], (lo, hi)
    return None


def _docstring_unit(
    owner: ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    source: str,
    index: _SourceIndex,
    structure: tuple[str, ...],
    owner_kind: str,
    qualified_name: str,
) -> Unit | None:
    if not owner.body:
        return None
    statement = owner.body[0]
    if not (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    ):
        return None
    content = ast.get_docstring(owner, clean=False)
    if not content:
        return None
    value = statement.value
    end_line = value.end_lineno or value.lineno
    lo = index.ast_offset(value.lineno, value.col_offset)
    hi = index.ast_offset(end_line, value.end_col_offset or 0)
    return Unit(
        source=source,
        modality=Modality.PROSE,
        content=content,
        origin=_origin(source, value.lineno, end_line, (lo, hi)),
        role=Role.UNKNOWN,
        structure=structure,
        salience=0.65,
        meta={
            "docstring": True,
            "owner_kind": owner_kind,
            "qualified_name": qualified_name,
            "language": "python",
        },
    )
