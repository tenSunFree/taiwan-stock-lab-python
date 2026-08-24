"""
Map TPEx's bulletin/attention and bulletin/disposal JSON responses
into RegulatoryRiskStatus domain models.

Both endpoints share the same real, verified request/response shape
(confirmed via a real HAR capture + PowerShell round-trip, not
guessed):

    POST https://www.tpex.org.tw/www/zh-tw/bulletin/attention
    POST https://www.tpex.org.tw/www/zh-tw/bulletin/disposal
    Content-Type: application/x-www-form-urlencoded
    Body: startDate=YYYY%2FMM%2FDD&endDate=YYYY%2FMM%2FDD&code=&cate=
          &type=all&order=date&id=&response=json
          (disposal also has reason=-1&measure=-1)

    {"tables": [{"fields": [...], "data": [[...], [...], ...]}], ...}

No auth, no Cloudflare cookie required — confirmed by a clean
Invoke-RestMethod call with no session/cookie state at all.

TWSE's equivalent (announcement/notice, announcement/punish) is a
DIFFERENT shape entirely — an HTML table, not this JSON envelope —
and is handled by a separate mapper once that endpoint's own HTML
structure is implemented (see app.jobs.daily_ranking's rollout notes;
this module is TPEx-only, deliberately, per this feature's own
"verify each source independently before merging" rollout plan).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from app.domain.models import RegulatoryRiskStatus


class RegulatorySourceFormatError(Exception):
    """
    Raised when a TPEx bulletin response doesn't match the verified
    {"tables": [{"fields": [...], "data": [...]}]} shape, or when an
    expected column is missing from `fields`. Deliberately loud (not
    a silently-returned empty dict) — a malformed table shape or a
    disappeared column means every row may be misread, not just the
    ones that happen to fail a narrower per-row check, and hiding that
    behind an empty result would look identical to "genuinely zero
    stocks currently flagged today."
    """


# TPEx's bulletin/attention and bulletin/disposal endpoints use a
# slash-separated ROC date ("115/08/21") — a DIFFERENT format from
# TPEx's own daily-price/valuation endpoints' "1150812" (no
# separators, see app.ingestion.tpex_mapper.roc_date_to_gregorian and
# app.ingestion.valuation_mapper), confirmed via real fixtures from
# both endpoint families, not assumed to match just because they're
# all "TPEx."
_TPEX_BULLETIN_DATE_RE = re.compile(r"^(\d{2,3})/(\d{2})/(\d{2})$")


def _tpex_bulletin_date_to_gregorian(text: str) -> dt.date | None:
    match = _TPEX_BULLETIN_DATE_RE.match((text or "").strip())
    if not match:
        return None
    roc_year, month, day = (int(group) for group in match.groups())
    try:
        return dt.date(roc_year + 1911, month, day)
    except ValueError:
        return None


_TPEX_PERIOD_RE = re.compile(r"^(\d{2,3}/\d{2}/\d{2})\s*~\s*(\d{2,3}/\d{2}/\d{2})$")


def _parse_tpex_disposition_period(text: str) -> tuple[dt.date | None, dt.date | None]:
    """
    Parses TPEx's 處置起訖時間 field, e.g. "115/08/24~115/08/28", into
    (start, end) Gregorian dates. Returns (None, None) — not a
    zero-length or open-ended range — if the text doesn't match the
    expected shape; the caller treats that row as "cannot verify its
    active period," which fails it out of the result the same way a
    missing stock_id would.
    """
    match = _TPEX_PERIOD_RE.match((text or "").strip())
    if not match:
        return None, None
    return (
        _tpex_bulletin_date_to_gregorian(match.group(1)),
        _tpex_bulletin_date_to_gregorian(match.group(2)),
    )


def _extract_tpex_table(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Both bulletin/attention and bulletin/disposal share the same
    {"tables": [{"fields": [...], "data": [[...], ...]}]} envelope —
    confirmed via real fixtures from both. Raises
    RegulatorySourceFormatError (never a silent empty return) if that
    envelope shape itself is missing or malformed, since that means
    the WHOLE payload can't be trusted, not just individual rows.
    """
    tables = payload.get("tables") if isinstance(payload, dict) else None
    if not isinstance(tables, list) or not tables:
        raise RegulatorySourceFormatError(
            "expected a non-empty 'tables' list in the TPEx bulletin response"
        )

    table = tables[0]
    if not isinstance(table, dict):
        raise RegulatorySourceFormatError("tables[0] is not an object")

    fields = table.get("fields")
    data = table.get("data")

    if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
        raise RegulatorySourceFormatError("tables[0].fields is missing or malformed")
    if not isinstance(data, list):
        raise RegulatorySourceFormatError("tables[0].data is missing or malformed")

    return {"fields": fields, "data": data}


