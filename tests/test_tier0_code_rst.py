"""Semantic contracts for syntax-backed Tier-0 code and native rST."""

from __future__ import annotations

import builtins
import warnings

import pytest

from autotldr.extract.code import (
    InvalidSourceCode,
    MissingCodeParser,
    UnsupportedSourceLanguage,
    _SourceIndex,
    _TREE_SITTER_GRAMMARS,
    extract as extract_code,
)
from autotldr.extract.rst import InvalidRst, extract as extract_rst
from autotldr.unit import Modality, Role


PYTHON_SOURCE = '''\
"""Module documentation.
Second module line.
"""

import relay.transport as transport
from .helpers import ready

class Relay(BaseRelay):
    """Coordinates relay state."""

    async def start(
        self,
        attempts: int = 2,
    ) -> bool:
        """Start the relay."""
        return True

    def stop(self) -> None:
        pass

def status() -> str:
    return "ok"
'''


def test_python_ast_emits_exact_symbols_docstrings_and_imports(tmp_path):
    path = tmp_path / "relay.py"
    path.write_text(PYTHON_SOURCE, encoding="utf-8")

    result = extract_code(path)
    signatures = {
        unit.meta["qualified_name"]: unit
        for unit in result.units
        if unit.meta.get("signature")
    }

    assert result.kind == "python"
    assert result.meta["parser"] == "stdlib-ast"
    assert set(signatures) == {"Relay", "Relay.start", "Relay.stop", "status"}
    assert signatures["Relay"].content == "class Relay(BaseRelay):"
    assert signatures["Relay"].origin.ref == "line:8"
    assert signatures["Relay.start"].content == (
        "async def start(\n"
        "        self,\n"
        "        attempts: int = 2,\n"
        "    ) -> bool:"
    )
    assert signatures["Relay.start"].origin.ref == "line:11-14"
    assert signatures["Relay.start"].structure == ("Relay", "start")
    assert signatures["Relay.stop"].structure == ("Relay", "stop")
    assert signatures["status"].structure == ("status",)
    assert all(unit.role is Role.UNKNOWN for unit in result.units)
    assert not any(unit.content.strip().startswith("return ") for unit in result.units)

    docstrings = [unit for unit in result.units if unit.meta.get("docstring")]
    assert {unit.meta["qualified_name"] for unit in docstrings} == {
        "<module>",
        "Relay",
        "Relay.start",
    }
    assert any(unit.content == "Coordinates relay state." for unit in docstrings)
    assert any(unit.structure == ("Relay", "start") for unit in docstrings)

    references = {
        unit.content for unit in result.units if unit.modality is Modality.REFERENCE
    }
    assert references == {"relay.transport", ".helpers"}


def test_python_units_have_verifiable_spans_and_resolved_nesting_relations(tmp_path):
    path = tmp_path / "relay.py"
    path.write_text(PYTHON_SOURCE, encoding="utf-8")
    source = path.read_text(encoding="utf-8")

    result = extract_code(path)
    ids = {unit.id for unit in result.units}
    for unit in result.units:
        assert unit.origin.source == str(path)
        assert unit.origin.ref.startswith("line:")
        assert unit.origin.char_span is not None
        lo, hi = unit.origin.char_span
        if unit.modality is Modality.CODE:
            assert unit.content == source[lo:hi]
            continue
        excerpt = " ".join(source[lo:hi].split())
        content = " ".join(unit.content.split())
        assert content in excerpt

    assert result.relations
    assert all(relation.src in ids and relation.dst in ids for relation in result.relations)
    by_id = {unit.id: unit for unit in result.units}
    assert any(
        by_id[relation.src].meta.get("qualified_name") == "Relay"
        and by_id[relation.dst].meta.get("qualified_name") == "Relay.start"
        and relation.evidence == "AST lexical nesting"
        for relation in result.relations
    )


