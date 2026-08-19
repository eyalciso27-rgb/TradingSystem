"""Schema-aware parsing of the free constituent table, in both MediaWiki forms.

The owner-host run that motivated this module produced an empty snapshot from a
perfectly healthy endpoint. The reason was narrow: the previous parser assumed
ticker cells look like ``| AAPL``, while the live page writes them as templates::

    {{NasdaqSymbol|AAPL}}
    {{NyseSymbol|MMM}}
    {{NyseSymbol|BRK.B}}
    {{BZX link|CBOE}}

Splitting a row on ``|`` cut those templates in half, the fragments failed
ticker validation, every row was skipped, and the validator correctly refused
the empty result. Nothing was wrong except the parser's idea of the syntax.

Two things follow from that, and both shape this module:

* **Parse the structure, not a guessed shape.** Cells are split with a scanner
  that understands ``{{…}}`` and ``[[…]]`` nesting, so a separator inside a
  template is not mistaken for a cell boundary. Tickers are read from template
  arguments generically rather than by listing the template names that happen to
  appear today.
* **Bind fields to the table's own header row.** The live table is Symbol,
  Security, GICS Sector, GICS Sub-Industry, Headquarters Location, Date added,
  CIK, Founded. Scanning every cell for "something that looks like a date" is
  how Headquarters quietly becomes Date added when a column moves. The header
  row is read explicitly, and a table whose schema cannot be identified is
  refused rather than guessed at.

Diagnostics are first-class here. "snapshot is empty" was safe but told the
owner nothing about *why*, and diagnosing it took a round trip. A parse that
comes out implausible now reports what it saw — headers, row counts, and a
handful of bounded, sanitized samples of the cells it could not read — which is
enough to recognise upstream format drift from one run.

This module deals only in strings and rows. Identity, checksums, snapshots and
validation belong to :mod:`sp500_current_universe`, which consumes these rows.
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass, field

#: Canonical ticker shape. Unchanged: extraction feeds validation, it does not
#: replace it. ``BRK.B`` and ``BF.B`` are in the index and must pass.
TICKER_RE = re.compile(r"[A-Z][A-Z.\-]{0,6}")

#: How many unreadable cells to quote back, and how much of each. Bounded on
#: purpose: diagnostics must never turn into a dump of the source payload.
MAX_DIAGNOSTIC_SAMPLES = 6
MAX_DIAGNOSTIC_SAMPLE_CHARS = 80

#: The header names the live table uses, normalised, mapped to our field names.
#: A header we do not recognise is reported, never silently positional.
HEADER_ALIASES: dict[str, str] = {
    "symbol": "ticker",
    "ticker": "ticker",
    "ticker symbol": "ticker",
    "security": "name",
    "company": "name",
    "gics sector": "gics_sector",
    "gics sub-industry": "gics_sub_industry",
    "gics sub industry": "gics_sub_industry",
    "headquarters location": "headquarters",
    "headquarters": "headquarters",
    "date added": "date_added",
    "date first added": "date_added",
    "cik": "cik",
    "founded": "founded",
}

#: Without these two there is no constituent table worth reading.
REQUIRED_FIELDS = ("ticker", "name")

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_REF_RE = re.compile(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_ROW_BREAK_RE = re.compile(r"^\|-.*$", re.MULTILINE)
_INNERMOST_TEMPLATE_RE = re.compile(r"\{\{([^{}]*)\}\}")
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]*)\]\]")
_ATTRIBUTE_RE = re.compile(r"^[^|\[{]*?[A-Za-z-]+\s*=\s*[^|]*\|")
_HTML_TABLE_OPEN_RE = re.compile(r"<table[^>]*>", re.IGNORECASE)
_HTML_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_HTML_CELL_RE = re.compile(r"<(t[dh])[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)


class TableParseError(Exception):
    """The payload carries no table this parser is willing to read."""


@dataclass(frozen=True, slots=True)
class ParseDiagnostics:
    """What the parser saw. Bounded, sanitized, safe to print or log.

    Exists because "snapshot is empty" is a safe answer and a useless one: it
    says a refresh was refused without saying whether the table was missing, the
    schema moved, or the cells simply stopped looking the way we expected.
    """

    representation: str
    table_found: bool
    headers: tuple[str, ...] = ()
    schema: tuple[str, ...] = ()
    candidate_rows: int = 0
    accepted_rows: int = 0
    skipped_rows: int = 0
    #: Sanitized, truncated samples of ticker cells that could not be read.
    skipped_samples: tuple[str, ...] = ()
    unmapped_headers: tuple[str, ...] = ()

    def lines(self) -> tuple[str, ...]:
        """Operator-readable diagnostics, bounded in both count and length."""
        detail = [
            f"representation={self.representation}",
            f"table_found={self.table_found}",
            f"headers={list(self.headers)}",
            f"schema={list(self.schema)}",
            f"candidate_rows={self.candidate_rows}",
            f"accepted_rows={self.accepted_rows}",
            f"skipped_rows={self.skipped_rows}",
        ]
        if self.unmapped_headers:
            detail.append(f"unrecognised_headers={list(self.unmapped_headers)}")
        for sample in self.skipped_samples:
            detail.append(f"unreadable ticker cell: {sample}")
        return tuple(detail)


@dataclass(frozen=True, slots=True)
class ConstituentRow:
    """One table row, still as text. Identity decisions happen a layer up."""

    ticker: str
    name: str
    gics_sector: str | None = None
    gics_sub_industry: str | None = None
    headquarters: str | None = None
    date_added: str | None = None
    cik: str | None = None
    founded: str | None = None


@dataclass
class _Accumulator:
    rows: list[ConstituentRow] = field(default_factory=list)
    candidates: int = 0
    skipped: int = 0
    samples: list[str] = field(default_factory=list)

    def skip(self, cell: str) -> None:
        self.skipped += 1
        if len(self.samples) < MAX_DIAGNOSTIC_SAMPLES:
            self.samples.append(sanitize_sample(cell))


# -- text helpers --------------------------------------------------------------


def sanitize_sample(text: str) -> str:
    """One line, bounded length, quoted. Never a payload dump."""
    collapsed = " ".join(text.split())
    if len(collapsed) > MAX_DIAGNOSTIC_SAMPLE_CHARS:
        collapsed = collapsed[: MAX_DIAGNOSTIC_SAMPLE_CHARS - 1] + "…"
    return repr(collapsed)


def _strip_noise(text: str) -> str:
    """Comments and citations are not data."""
    return _REF_RE.sub(" ", _COMMENT_RE.sub(" ", text))


def _strip_cell_attributes(cell: str) -> str:
    """Drop wikitext cell attributes: ``style="…" | value`` -> ``value``.

    Only a leading run that actually looks like ``key=value`` is dropped, and
    only before any link or template, so a cell whose content merely contains an
    equals sign keeps it.
    """
    previous = None
    current = cell
    while previous != current:
        previous = current
        current = _ATTRIBUTE_RE.sub("", current, count=1).lstrip()
    return current


def _template_parts(body: str) -> tuple[str, list[str]]:
    """``name|a|b=c|d`` -> ("name", ["a", "d"]). Named arguments are dropped."""
    pieces = body.split("|")
    name = pieces[0].strip()
    positional = [piece.strip() for piece in pieces[1:] if "=" not in piece]
    return name, positional


def _resolve_display(text: str) -> str:
    """Reduce wikitext to the text a reader would see.

    Templates collapse to their last positional argument and links to their
    display form, both innermost-first so nesting resolves correctly.
    """
    current = _strip_noise(text)

    def _template(match: re.Match[str]) -> str:
        _, positional = _template_parts(match.group(1))
        return positional[-1] if positional else " "

    def _link(match: re.Match[str]) -> str:
        target, _, display = match.group(1).partition("|")
        return (display or target).strip()

    for pattern, repl in ((_INNERMOST_TEMPLATE_RE, _template), (_WIKILINK_RE, _link)):
        previous = None
        while previous != current:
            previous = current
            current = pattern.sub(repl, current)
    current = _TAG_RE.sub(" ", current)
    current = html_module.unescape(current).replace(" ", " ")
    return " ".join(current.split())


def cell_text(cell: str) -> str:
    """A wikitext cell as displayed text."""
    return _resolve_display(_strip_cell_attributes(cell))


def extract_ticker(cell: str) -> str | None:
    """Read a ticker out of a cell, whatever wrapper the page currently uses.

    The live page wraps tickers in exchange templates (``{{NasdaqSymbol|AAPL}}``,
    ``{{NyseSymbol|BRK.B}}``, ``{{BZX link|CBOE}}``). Rather than enumerate
    template names — which would break on the next one added — every positional
    template argument is a candidate, alongside link targets, link display text
    and the plain rendered text. The first candidate that passes canonical
    ticker validation wins.

    Template *names* are never candidates: ``{{NYSE|MMM}}`` must yield ``MMM``,
    not ``NYSE``.
    """
    body = _strip_noise(_strip_cell_attributes(cell))
    candidates: list[str] = []

    # Template arguments, innermost first, until no templates remain.
    remaining = body
    while True:
        matches = list(_INNERMOST_TEMPLATE_RE.finditer(remaining))
        if not matches:
            break
        for match in matches:
            _, positional = _template_parts(match.group(1))
            candidates.extend(positional)
        remaining = _INNERMOST_TEMPLATE_RE.sub(" ", remaining)

    for match in _WIKILINK_RE.finditer(body):
        target, _, display = match.group(1).partition("|")
        candidates.extend(part for part in (display, target) if part)

    candidates.append(_resolve_display(body))

    for candidate in candidates:
        token = _resolve_display(candidate).strip().upper()
        if TICKER_RE.fullmatch(token):
            return token
    return None


# -- wikitext ------------------------------------------------------------------


def _iter_wikitext_tables(wikitext: str) -> list[str]:
    """Every ``{| … |}`` table, nesting-aware, outermost first."""
    tables: list[str] = []
    depth = 0
    start = 0
    index = 0
    length = len(wikitext)
    while index < length - 1:
        pair = wikitext[index : index + 2]
        if pair == "{|":
            if depth == 0:
                start = index
            depth += 1
            index += 2
            continue
        if pair == "|}" and depth:
            depth -= 1
            if depth == 0:
                tables.append(wikitext[start : index + 2])
            index += 2
            continue
        index += 1
    return tables


def _split_row(segment: str, marker: str) -> list[str]:
    """Split one row into cells, respecting ``{{…}}`` and ``[[…]]`` nesting.

    This is the fix at its narrowest: a naive split on ``|`` tears
    ``{{NasdaqSymbol|AAPL}}`` into ``{{NasdaqSymbol`` and ``AAPL}}``, and neither
    half is a ticker. Cells are separated by ``||`` (or ``!!`` in a header) or by
    a marker at the start of a line, and only at nesting depth zero.

    Empty cells are preserved. Dropping them would silently shift every later
    column — exactly the failure mode that turns Headquarters into Date added.
    """
    cells: list[str] = []
    buffer: list[str] = []
    depth = 0
    at_line_start = True
    seen_marker = False
    index = 0
    length = len(segment)

    while index < length:
        char = segment[index]
        pair = segment[index : index + 2]
        if pair in ("{{", "[["):
            depth += 1
            buffer.append(pair)
            index += 2
            at_line_start = False
            continue
        if pair in ("}}", "]]"):
            depth = max(0, depth - 1)
            buffer.append(pair)
            index += 2
            at_line_start = False
            continue
        is_separator = depth == 0 and (
            pair == marker * 2 or (at_line_start and char == marker)
        )
        if is_separator:
            width = 2 if pair == marker * 2 else 1
            if seen_marker:
                cells.append("".join(buffer))
            # Text before the first marker is row attributes, not a cell.
            buffer = []
            seen_marker = True
            index += width
            at_line_start = False
            continue
        if char == "\n":
            buffer.append(char)
            at_line_start = True
            index += 1
            continue
        if at_line_start and char.isspace():
            buffer.append(char)
            index += 1
            continue
        buffer.append(char)
        at_line_start = False
        index += 1

    if seen_marker:
        cells.append("".join(buffer))
    return [cell.strip() for cell in cells]


def _map_schema(headers: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Header names -> our field names, positionally. Unknown headers reported."""
    schema: list[str] = []
    unmapped: list[str] = []
    for header in headers:
        key = header.strip().lower().rstrip(":")
        mapped = HEADER_ALIASES.get(key)
        if mapped is None:
            schema.append("")
            if key:
                unmapped.append(header)
        else:
            schema.append(mapped)
    return tuple(schema), tuple(unmapped)