def _build_field_index(
    fields: list[str], *, required: tuple[str, ...]
) -> dict[str, int]:
    """
    Index-by-name lookup built fresh from each response's own `fields`
    array, rather than hardcoded positional indices — so a future
    TPEx column reordering doesn't silently misread data into the
    wrong field. A genuinely MISSING required column raises
    RegulatorySourceFormatError instead of quietly producing wrong or
    empty results.
    """
    index = {name: position for position, name in enumerate(fields)}
    missing = [name for name in required if name not in index]
    if missing:
        raise RegulatorySourceFormatError(
            f"expected column(s) {missing} not found in fields={fields!r}"
        )
    return index


def build_tpex_attention_statuses(
    *, target_date: dt.date, payload: dict[str, Any]
) -> dict[str, RegulatoryRiskStatus]:
    """
    Parses TPEx's bulletin/attention JSON response into a
    stock_id -> RegulatoryRiskStatus dict, filtered to rows whose own
    公告日期 (announcement date) is EXACTLY target_date.

    Exact-date match, deliberately NOT "latest available <=
    target_date" (unlike app.ingestion.valuation_mapper's P/E date
    handling): attention is a per-day announcement, not a
    slowly-updating snapshot value. The query response commonly
    contains the SAME stock_id on multiple different announcement
    dates within the query window — confirmed via a real fixture:
    stock 30811 appeared on 115/08/21, 115/08/20, AND 115/08/19 in one
    response, each with a different 累計 (cumulative count) and a
    different 注意交易資訊 reason text. "Was this stock flagged today"
    must only match today's own announcement row; using "latest
    available" semantics here would make yesterday's announcement
    silently still count as today's even on a day with no new
    announcement for that stock.
    """
    table = _extract_tpex_table(payload)
    field_index = _build_field_index(
        table["fields"],
        required=("證券代號", "注意交易資訊", "公告日期"),
    )

    result: dict[str, RegulatoryRiskStatus] = {}

    for row in table["data"]:
        if not isinstance(row, list):
            continue

        row_date = _tpex_bulletin_date_to_gregorian(str(row[field_index["公告日期"]]))
        if row_date != target_date:
            continue

        stock_id = str(row[field_index["證券代號"]] or "").strip()
        if not stock_id:
            continue

        reason = str(row[field_index["注意交易資訊"]] or "").strip() or None

        result[stock_id] = RegulatoryRiskStatus(
            trading_date=target_date,
            stock_id=stock_id,
            is_attention=True,
            attention_reason=reason,
        )

    return result


def build_tpex_disposition_statuses(
    *, target_date: dt.date, payload: dict[str, Any]
) -> dict[str, RegulatoryRiskStatus]:
    """
    Parses TPEx's bulletin/disposal JSON response into a
    stock_id -> RegulatoryRiskStatus dict, filtered to rows whose
    disposition PERIOD (處置起訖時間, e.g. "115/08/24~115/08/28")
    covers target_date — i.e. start <= target_date <= end.

    Deliberately a RANGE match, NOT an exact-date match on 公布日期
    (unlike build_tpex_attention_statuses's date handling above): a
    disposition is an ACTIVE PERIOD, not a point-in-time announcement.
    The announcement date and the period it governs are different
    fields — note also the field name itself differs from attention's
    ("公布日期" here vs "公告日期" there, confirmed via real fixtures
    from both, not the same string). "Is this stock CURRENTLY under
    disposition today" means checking whether today falls inside its
    active period, not whether today happens to be the day it was
    announced.

    If more than one row for the same stock_id has a period covering
    target_date (e.g. an overlapping renewal into a further
    disposition round), the row with the latest 公布日期 wins — same
    "prefer the newest applicable record" principle as
    app.ingestion.valuation_mapper.build_twse_valuations, applied here
    to disposition periods instead of valuation snapshot dates.
    """
    table = _extract_tpex_table(payload)
    field_index = _build_field_index(
        table["fields"],
        required=("公布日期", "證券代號", "處置起訖時間", "處置原因", "處置內容"),
    )

    # stock_id -> (announced_on, RegulatoryRiskStatus) — announced_on
    # is kept alongside the built status only to decide which row wins
    # when more than one covers target_date; it isn't part of the
    # final result.
    candidates: dict[str, tuple[dt.date, RegulatoryRiskStatus]] = {}

    for row in table["data"]:
        if not isinstance(row, list):
            continue

        announced_on = _tpex_bulletin_date_to_gregorian(
            str(row[field_index["公布日期"]])
        )
        if announced_on is None:
            continue

        period_text = str(row[field_index["處置起訖時間"]] or "")
        start_date, end_date = _parse_tpex_disposition_period(period_text)
        if start_date is None or end_date is None:
            continue
        if not (start_date <= target_date <= end_date):
            continue

        stock_id = str(row[field_index["證券代號"]] or "").strip()
        if not stock_id:
            continue

        existing = candidates.get(stock_id)
        if existing is not None and existing[0] >= announced_on:
            continue  # already have an equally or more recent record

        reason = str(row[field_index["處置原因"]] or "").strip() or None
        measure = str(row[field_index["處置內容"]] or "").strip() or None

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
