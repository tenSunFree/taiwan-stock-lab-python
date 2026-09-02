"""
Unified fetch client for TWSE/TPEx general-industry quarterly
comprehensive-income-statement open data — the source of the raw
`基本每股盈餘（元）` figures that app.ingestion.eps_mapper turns into
RawCumulativeEps.

TWSE and TPEx publish the SAME t187ap06_{L,O}_ci dataset shape, but
from two structurally different systems, each with its own
verified-on-the-wire quirk:

    TWSE  https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci
          -> a bare JSON array of dict[str, str]. No auth, no query
          params. Values are already strings (e.g. "季別": "2"), so
          nothing needs coercing before eps_mapper sees it.

    TPEx  https://mopsfin.twse.com.tw/opendata/t187ap06_O_ci
          -> CSV text, not JSON. No auth, no query params. Real TPEx
          exports from this host are UTF-8 WITH a leading BOM
          (U+FEFF); httpx's response.text does NOT strip it, so an
          un-stripped BOM would silently corrupt the first column's
          header (出表日期 -> \ufeff出表日期) and make every row look
          like that field is missing to eps_mapper — a real,
          easy-to-miss bug class, not a hypothetical one.

    NOT independently confirmed at the time of writing: whether
    mopsfin.twse.com.tw currently redirects http -> https on this
    exact path (every real usage example found during verification
    used plain http://, e.g. pandas.read_csv(f"http://mopsfin...
    /t187ap06_O_ci.csv")). This client requests https:// by default;
    if that ever fails with a connection-level error in production
    (not an HTTP error status — those propagate normally), that is
    the first thing to check, not a code bug.

Design mirrors this project's other ingestion clients
(app.ingestion.market_data_client): fetch and snapshot the RAW
response first, via MarketDataClient.fetch_and_snapshot (which also
supplies the shared bounded-retry-on-timeout behaviour — see that
module's docstring — so it is intentionally NOT reimplemented here).
Turning either raw shape into the list[dict[str, str]] rows that
eps_mapper.build_raw_cumulative_eps_points expects is a SEPARATE,
un-bundled step done by the parse_twse_json_rows / parse_tpex_csv_rows
functions below — kept as plain functions rather than methods so they
can be unit-tested directly, without constructing an httpx client or
going through a network round trip at all.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

from app.ingestion.market_data_client import MarketDataClient, RawSourcePayload


class FinancialStatementClient(MarketDataClient):
    """Fetches TWSE's and TPEx's t187ap06_{L,O}_ci general-industry
    comprehensive-income-statement datasets and snapshots them as raw
    payloads. Parsing lives in the module-level parse_* functions
    below, not on this class — see module docstring."""

    source_name = "financial_statement"

    def __init__(self, repository, **kwargs) -> None:
        super().__init__(repository, **kwargs)
        self.twse_url = "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci"
        self.tpex_url = "https://mopsfin.twse.com.tw/opendata/t187ap06_O_ci"

    def fetch_twse_financial_statement(
        self, *, ingestion_run_id: str, target_date: dt.date
    ) -> RawSourcePayload:
        """Fetch TWSE's t187ap06_L_ci (上市公司綜合損益表-一般業).

        No query params exist for this endpoint; target_date is
        ingestion bookkeeping only, exactly like TwseClient.
        fetch_daily_price — the response covers every filed
        year/quarter for every listed company at once, not just
        target_date, and is filtered downstream by eps_mapper /
        eps_period_converter using each row's own 年度/季別 fields.
        """

        def _fetch():
            response = self.http_client.get(self.twse_url)
            response.raise_for_status()
            # Save the parsed JSON as-is (a list[dict]) — TWSE returns
            # JSON directly, unlike TPEx's CSV below. Parsing further
            # than response.json() happens only in
            # parse_twse_json_rows, not here.
            return response.json(), None

        return self.fetch_and_snapshot(
            ingestion_run_id=ingestion_run_id,
            target_date=target_date,
            request_parameters={"market": "twse", "dataset": "t187ap06_L_ci"},
            fetch_fn=_fetch,
        )

    def fetch_tpex_financial_statement(
        self, *, ingestion_run_id: str, target_date: dt.date
    ) -> RawSourcePayload:
        """Fetch TPEx's t187ap06_O_ci (上櫃公司綜合損益表-一般業).

        Same "no query params, target_date is bookkeeping only"
        reasoning as fetch_twse_financial_statement above. Unlike
        that method, the raw payload saved here is CSV TEXT, not
        JSON — see module docstring's BOM warning. That raw text is
        saved completely untouched (BOM included); stripping it is
        parse_tpex_csv_rows's job, not this fetch step's, so the
        exact bytes TPEx actually sent stay recoverable from
        raw_source_payloads.
        """

        def _fetch():
            response = self.http_client.get(self.tpex_url)
            response.raise_for_status()
            return response.text, None

        return self.fetch_and_snapshot(
            ingestion_run_id=ingestion_run_id,
            target_date=target_date,
            request_parameters={"market": "tpex", "dataset": "t187ap06_O_ci"},
            fetch_fn=_fetch,
        )


def parse_twse_json_rows(raw_payload: Any) -> list[dict[str, str]]:
    """Turn a TWSE t187ap06_L_ci raw payload (as saved by
    fetch_twse_financial_statement — already response.json()'d) into
    rows directly consumable by
    eps_mapper.build_raw_cumulative_eps_points.

    Defensive rather than trusting: if the top level ever isn't a
    list (an HTML error page returned with a 200 status, an
    unexpected envelope, etc.), or an individual element isn't a
    dict, this silently drops it instead of raising — matching
    eps_mapper's own fail-closed-per-row convention one layer up,
    rather than crashing the whole ingestion run over one malformed
    element.
    """
    if not isinstance(raw_payload, list):
        return []
    return [row for row in raw_payload if isinstance(row, dict)]


def parse_tpex_csv_rows(raw_payload: str) -> list[dict[str, str]]:
    """Turn a TPEx t187ap06_O_ci raw payload (as saved by
    fetch_tpex_financial_statement — raw CSV text, BOM included if
    present) into rows directly consumable by
    eps_mapper.build_raw_cumulative_eps_points.

    BOM handling: strips a leading U+FEFF from the START OF THE TEXT
    only (str.lstrip("\ufeff")), not per-field — real TPEx CSV exports
    place exactly one BOM at the very beginning of the file, which
    would otherwise land inside the first header cell's name
    (出表日期 -> \ufeff出表日期) and make eps_mapper treat every row as
    missing that field. This must run BEFORE csv.DictReader sees the
    text, since DictReader has no BOM awareness of its own when fed a
    plain str (its BOM handling only applies to newline/encoding
    detection on a byte stream opened with encoding="utf-8-sig",
    which does not apply here since httpx already decoded this to
    str).

    Blank-row handling: a trailing blank line in the source (common in
    these exports) still yields a DictReader row with every value
    None or ""; such rows are dropped here rather than being passed
    downstream and relying on eps_mapper to drop them for missing
    fields, since a fully-blank row isn't a malformed DATA row, it's
    not a row at all.

    Ragged-row handling: DictReader maps a short row's missing trailing
    columns to None (not KeyError) and stashes any extra columns under
    the None key. Values are coalesced to "" (never None) so
    downstream code that does row.get(field) or "" style checks
    behaves the same as it does for a real empty string, and the
    None-keyed overflow bucket (if any) is dropped rather than passed
    through as a phantom column.
    """
    if not raw_payload:
        return []

    text = raw_payload.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))

    rows: list[dict[str, str]] = []
    for raw_row in reader:
        row = {key: (value or "") for key, value in raw_row.items() if key is not None}
        if any(value.strip() for value in row.values()):
            rows.append(row)
    return rows