def _row_from_cells(cells: list[str], schema: tuple[str, ...]) -> ConstituentRow | None:
    """Bind cells to fields BY SCHEMA POSITION, never by pattern-hunting."""
    values: dict[str, str] = {}
    for position, field_name in enumerate(schema):
        if not field_name or position >= len(cells):
            continue
        if field_name == "ticker":
            ticker = extract_ticker(cells[position])
            if ticker is None:
                return None
            values["ticker"] = ticker
            continue
        text = cell_text(cells[position])
        if text:
            values[field_name] = text
    if "ticker" not in values:
        return None
    return ConstituentRow(
        ticker=values["ticker"],
        name=values.get("name", ""),
        gics_sector=values.get("gics_sector"),
        gics_sub_industry=values.get("gics_sub_industry"),
        headquarters=values.get("headquarters"),
        date_added=values.get("date_added"),
        cik=values.get("cik"),
        founded=values.get("founded"),
    )


def _ticker_cell(cells: list[str], schema: tuple[str, ...]) -> str:
    for position, field_name in enumerate(schema):
        if field_name == "ticker" and position < len(cells):
            return cells[position]
    return cells[0] if cells else ""


def parse_wikitext_table(
    wikitext: str,
) -> tuple[tuple[ConstituentRow, ...], ParseDiagnostics]:
    """Parse the constituent table out of MediaWiki wikitext."""
    representation = "wikitext"
    tables = _iter_wikitext_tables(wikitext)
    if not tables:
        raise TableParseError("payload carries no wikitext table")

    for table in tables:
        # Drop the closing `|}`: left on, it rides along in the final cell of
        # the last row and quietly corrupts that value.
        body = table[:-2] if table.endswith("|}") else table
        segments = _ROW_BREAK_RE.split(body)
        headers: list[str] = []
        header_index = -1
        for position, segment in enumerate(segments):
            cells = _split_row(segment, "!")
            mapped = [cell_text(cell) for cell in cells]
            schema, _ = _map_schema(mapped)
            if all(required in schema for required in REQUIRED_FIELDS):
                headers, header_index = mapped, position
                break
        if header_index < 0:
            continue

        schema, unmapped = _map_schema(headers)
        accumulator = _Accumulator()
        for segment in segments[header_index + 1 :]:
            cells = _split_row(segment, "|")
            if not cells:
                continue
            accumulator.candidates += 1
            row = _row_from_cells(cells, schema)
            if row is None:
                accumulator.skip(_ticker_cell(cells, schema))
                continue
            accumulator.rows.append(row)

        return tuple(accumulator.rows), ParseDiagnostics(
            representation=representation,
            table_found=True,
            headers=tuple(headers),
            schema=schema,
            candidate_rows=accumulator.candidates,
            accepted_rows=len(accumulator.rows),
            skipped_rows=accumulator.skipped,
            skipped_samples=tuple(accumulator.samples),
            unmapped_headers=unmapped,
        )

    raise TableParseError(
        "no wikitext table carries an identifiable constituent schema"
        f" (needs {list(REQUIRED_FIELDS)} in its header row)"
    )


