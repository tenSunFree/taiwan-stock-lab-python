"""
Map TWSE's announcement/notice and announcement/punish HTML responses
into RegulatoryRiskStatus domain models.

Verified request/response shape (confirmed via real
Invoke-WebRequest -UseBasicParsing calls with correct UTF-8 decoding,
not guessed):

    GET https://www.twse.com.tw/announcement/notice?response=html
    GET https://www.twse.com.tw/announcement/punish?response=html

Both return a full HTML document — NOT the {"tables": [...]} JSON
envelope TPEx's bulletin/attention and bulletin/disposal endpoints use
(see app.ingestion.regulatory_mapper for those). Confirmed via three
separate response= attempts (html/csv/json — see
market_data_client.TwseClient.fetch_attention/fetch_disposition
docstrings) that the server always serves this same HTML "報表"
template regardless of the requested response= value — an older
report-generator system, not one of TWSE's newer JSON-first OpenAPI
endpoints like BWIBBU_ALL.

Verified table shape (confirmed via a real fetch of notice.html — on
a day with zero currently-flagged attention stocks, so the <tbody>
happened to be empty, but the surrounding structure is real, not
synthesized):

    <table>
      <thead>
        <tr><th colspan="N"><div>公布XX有價證券資訊 (ROC date range
            [, 全部OO有價證券])</div></th></tr>
        <tr><th>col1</th><th>col2</th>...</tr>
      </thead>
      <tbody>
        <tr><td>val1</td><td>val2</td>...</tr>
        ...  (zero or more)
      </tbody>
    </table>

Column headers, confirmed via real fetches of both pages:
  notice (attention): 編號 / 證券代號 / 證券名稱 / 累計次數 /
      注意交易資訊 / 日期 / 收盤價 / 本益比
  punish (disposition): 編號 / 公布日期 / 證券代號 / 證券名稱 / 累計 /
      處置條件 / 處置起迄時間 / 處置措施 / 處置內容 / 備註

NOT YET independently confirmed: the exact date-string format inside
a DATA row's <td> (as opposed to the title's own date range text,
which IS confirmed). The one real fetch available had zero rows to
inspect. Both "1150821" (bare, TWSE's other JSON endpoints' format)
and "115/08/21" (slash-separated, confirmed in both this page's own
title text AND in TPEx's bulletin endpoints) are accepted defensively
below — but this should be re-verified against a real non-empty
response the next time one is available, the same "verify before
fully trusting" discipline this project used for TPEx's bare-array-
vs-wrapped-envelope question earlier in this rollout.

Parsed with Python's stdlib html.parser.HTMLParser (a small
state-machine table extractor below) — not a regex over raw HTML
(regex-based HTML parsing is a well-known foot-gun: nesting, optional
closing tags, and attribute quoting styles all break naive patterns),
and not a new third-party dependency (e.g. BeautifulSoup) — this
project has none currently, and neither TWSE page's table nesting is
complex enough to need one.
"""

from __future__ import annotations

import datetime as dt
import re
from html.parser import HTMLParser
from typing import Any

from app.domain.models import RegulatoryRiskStatus
from app.ingestion.regulatory_mapper import RegulatorySourceFormatError

# --- HTML table extraction ---------------------------------------------------


