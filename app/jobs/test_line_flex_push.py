"""
Manual end-to-end check of the Flex carousel, on ONE LINE account.

    python -m app.jobs.test_line_flex_push            # render + upload + push
    python -m app.jobs.test_line_flex_push --no-send  # everything but the LINE push

Why this exists: the daily pipeline only produces a Flex card on a day when
at least one stock qualifies, so a real-device check cannot be scheduled.
This tool feeds three deterministic SYNTHETIC stocks (E2E001..E2E003,
names prefixed 【測試】) through the PRODUCTION path:

    synthetic ReportStockView + StockChartData
      -> render_stock_chart_png        (production chart renderer)
      -> ChartImageUploader -> REAL GCS (production uploader, CHART_GCS_BUCKET)
      -> anonymous GET of every hero URL
      -> build_flex_carousels          (production Flex builder)
      -> LineMessagingClient.push_flex -> ONLY LINE_TARGET_ID

What it deliberately is NOT:
  * not a market-data run: the stocks are fake, so it says nothing about
    candidate selection, P/E, RiskPolicy or scoring;
  * not idempotent: it never touches the delivery database (no DATABASE_URL),
    so every run sends a new message — which is also why it can never
    affect the real broadcast's de-duplication;
  * never a broadcast: it only ever pushes to LINE_TARGET_ID.

FAIL-FAST, unlike production. Production degrades on purpose (a chart that
fails -> a card without a hero; a Flex that fails -> a text report). Here
that would hide exactly what is being tested, so ANY missing chart, any hero
that cannot be fetched, or any Flex problem is a FAIL and a non-zero exit.

Needs (environment or .env, never committed, never pasted into chat):
    LINE_CHANNEL_ACCESS_TOKEN, LINE_TARGET_ID   (not needed with --no-send)
    CHART_GCS_BUCKET   (+ optional CHART_PUBLIC_BASE_URL)
    Application Default Credentials whose identity may CREATE objects in the
    bucket (`gcloud auth application-default login` is not enough by itself).

Side effect: a few small objects under charts/YYYY/MM/DD/E2E00N_*.png.
Production's service account cannot delete them; remove them as an admin.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sys
from decimal import Decimal
from typing import Callable, Sequence

import httpx

from app.clients.line_client import LineMessagingClient, LinePushError
from app.domain.chart_data import build_stock_chart_data
from app.domain.feature_builder import HistoricalPricePoint
from app.domain.signal_status import SignalStatus
from app.jobs.test_line_push import load_env_file
from app.reports.chart_images import prepare_chart_heroes
from app.reports.flex_builder import FlexBuildError, build_flex_carousels
from app.reports.text_renderer import ReportStockView
from app.storage.chart_image_uploader import ChartImageUploader, build_uploader_from_env
from app.storage.smoke import Check, format_report

HISTORY_SESSIONS = 119  # + today = 120, the production fetch size
TAIPEI = dt.timezone(dt.timedelta(hours=8))

TRUE = SignalStatus.TRUE
FALSE = SignalStatus.FALSE


# --- deterministic synthetic data ------------------------------------------


def _weekdays_before(day: dt.date, count: int) -> list[dt.date]:
    days: list[dt.date] = []
    current = day - dt.timedelta(days=1)
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current -= dt.timedelta(days=1)
    days.reverse()
    return days


def _history(
    trading_date: dt.date, close_at: Callable[[int, int], float]
) -> list[HistoricalPricePoint]:
    days = _weekdays_before(trading_date, HISTORY_SESSIONS)
    points: list[HistoricalPricePoint] = []
    for i, day in enumerate(days):
        close = round(close_at(i, len(days)), 2)
        open_ = round(close - 1.6 * math.cos(i / 2), 2)
        volume = round(1_000_000 * (1 + 0.3 * math.sin(i / 4)))
        points.append(
            HistoricalPricePoint(
                trading_date=day,
                open=open_,
                high=round(max(open_, close) + 0.8, 2),
                low=round(min(open_, close) - 0.8, 2),
                close=close,
                volume=float(volume),
                turnover=float(volume * close),
            )
        )
    return points


def _falling(i: int, n: int) -> float:  # long slide to a low: 低檔首板
    return 120 - 40 * (i / (n - 1)) + 1.5 * math.sin(i / 3)


def _rallying(i: int, n: int) -> float:  # steady climb + a hot last week: 漲多過熱
    price = 60 + 30 * (i / (n - 1)) + 1.2 * math.sin(i / 3)
    return price + max(0, i - (n - 6)) * 2.5


def _sideways(i: int, n: int) -> float:  # range-bound: plain (one-price) limit-up
    return 50 + 4 * math.sin(i / 5)


def _stock(
    *,
    rank: int,
    suffix: str,
    name: str,
    score: float,
    trading_date: dt.date,
    close_at: Callable[[int, int], float],
    one_price: bool = False,
    low_level: SignalStatus = FALSE,
    first_board: SignalStatus = FALSE,
    overheated: SignalStatus = FALSE,
    risk_flags: tuple[str, ...] = (),
    missing: tuple[str, ...] = ("is_managed",),
) -> ReportStockView:
    history = _history(trading_date, close_at)
    prev = history[-1].close
    close = round(prev * 1.098, 2)
    if one_price:
        open_ = high = low = close
    else:
        open_, high, low = round(prev * 1.03, 2), close, round(prev * 1.02, 2)
    today = HistoricalPricePoint(
        trading_date=trading_date,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=9_000_000.0,
        turnover=9_000_000.0 * close,
    )
    chart = build_stock_chart_data(history=history, today=today)
    if chart is None:
        raise RuntimeError(f"synthetic chart for E2E{suffix} is not drawable")
    closes = [p.close for p in history]
    return ReportStockView(
        rank=rank,
        stock_id=f"E2E{suffix}",
        stock_name=name,
        total_score=score,
        data_completeness=0.9,
        top_factor_names=(),
        risk_flags=risk_flags,
        close_price=Decimal(str(close)),
        change_percent=round((close / prev - 1) * 100, 2),
        volume_ratio_20d=9.0,
        return_5d=round(close / closes[-5] - 1, 4),
        is_one_price_limit_up=one_price,
        risk_missing_inputs=missing,
        chart_data=chart,
        limit_up_status=TRUE,
        low_level_status=low_level,
        low_level_first_limit_up_status=first_board,
        momentum_overheated_status=overheated,
    )


def make_fixture_stocks(trading_date: dt.date) -> list[ReportStockView]:
    """Three stocks covering the three chart markers' situations."""
    return [
        _stock(
            rank=1,
            suffix="001",
            name="【測試】模擬A",
            score=82.5,
            trading_date=trading_date,
            close_at=_falling,
            low_level=TRUE,
            first_board=TRUE,
            missing=("is_managed",),
        ),
        _stock(
            rank=2,
            suffix="002",
            name="【測試】模擬B",
            score=76.3,
            trading_date=trading_date,
            close_at=_rallying,
            overheated=TRUE,
            risk_flags=("HIGH_FIVE_DAY_RETURN",),
            missing=("is_attention", "is_managed"),
        ),
        _stock(
            rank=3,
            suffix="003",
            name="【測試】模擬C",
            score=70.1,
            trading_date=trading_date,
            close_at=_sideways,
            one_price=True,
            missing=(),
        ),
    ]