# -- MediaWiki rendered HTML (the SAME page, a second documented form) ---------


def _iter_html_tables(markup: str) -> list[str]:
    """Every ``<table>…</table>``, nesting-aware, outermost first."""
    tables: list[str] = []
    depth = 0
    start = 0
    index = 0
    lowered = markup.lower()
    length = len(markup)
    while index < length:
        if lowered.startswith("<table", index):
            match = _HTML_TABLE_OPEN_RE.match(markup, index)
            if match is not None:
                if depth == 0:
                    start = index
                depth += 1
                index = match.end()
                continue
        if lowered.startswith("</table>", index) and depth:
            depth -= 1
            index += len("</table>")
            if depth == 0:
                tables.append(markup[start:index])
            continue
        index += 1
    return tables


def _html_cell_text(cell: str) -> str:
    text = _TAG_RE.sub(" ", _strip_noise(cell))
    return " ".join(html_module.unescape(text).replace(" ", " ").split())


def parse_html_table(markup: str) -> tuple[tuple[ConstituentRow, ...], ParseDiagnostics]:
    """Parse the constituent table out of MediaWiki's own rendered HTML.

    Same page, same API, second documented representation. MediaWiki has already
    expanded the templates here, so a ticker cell arrives as plain text and this
    path is immune to the template-syntax drift that broke the wikitext parse.
    """
    tables = _iter_html_tables(markup)
    if not tables:
        raise TableParseError("payload carries no HTML table")

    for table in tables:
        rows = _HTML_ROW_RE.findall(table)
        headers: list[str] = []
        header_index = -1
        for position, row in enumerate(rows):
            cells = _HTML_CELL_RE.findall(row)
            if not any(tag.lower() == "th" for tag, _ in cells):
                continue
            mapped = [_html_cell_text(body) for _, body in cells]
            schema, _ = _map_schema(mapped)
            if all(required in schema for required in REQUIRED_FIELDS):
                headers, header_index = mapped, position
                break
        if header_index < 0:
            continue

        schema, unmapped = _map_schema(headers)
        accumulator = _Accumulator()
        for row in rows[header_index + 1 :]:
            cells = [body for tag, body in _HTML_CELL_RE.findall(row) if tag.lower() != "th"]
            if not cells:
                continue
            accumulator.candidates += 1
            texts = [_html_cell_text(body) for body in cells]
            built = _row_from_cells(texts, schema)
            if built is None:
                accumulator.skip(_ticker_cell(texts, schema))
                continue
            accumulator.rows.append(built)

        return tuple(accumulator.rows), ParseDiagnostics(
            representation="html",
            table_found=True,
            headers=tuple(headers),
            schema=schema,
            candidate_rows=accumulator.candidates,
            accepted_rows=len(accumulator.rows),
            skipped_rows=accumulator.skipped,
            skipped_samples=tuple(accumulator.samples),
            unmapped_headers=unmapped,
        )

    raise TableParseError("no HTML table carries an identifiable constituent schema")


__all__ = [
    "HEADER_ALIASES",
    "MAX_DIAGNOSTIC_SAMPLES",
    "MAX_DIAGNOSTIC_SAMPLE_CHARS",
    "REQUIRED_FIELDS",
    "TICKER_RE",
    "ConstituentRow",
    "ParseDiagnostics",
    "TableParseError",
    "cell_text",
    "extract_ticker",
    "parse_html_table",
    "parse_wikitext_table",
    "sanitize_sample",
]