def test_malformed_python_declines_to_invent_partial_symbols(tmp_path):
    path = tmp_path / "broken.py"
    path.write_text(
        '"""This prefix parses alone."""\n\ndef broken(:\n    return 1\n',
        encoding="utf-8",
    )

    result = extract_code(path)

    assert result.units == []
    assert any("syntax error" in gap and "line 3" in gap for gap in result.gaps)
    assert result.meta["parse_error"]


TREE_SITTER_SOURCES = {
    "javascript": (
        'import client from "./client.js";\n'
        "export function start(id) { return client(id); }\n"
    ),
    "typescript": (
        'import type { Client } from "./client";\n'
        "export interface Relay { start(id: string): Promise<void>; }\n"
    ),
    "tsx": (
        'import React from "react";\n'
        "export function View() { return <div />; }\n"
    ),
    "c": (
        "#include <stdio.h>\n"
        "struct Relay { int id; };\n"
        "int start(int id) { return id; }\n"
    ),
    "cpp": (
        "#include <vector>\n"
        "namespace relay { class Cache { public: int get(int id) { return id; } }; }\n"
    ),
    "java": (
        "import java.util.List;\n"
        "public class Relay { public int start(int id) { return id; } }\n"
    ),
    "rust": (
        "use std::io::Read;\n"
        "pub struct Relay { id: u64 }\n"
        "pub fn start(id: u64) -> u64 { id }\n"
    ),
    "go": (
        'package relay\nimport "fmt"\n'
        "type Relay struct { ID int }\n"
        "func Start(id int) int { return id }\n"
    ),
    "ruby": (
        'require "json"\n'
        "class Relay\n def start(id)\n  id\n end\nend\n"
    ),
    "php": (
        "<?php\nuse Vendor\\Client;\n"
        "class Relay { public function start(int $id): int { return $id; } }\n"
    ),
    "kotlin": (
        "import kotlin.io.path.Path\n"
        "data class Relay(val id: Int)\n"
        "fun start(id: Int): Int { return id }\n"
    ),
    "c_sharp": (
        "using System.Text;\n"
        "public class Relay { public int Start(int id) { return id; } }\n"
    ),
    "bash": "source ./helpers.sh\nstart() { echo hi; }\n",
    "sql": "CREATE TABLE users (id INTEGER PRIMARY KEY);\n",
    "scala": (
        "import scala.concurrent.Future\n"
        "trait Relay { def start(id: Int): Int }\n"
    ),
    "lua": "function start() end\n",
    "perl": "use strict;\nsub start { return 1; }\n",
    "r": "library(stats)\nstart <- function(id) { id }\n",
    "elixir": (
        "defmodule Relay do\n import Enum\n def start(id), do: id\nend\n"
    ),
    "haskell": (
        "module Relay where\nimport Data.Text\n"
        "start :: Int -> Int\nstart value = value\n"
    ),
    "objc": (
        "#import <Foundation/Foundation.h>\n"
        "@interface Relay : NSObject\n- (BOOL)start;\n@end\n"
    ),
}