class _TwseReportTableParser(HTMLParser):
    """
    Extracts exactly the shape described in this module's docstring:
    a title cell's text, a header row's cell texts, and each tbody
    row's cell texts — nothing else. Ignores everything outside the
    first <table> it finds (CSS in <head>, surrounding page chrome).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_text: str | None = None
        self.header_cells: list[str] = []
        self.rows: list[list[str]] = []

        self._in_table = False
        self._in_thead = False
        self._in_tbody = False
        self._thead_tr_count = 0
        self._in_cell = False
        self._current_cell_text: list[str] = []
        self._current_row: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table" and not self._in_table:
            self._in_table = True
        elif tag == "thead" and self._in_table:
            self._in_thead = True
        elif tag == "tbody" and self._in_table:
            self._in_tbody = True
        elif tag == "tr" and self._in_thead:
            self._thead_tr_count += 1
        elif tag in ("th", "td") and self._in_table:
            self._in_cell = True
            self._current_cell_text = []
        elif tag == "tr" and self._in_tbody:
            self._current_row = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("th", "td") and self._in_cell:
            self._in_cell = False
            text = "".join(self._current_cell_text).strip()
            if self._in_thead:
                if self._thead_tr_count == 1:
                    # title row — may be the only cell, keep first
                    if self.title_text is None:
                        self.title_text = text
                else:
                    self.header_cells.append(text)
            elif self._in_tbody:
                self._current_row.append(text)
        elif tag == "tr" and self._in_tbody:
            if self._current_row:
                self.rows.append(self._current_row)
        elif tag == "thead":
            self._in_thead = False
        elif tag == "tbody":
            self._in_tbody = False
        elif tag == "table":
            self._in_table = False

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._current_cell_text.append(data)


def _extract_twse_table(html_text: str) -> dict[str, Any]:
    """
    Parses the raw HTML document into {"title": str, "fields": [...],
    "data": [[...], ...]} — mirroring the shape
    app.ingestion.regulatory_mapper's TPEx functions already consume,
    so the rest of this module's logic stays symmetric with TPEx's.
    Raises RegulatorySourceFormatError if the expected <table>/title/
    header structure isn't found at all — this means the page
    genuinely doesn't match the verified template (e.g. TWSE changed
    it, or the response was an error page), not just that a row was
    unusable.
    """
    parser = _TwseReportTableParser()
    try:
        parser.feed(html_text)
    except Exception as exc:  # pragma: no cover - defensive, HTMLParser is lenient
        raise RegulatorySourceFormatError(f"failed to parse HTML: {exc}") from exc

    if parser.title_text is None:
        raise RegulatorySourceFormatError(
            "expected table title row not found in TWSE HTML response"
        )
    if not parser.header_cells:
        raise RegulatorySourceFormatError(
            "expected table header row not found in TWSE HTML response"
        )

    return {
        "title": parser.title_text,
        "fields": parser.header_cells,
        "data": parser.rows,
    }


def _build_field_index(
    fields: list[str], *, required: tuple[str, ...]
) -> dict[str, int]:
    """Same principle as app.ingestion.regulatory_mapper's — index by
    column NAME from this response's own header row, not a hardcoded
    position, so a header reorder can't silently misread data."""
    index = {name: position for position, name in enumerate(fields)}
    missing = [name for name in required if name not in index]
    if missing:
        raise RegulatorySourceFormatError(
            f"expected column(s) {missing} not found in fields={fields!r}"
        )
    return index


# --- Date parsing -------------------------------------------------------------

# Both known ROC date shapes seen across this project's various TWSE/
# TPEx endpoints — see this module's docstring for why row-level cell
# format isn't independently confirmed yet for these two specific
# pages, and why both are accepted here defensively rather than
# picking one and failing on the other.
_BARE_ROC_DATE_RE = re.compile(r"^(\d{3})(\d{2})(\d{2})$")
_SLASH_ROC_DATE_RE = re.compile(r"^(\d{2,3})/(\d{2})/(\d{2})$")


def _twse_roc_date_to_gregorian(text: str) -> dt.date | None:
    cleaned = (text or "").strip()

    match = _SLASH_ROC_DATE_RE.match(cleaned)
    if not match:
        match = _BARE_ROC_DATE_RE.match(cleaned)
    if not match:
        return None

    roc_year, month, day = (int(group) for group in match.groups())
    try:
        return dt.date(roc_year + 1911, month, day)
    except ValueError:
        return None


# Title date-range text is a DIFFERENT format between the two pages —
# confirmed via real fetches of both: notice.html's title uses full
# Chinese-character dates ("115年08月22日"), punish's uses slash dates
# ("115/08/22"). Both patterns are tried; the caller doesn't need to
# know which one matched.
_TITLE_DATE_RANGE_PATTERNS = (
    re.compile(
        r"(\d{2,3})年(\d{1,2})月(\d{1,2})日\s*至\s*(\d{2,3})年(\d{1,2})月(\d{1,2})日"
    ),
    re.compile(r"(\d{2,3})/(\d{2})/(\d{2})\s*至\s*(\d{2,3})/(\d{2})/(\d{2})"),
)