# --- the end-to-end run -------------------------------------------------------


def _default_get(url: str) -> httpx.Response:
    return httpx.get(url, timeout=30.0, follow_redirects=False)


def run_e2e(
    *,
    uploader: ChartImageUploader,
    trading_date: dt.date,
    client: LineMessagingClient | None = None,
    target_id: str | None = None,
    http_get: Callable[[str], httpx.Response] = _default_get,
    stocks: Sequence[ReportStockView] | None = None,
) -> tuple[list[Check], dict[str, str]]:
    """Run every step; stop at the first failed one. client None = no send.

    Returns (checks, hero_urls). Never raises for an expected failure.
    """
    checks: list[Check] = []
    heroes: dict[str, str] = {}

    def fail(name: str, detail: str = "") -> tuple[list[Check], dict[str, str]]:
        checks.append(Check(name, False, detail))
        return checks, heroes

    try:
        stocks = (
            list(stocks) if stocks is not None else make_fixture_stocks(trading_date)
        )
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        return fail("build synthetic stocks", repr(exc))
    checks.append(Check("build synthetic stocks", True, f"{len(stocks)} stocks"))

    heroes = prepare_chart_heroes(stocks, trading_date=trading_date, uploader=uploader)
    missing = [s.stock_id for s in stocks if s.stock_id not in heroes]
    if missing:
        return fail(
            "every chart rendered and uploaded",
            "missing hero for " + ", ".join(missing) + " (see chart_render_failed / "
            "image_upload_failed lines above)",
        )
    checks.append(
        Check("every chart rendered and uploaded", True, f"{len(heroes)} hero URLs")
    )

    for stock_id, url in heroes.items():
        try:
            response = http_get(url)
        except httpx.HTTPError as exc:
            return fail(f"anonymous GET {stock_id}", repr(exc))
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        if response.status_code != 200 or content_type != "image/png":
            return fail(
                f"anonymous GET {stock_id}",
                f"HTTP {response.status_code}, content-type {content_type!r}",
            )
    checks.append(
        Check("every hero URL is publicly fetchable (200, image/png)", True, "")
    )

    try:
        carousels = build_flex_carousels(
            stocks, trading_date=trading_date, hero_urls=heroes
        )
    except FlexBuildError as exc:
        return fail("build Flex carousel", str(exc))
    bubbles = [b for c in carousels for b in c.contents.get("contents", [])]
    hero_ok = all(b.get("hero", {}).get("url") in heroes.values() for b in bubbles)
    if (
        not carousels
        or any(c.contents.get("type") != "carousel" for c in carousels)
        or len(bubbles) != len(stocks)
        or not hero_ok
    ):
        return fail(
            "Flex carousel has one bubble with a hosted hero per stock",
            f"carousels={len(carousels)} bubbles={len(bubbles)} hero_ok={hero_ok}",
        )
    checks.append(
        Check(
            "Flex carousel has one bubble with a hosted hero per stock",
            True,
            f"{len(carousels)} carousel(s), {len(bubbles)} bubbles",
        )
    )

    if client is None or not target_id:
        checks.append(Check("LINE push", True, "skipped (--no-send)"))
        return checks, heroes

    for number, carousel in enumerate(carousels, start=1):
        try:
            result = client.push_flex(
                target_id=target_id,
                alt_text=carousel.alt_text,
                contents=carousel.contents,
            )
        except LinePushError as exc:
            return fail(f"LINE push flex #{number}", str(exc))
        if not result.success:
            return fail(f"LINE push flex #{number}", f"HTTP {result.status_code}")
        checks.append(
            Check(
                f"LINE push flex #{number} (type flex)",
                True,
                f"HTTP {result.status_code}, request_id={result.request_id}",
            )
        )
    return checks, heroes


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1].strip())
    parser.add_argument(
        "--no-send",
        action="store_true",
        help="render, upload and build the Flex, but do not call LINE",
    )
    parser.add_argument(
        "--date",
        type=dt.date.fromisoformat,
        default=None,
        help="trading date to stamp on the fixture (default: today, Taipei)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    load_env_file()

    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    target_id = os.environ.get("LINE_TARGET_ID", "").strip()
    if not args.no_send and not (token and target_id):
        print(
            "ERROR: LINE_CHANNEL_ACCESS_TOKEN / LINE_TARGET_ID not set "
            "(or use --no-send)",
            file=sys.stderr,
        )
        return 2

    uploader = build_uploader_from_env()
    if uploader is None:
        print(
            "ERROR: image hosting is not configured: set CHART_GCS_BUCKET to a "
            "valid bucket name (and have Application Default Credentials).",
            file=sys.stderr,
        )
        return 2

    trading_date = args.date or dt.datetime.now(TAIPEI).date()
    client = None if args.no_send else LineMessagingClient(channel_access_token=token)

    checks, heroes = run_e2e(
        uploader=uploader,
        trading_date=trading_date,
        client=client,
        target_id=target_id,
    )
    print(format_report(checks))
    ok = all(check.passed for check in checks)
    print("\nFLEX E2E " + ("PASSED" if ok else "FAILED"))
    if heroes:
        print("\nHosted charts:")
        for stock_id, url in heroes.items():
            print(f"  {stock_id}: {url}")
    if ok and client is not None:
        print("\nNow check the message on your phone (see the checklist in the docs).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