@pytest.mark.parametrize(
    ("filename", "body", "expected_kinds"),
    [
        (
            "relay.ts",
            'import { Client } from "./client";\r\n'
            "export interface Relay { start(id: string): Promise<void>; }\r\n"
            "export const load = (id: string): Client => make(id);\r\n",
            {"interface", "method-signature", "function"},
        ),
        (
            "relay.rs",
            "use std::io::Read;\n"
            "pub struct Relay { id: u64 }\n"
            "impl Relay { pub fn start(&self) -> bool { true } }\n",
            {"struct", "implementation", "function"},
        ),
        (
            "relay.hs",
            "import qualified Data.Text as T\n"
            "data Relay = Relay Int\n"
            "start :: Relay -> IO Bool\n"
            "start relay = pure True\n",
            {"data-type", "signature", "function"},
        ),
        (
            "analysis.R",
            'source("helpers.R")\nmeasure <- function(samples = 40) { median(samples) }\n',
            {"function"},
        ),
        (
            "schema.sql",
            "CREATE TABLE users (id INTEGER PRIMARY KEY);\n"
            "CREATE FUNCTION add_one(x integer) RETURNS integer "
            "AS $$ SELECT x + 1 $$ LANGUAGE SQL;\n",
            {"table", "function"},
        ),
    ],
)
def test_tree_sitter_recovers_native_api_kinds_with_exact_source_slices(
    tmp_path,
    filename,
    body,
    expected_kinds,
):
    path = tmp_path / filename
    raw = body.encode("utf-8")
    path.write_bytes(raw)

    result = extract_code(path)
    kinds = {
        unit.meta["symbol_kind"]
        for unit in result.units
        if unit.meta.get("signature") is True
    }

    assert result.kind == "source"
    assert result.meta["parser"] == "tree-sitter-languages"
    assert result.meta["parse_errors"] == 0
    assert expected_kinds <= kinds
    assert len({unit.id for unit in result.units}) == len(result.units)
    for unit in result.units:
        lo, hi = unit.origin.char_span
        assert unit.origin.ref.startswith("line:")
        assert unit.content == body[lo:hi]
        byte_lo = len(body[:lo].encode("utf-8"))
        byte_hi = len(body[:hi].encode("utf-8"))
        assert unit.content.encode("utf-8") == raw[byte_lo:byte_hi]


def test_sql_native_kinds_never_label_a_table_as_a_function(tmp_path):
    path = tmp_path / "schema.sql"
    path.write_text(
        "CREATE TABLE users (id INTEGER PRIMARY KEY);\n"
        "CREATE FUNCTION add_one(x integer) RETURNS integer "
        "AS $$ SELECT x + 1 $$ LANGUAGE SQL;\n",
        encoding="utf-8",
    )

    result = extract_code(path)
    by_symbol = {
        unit.meta["symbol"]: unit
        for unit in result.units
        if unit.meta.get("signature") is True
    }

    assert by_symbol["users"].meta["native_kind"] == "create_table_statement"
    assert by_symbol["users"].meta["symbol_kind"] == "table"
    assert by_symbol["add_one"].meta["native_kind"] == "create_function_statement"
    assert by_symbol["add_one"].meta["symbol_kind"] == "function"


def test_same_line_duplicate_signatures_keep_unique_exact_ids(tmp_path):
    path = tmp_path / "duplicate.ts"
    source = "interface Relay { start(): void; start(): void; }\n"
    path.write_text(source, encoding="utf-8")

    result = extract_code(path)
    methods = [
        unit
        for unit in result.units
        if unit.meta.get("symbol_kind") == "method-signature"
    ]

    assert [unit.content for unit in methods] == ["start(): void", "start(): void"]
    assert len({unit.origin.ref for unit in methods}) == 2
    assert len({unit.id for unit in methods}) == 2
    for unit in methods:
        lo, hi = unit.origin.char_span
        assert unit.content == source[lo:hi]


@pytest.mark.parametrize(("suffix", "grammar"), sorted(_TREE_SITTER_GRAMMARS.items()))
def test_every_bundled_source_grammar_emits_exact_unique_units(
    tmp_path,
    suffix,
    grammar,
):
    path = tmp_path / f"sample{suffix}"
    source = TREE_SITTER_SOURCES[grammar]
    path.write_bytes(source.encode("utf-8"))

    result = extract_code(path)

    assert result.meta["grammar"] == grammar
    assert result.meta["parse_errors"] == 0
    assert result.units
    assert len({unit.id for unit in result.units}) == len(result.units)
    for unit in result.units:
        lo, hi = unit.origin.char_span
        assert unit.content == source[lo:hi]