def _parse_title_date_range(title_text: str) -> tuple[dt.date | None, dt.date | None]:
    for pattern in _TITLE_DATE_RANGE_PATTERNS:
        match = pattern.search(title_text)
        if not match:
            continue
        y1, m1, d1, y2, m2, d2 = (int(g) for g in match.groups())
        try:
            return (
                dt.date(y1 + 1911, m1, d1),
                dt.date(y2 + 1911, m2, d2),
            )
        except ValueError:
            return None, None
    return None, None


def _validate_title_is_parseable(title_text: str) -> None:
    """
    Confirms the response's title at least parses into a real date
    range — catches a wrong page / error page / template change before
    any row is trusted. Raises RegulatorySourceFormatError (not a
    silent pass) if it can't be parsed at all.

    Deliberately does NOT also require target_date to fall inside that
    range — see build_twse_attention_statuses vs
    build_twse_disposition_statuses's own docstrings for why that
    stronger check only makes sense for attention data, not
    disposition.
    """
    start, end = _parse_title_date_range(title_text)
    if start is None or end is None:
        raise RegulatorySourceFormatError(
            f"could not parse a date range from table title: {title_text!r}"
        )


def _validate_title_covers_target_date(title_text: str, target_date: dt.date) -> None:
    """
    Stronger check than _validate_title_is_parseable: also confirms
    target_date itself falls inside the title's own query-date-range —
    the same role as this project's other date-verification checks
    (e.g. twse_mapper's STOCK_DAY_ALL date check), catching a
    wrong-query or stale-cache response before any row is trusted.

    Only valid for ATTENTION data (see
    build_twse_attention_statuses): attention's target_date and the
    title's date range are the same axis — both are announcement
    dates, so a target_date outside the title's range genuinely cannot
    have a matching row. Disposition's target_date is checked against
    each ROW's own active period instead (see
    build_twse_disposition_statuses), which routinely extends beyond
    the title's own announcement-date query window — applying this
    stronger check there would incorrectly reject legitimate matches.
    """
    start, end = _parse_title_date_range(title_text)
    if start is None or end is None:
        raise RegulatorySourceFormatError(
            f"could not parse a date range from table title: {title_text!r}"
        )
    if not (start <= target_date <= end):
        raise RegulatorySourceFormatError(
            f"table title's query window ({start} ~ {end}) does not cover "
            f"target_date {target_date}: {title_text!r}"
        )


# --- Public mappers -----------------------------------------------------------


def build_twse_attention_statuses(
    *, target_date: dt.date, html_text: str
) -> dict[str, RegulatoryRiskStatus]:
    """
    Parses TWSE's announcement/notice HTML response into a
    stock_id -> RegulatoryRiskStatus dict, filtered to rows whose own
    日期 (announcement date) is EXACTLY target_date.

    Same exact-date-match reasoning as
    app.ingestion.regulatory_mapper.build_tpex_attention_statuses:
    attention is a per-day announcement, and this endpoint's own query
    window can span multiple days, so a stock's older announcement
    must not be mistaken for today's.

    A structurally intact table (title parses, headers present) with
    an empty <tbody> is a legitimate "zero stocks currently flagged"
    result and returns {} without raising — confirmed as a real,
    observed response shape (not merely theorized) via a live fetch on
    2026-08-22, a day with zero attention-listed stocks.
    """
    table = _extract_twse_table(html_text)
    _validate_title_covers_target_date(table["title"], target_date)

    field_index = _build_field_index(
        table["fields"], required=("證券代號", "注意交易資訊", "日期")
    )

    result: dict[str, RegulatoryRiskStatus] = {}

    for row in table["data"]:
        if len(row) <= max(field_index.values()):
            continue  # malformed row, fewer cells than the header promised

        row_date = _twse_roc_date_to_gregorian(row[field_index["日期"]])
        if row_date != target_date:
            continue

        stock_id = row[field_index["證券代號"]].strip()
        if not stock_id:
            continue

        reason = row[field_index["注意交易資訊"]].strip() or None

        result[stock_id] = RegulatoryRiskStatus(
            trading_date=target_date,
            stock_id=stock_id,
            is_attention=True,
            attention_reason=reason,
        )

    return result