@pytest.mark.parametrize(
    ("filename", "body", "language"),
    [
        ("relay.swift", "func start() {}\n", "Swift"),
        ("relay.zsh", "function start { true }\n", "Zsh"),
        ("relay.fish", "function start; end\n", "Fish"),
        ("relay.dart", "void start() {}\n", "Dart"),
        ("relay.clj", "(defn start [] true)\n", "Clojure"),
        ("relay.cljs", "(defn start [] true)\n", "ClojureScript"),
        ("relay.fs", "let start () = true\n", "F#"),
        ("relay.fsx", "let start () = true\n", "F#"),
        ("relay.vb", "Sub Start()\nEnd Sub\n", "Visual Basic"),
        ("relay.groovy", "def start() { true }\n", "Groovy"),
        ("relay.vue", "<template><div /></template>\n", "Vue"),
        ("relay.svelte", "<script>export let id;</script>\n", "Svelte"),
        ("relay.lua", "function start()\n  return true\nend\n", "Lua"),
        ("relay.mm", "class Relay { public: void start(); };\n", "Objective-C++"),
        (
            "model.m",
            "function value = relay(input)\nvalue = input;\nend\n",
            "Objective-C or MATLAB",
        ),
        ("rules.pl", "ancestor(X, Y) :- parent(X, Y).\n", "Perl or Prolog"),
    ],
)
def test_unavailable_or_ambiguous_source_is_declined_by_name(
    tmp_path,
    filename,
    body,
    language,
):
    path = tmp_path / filename
    path.write_text(body, encoding="utf-8")

    with pytest.raises(UnsupportedSourceLanguage) as raised:
        extract_code(path)

    assert raised.value.path == path
    assert raised.value.suffix == path.suffix
    assert raised.value.language == language
    assert raised.value.tier == 0
    assert "refusing conservative regex extraction" in str(raised.value)


def test_tree_sitter_parse_gaps_have_exact_native_origins(tmp_path):
    path = tmp_path / "broken.ts"
    source = "export function broken(id: string { return id; }\n"
    path.write_text(source, encoding="utf-8")

    result = extract_code(path)
    parse_gaps = [gap for gap in result.gaps if "parse gap" in gap]

    assert result.meta["parse_errors"] >= 1
    assert parse_gaps
    assert result.units == []
    for gap in parse_gaps:
        assert gap.origin.source == str(path)
        assert gap.origin.ref.startswith("line:")
        assert gap.origin.char_span is not None
        lo, hi = gap.origin.char_span
        assert 0 <= lo <= hi <= len(source)


def test_non_python_rejects_non_utf8_before_loading_the_parser(tmp_path):
    path = tmp_path / "broken.ts"
    path.write_bytes(b"export function valid() {}\n" + bytes([0xFF]))

    with pytest.raises(InvalidSourceCode) as raised:
        extract_code(path)

    assert raised.value.path == path
    assert raised.value.kind == "TypeScript source"
    assert "strict UTF-8" in str(raised.value)