def build_twse_disposition_statuses(
    *, target_date: dt.date, html_text: str
) -> dict[str, RegulatoryRiskStatus]:
    """
    Parses TWSE's announcement/punish HTML response into a
    stock_id -> RegulatoryRiskStatus dict, filtered to rows whose
    disposition PERIOD (處置起迄時間) covers target_date — same
    range-match reasoning as
    app.ingestion.regulatory_mapper.build_tpex_disposition_statuses.

    處置措施 (a short label, e.g. "第一次處置") and 處置內容 (the full
    legal-text description of the actual measures) are BOTH present
    as separate columns on TWSE's punish page — unlike TPEx's
    disposal endpoint, which only has one combined "處置內容" column
    (see build_tpex_disposition_statuses's own docstring). Mapped
    here as disposition_reason=處置條件 (the short trigger condition,
    e.g. "連續三次") and disposition_measure=處置內容 (the full
    description) to stay consistent with what those two
    RegulatoryRiskStatus fields already mean for the TPEx mapper —
    處置措施's short label isn't separately stored, since
    RegulatoryRiskStatus has no field for it and 處置內容 already
    contains a fuller description of the same thing.

    If more than one row for the same stock_id has a period covering
    target_date, the row with the latest 公布日期 wins — same
    principle as the TPEx disposition mapper.
    """
    table = _extract_twse_table(html_text)
    _validate_title_is_parseable(table["title"])

    field_index = _build_field_index(
        table["fields"],
        required=("公布日期", "證券代號", "處置條件", "處置起迄時間", "處置內容"),
    )

    candidates: dict[str, tuple[dt.date, RegulatoryRiskStatus]] = {}

    for row in table["data"]:
        if len(row) <= max(field_index.values()):
            continue

        announced_on = _twse_roc_date_to_gregorian(row[field_index["公布日期"]])
        if announced_on is None:
            continue

        period_text = row[field_index["處置起迄時間"]]
        start_date, end_date = _parse_disposition_period(period_text)
        if start_date is None or end_date is None:
            continue
        if not (start_date <= target_date <= end_date):
            continue

        stock_id = row[field_index["證券代號"]].strip()
        if not stock_id:
            continue

        existing = candidates.get(stock_id)
        if existing is not None and existing[0] >= announced_on:
            continue

        reason = row[field_index["處置條件"]].strip() or None
        measure = row[field_index["處置內容"]].strip() or None

        candidates[stock_id] = (
            announced_on,
            RegulatoryRiskStatus(
                trading_date=target_date,
                stock_id=stock_id,
                is_disposition=True,
                disposition_start_date=start_date,
                disposition_end_date=end_date,
                disposition_reason=reason,
                disposition_measure=measure,
            ),
        )

    return {stock_id: status for stock_id, (_, status) in candidates.items()}


# TWSE's 處置起迄時間 uses "～" (fullwidth tilde, U+FF5E) as the
# separator, confirmed via a real fetch of punish.html
# ("115/08/24～115/08/28") — visually similar to but a DIFFERENT
# character from TPEx's plain ASCII "~" (U+007E) in the equivalent
# 處置起訖時間 field. Both are accepted here defensively.
_DISPOSITION_PERIOD_RE = re.compile(
    r"^(\d{2,3}/\d{2}/\d{2})\s*[~～]\s*(\d{2,3}/\d{2}/\d{2})$"
)


def _parse_disposition_period(text: str) -> tuple[dt.date | None, dt.date | None]:
    match = _DISPOSITION_PERIOD_RE.match((text or "").strip())
    if not match:
        return None, None
    return (
        _twse_roc_date_to_gregorian(match.group(1)),
        _twse_roc_date_to_gregorian(match.group(2)),
    )