def test_missing_tree_sitter_dependency_names_the_code_extra(tmp_path, monkeypatch):
    path = tmp_path / "relay.js"
    path.write_text("export function start() {}\n", encoding="utf-8")
    real_import = builtins.__import__

    def missing_tree_sitter(name, *args, **kwargs):
        if name == "tree_sitter_languages":
            raise ModuleNotFoundError("tree_sitter_languages is unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_tree_sitter)
    with pytest.raises(MissingCodeParser) as raised:
        extract_code(path)

    assert raised.value.path == path
    assert raised.value.language == "JavaScript"
    assert "autotldr[code]" in str(raised.value)


def test_tree_sitter_compatibility_warning_is_narrowly_suppressed(tmp_path):
    path = tmp_path / "relay.js"
    path.write_text("export function start() {}\n", encoding="utf-8")

    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        extract_code(path)

    assert not any("Language(path, name) is deprecated" in str(item.message) for item in seen)


def test_tree_sitter_byte_index_uses_line_scale_memory_and_exact_utf8_offsets():
    source = ("café" * 25_000) + "\r\nnext\n"
    index = _SourceIndex(source)

    assert len(index.byte_starts) == len(source.splitlines(keepends=True))
    assert not hasattr(index, "byte_to_char")
    for char_offset in (0, 1, 25_000, 100_000, len(source)):
        byte_offset = len(source[:char_offset].encode("utf-8"))
        assert index.byte_offset(byte_offset) == char_offset


def test_python_code_claim_round_trips_exact_crlf_characters_and_bytes(tmp_path):
    path = tmp_path / "unicode.py"
    raw = (
        'def café(\r\n'
        '    value: str = "déjà",  \r\n'
        ') -> str:\r\n'
        '    return value\r\n'
    ).encode("utf-8")
    path.write_bytes(raw)
    source = raw.decode("utf-8")

    result = extract_code(path)
    code = [unit for unit in result.units if unit.modality is Modality.CODE]

    assert len(code) == 1
    assert code[0].content == (
        'def café(\r\n'
        '    value: str = "déjà",  \r\n'
        ') -> str:'
    )
    lo, hi = code[0].origin.char_span
    assert code[0].content == source[lo:hi]
    byte_lo = len(source[:lo].encode("utf-8"))
    byte_hi = len(source[:hi].encode("utf-8"))
    assert code[0].content.encode("utf-8") == raw[byte_lo:byte_hi]
    assert result.meta["bytes"] == len(raw)


def test_python_rejects_non_utf8_bytes_before_emitting_claims(tmp_path):
    path = tmp_path / "broken.py"
    path.write_bytes(b"def valid():\n    return 1\n\xff")

    with pytest.raises(InvalidSourceCode) as raised:
        extract_code(path)

    assert raised.value.path == path
    assert raised.value.kind == "Python source"
    assert "strict UTF-8" in str(raised.value)
    assert "byte 26" in str(raised.value)


RST_SOURCE = """\
Relay Guide
===========

Overview
--------

The relay coordinates retries
across worker nodes. See `remote guide <https://example.test/relay>`_.

.. note::
   Keep the manual fallback enabled.

.. include:: shared/setup.rst

Example
-------

.. code-block:: python
   :linenos:

   def ping():
       return True

A shell literal follows::

   relay --check
   relay --verbose

1. Warm the cache
2. Start the relay
"""


def test_rst_recovers_first_seen_adornment_hierarchy_and_paragraphs(tmp_path):
    path = tmp_path / "guide.rst"
    path.write_text(RST_SOURCE, encoding="utf-8")

    result = extract_rst(path)
    headings = [unit for unit in result.units if unit.meta.get("heading")]

    assert result.kind == "rst"
    assert [(unit.content, unit.meta["heading_level"]) for unit in headings] == [
        ("Relay Guide", 1),
        ("Overview", 2),
        ("Example", 2),
    ]
    assert headings[0].structure == ("Relay Guide",)
    assert headings[1].structure == ("Relay Guide", "Overview")
    assert headings[2].structure == ("Relay Guide", "Example")
    assert any(
        unit.content.startswith("The relay coordinates retries across worker nodes.")
        and unit.structure == ("Relay Guide", "Overview")
        for unit in result.units
    )
    assert all(unit.role is Role.UNKNOWN for unit in result.units)


def test_rst_preserves_directives_literal_blocks_lists_and_references(tmp_path):
    path = tmp_path / "guide.rst"
    path.write_text(RST_SOURCE, encoding="utf-8")

    result = extract_rst(path)
    directives = [unit for unit in result.units if unit.meta.get("directive")]
    code = [unit for unit in result.units if unit.modality is Modality.CODE]
    lists = [unit for unit in result.units if unit.meta.get("list")]
    refs = {
        unit.content for unit in result.units if unit.modality is Modality.REFERENCE
    }

    assert {unit.meta["directive"] for unit in directives} >= {
        "note",
        "include",
        "code-block",
    }
    assert any("manual fallback" in unit.content for unit in directives)
    assert {unit.content for unit in code} == {
        "   def ping():\n       return True\n",
        "   relay --check\n   relay --verbose\n",
    }
    assert all(unit.meta["literal_block"] is True for unit in code)
    assert len(lists) == 1
    assert lists[0].content == "1. Warm the cache\n2. Start the relay"
    assert lists[0].meta["procedure_cue"] is True
    assert refs == {"https://example.test/relay", "shared/setup.rst"}


def test_rst_every_unit_is_addressable_and_relations_resolve(tmp_path):
    path = tmp_path / "guide.rst"
    path.write_text(RST_SOURCE, encoding="utf-8")
    source = path.read_text(encoding="utf-8")

    result = extract_rst(path)
    ids = {unit.id for unit in result.units}
    for unit in result.units:
        assert unit.origin.source == str(path)
        assert unit.origin.ref.startswith("line:")
        assert unit.origin.char_span is not None
        lo, hi = unit.origin.char_span
        if unit.modality is Modality.CODE:
            assert unit.content == source[lo:hi]
            continue
        excerpt = " ".join(source[lo:hi].split())
        content = " ".join(unit.content.split())
        assert content in excerpt
    assert all(relation.src in ids and relation.dst in ids for relation in result.relations)


def test_rst_malformed_adornment_and_empty_code_directive_are_explicit(tmp_path):
    path = tmp_path / "broken.rst"
    path.write_text(
        "====\n\n.. code-block:: python\n\nnot indented\n",
        encoding="utf-8",
    )

    result = extract_rst(path)

    assert not any(unit.meta.get("heading") for unit in result.units)
    assert not any(unit.modality is Modality.CODE for unit in result.units)
    assert any("orphan section adornment at line 1" in gap for gap in result.gaps)
    assert any("has no indented code body" in gap for gap in result.gaps)
    assert any(unit.content == "not indented" for unit in result.units)


def test_empty_rst_reports_absence(tmp_path):
    path = tmp_path / "empty.rst"
    path.write_text("", encoding="utf-8")

    result = extract_rst(path)

    assert result.units == []
    assert result.gaps == ["empty or no addressable rST content"]


def test_rst_code_claims_round_trip_exact_crlf_characters_and_bytes(tmp_path):
    path = tmp_path / "exact.rst"
    raw = (
        "Example\r\n"
        "=======\r\n"
        "\r\n"
        ".. code-block:: python\r\n"
        "   :linenos:\r\n"
        "\r\n"
        "   def ping():  \r\n"
        '       return "café"\r\n'
        "\r\n"
        "Literal::\r\n"
        "\r\n"
        "   echo hi  \r\n"
    ).encode("utf-8")
    path.write_bytes(raw)
    source = raw.decode("utf-8")

    result = extract_rst(path)
    code = [unit for unit in result.units if unit.modality is Modality.CODE]

    assert [unit.content for unit in code] == [
        '   def ping():  \r\n       return "café"\r\n',
        "   echo hi  \r\n",
    ]
    for unit in code:
        lo, hi = unit.origin.char_span
        assert unit.content == source[lo:hi]
        byte_lo = len(source[:lo].encode("utf-8"))
        byte_hi = len(source[:hi].encode("utf-8"))
        assert unit.content.encode("utf-8") == raw[byte_lo:byte_hi]
    assert result.meta["bytes"] == len(raw)


def test_rst_rejects_non_utf8_bytes_before_emitting_claims(tmp_path):
    path = tmp_path / "broken.rst"
    path.write_bytes(b"Heading\n=======\n\n.. code-block:: text\n\n   \xff\n")

    with pytest.raises(InvalidRst) as raised:
        extract_rst(path)

    assert raised.value.path == path
    assert raised.value.kind == "reStructuredText"
    assert "strict UTF-8" in str(raised.value)
