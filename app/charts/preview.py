"""
Dev tool: render sample K-line charts from SYNTHETIC data so the chart can
be eyeballed without running the daily job or touching LINE.

    python -m app.charts.preview --out chart_preview

For every scenario it writes two files:
    <name>.png        the real 1024x768 output
    <name>_phone.png  the same image shrunk to ~300px wide, approximating
                      how a LINE carousel bubble shows it. Spec §8/§38:
                      the chart is only acceptable if the name, 漲停 badge,
                      marker, dates, 20D lines and volume are still legible
                      in THIS version (and later on a real phone).

Data is deterministic (seeded) and contains no real market information.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
from pathlib import Path

from PIL import Image

from app.charts.stock_chart_renderer import render_stock_chart_png
from app.domain.chart_data import build_stock_chart_data
from app.domain.feature_builder import HistoricalPricePoint
from app.domain.signal_status import SignalStatus
from app.reports.text_renderer import ReportStockView

_TRUE = SignalStatus.TRUE
_FALSE = SignalStatus.FALSE
_UNKNOWN = SignalStatus.INSUFFICIENT_DATA


def _weekdays(count: int, *, end: dt.date) -> list[dt.date]:
    days: list[dt.date] = []
    current = end
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current -= dt.timedelta(days=1)
    return list(reversed(days))


def _synthetic_history(
    *,
    sessions: int,
    start_price: float,
    drift: float,
    volatility: float,
    base_volume: float,
    seed: int,
    end: dt.date,
) -> list[HistoricalPricePoint]:
    rng = random.Random(seed)
    price = start_price
    points: list[HistoricalPricePoint] = []
    for day in _weekdays(sessions, end=end):
        open_price = price * (1 + rng.uniform(-0.006, 0.006))
        close = open_price * (1 + drift + rng.gauss(0, volatility))
        high = max(open_price, close) * (1 + abs(rng.gauss(0, volatility / 2)))
        low = min(open_price, close) * (1 - abs(rng.gauss(0, volatility / 2)))
        volume = base_volume * rng.uniform(0.6, 1.5)
        points.append(
            HistoricalPricePoint(
                trading_date=day,
                open=round(open_price, 2),
                high=round(high, 2),
                low=round(low, 2),
                close=round(close, 2),
                volume=float(round(volume)),
                turnover=float(round(volume * close)),
            )
        )
        price = close
    return points


def _today(
    prev: HistoricalPricePoint, day: dt.date, *, one_price: bool, vol_mult: float
):
    # SYNTHETIC preview data only. This is deliberately NOT a legal limit-up
    # price calculation (that lives in app.domain.price_ticks) — it is just a
    # big up-move that makes the chart's limit-up styling easy to eyeball.
    preview_close = round(prev.close * 1.098, 2)
    if one_price:
        open_price = high = low = close = preview_close
    else:
        open_price = round(prev.close * 1.03, 2)
        close = high = preview_close
        low = round(prev.close * 1.02, 2)
    volume = float(round(prev.volume * vol_mult))
    return HistoricalPricePoint(
        trading_date=day,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        turnover=volume * close,
    )


def _scenario(
    *,
    name: str,
    stock_id: str,
    sessions: int,
    start_price: float,
    drift: float,
    seed: int,
    one_price: bool = False,
    vol_mult: float = 9.0,
    first_board: SignalStatus = _FALSE,
    overheated: SignalStatus = _FALSE,
    limit_up: SignalStatus = _TRUE,
):
    today_date = dt.date(2026, 10, 1)
    history = _synthetic_history(
        sessions=sessions,
        start_price=start_price,
        drift=drift,
        volatility=0.018,
        base_volume=2_000_000,
        seed=seed,
        end=today_date - dt.timedelta(days=1),
    )
    today = _today(history[-1], today_date, one_price=one_price, vol_mult=vol_mult)
    chart = build_stock_chart_data(history=history, today=today)
    return ReportStockView(
        rank=1,
        stock_id=stock_id,
        stock_name=name,
        total_score=73.61,
        data_completeness=0.9,
        top_factor_names=(),
        risk_flags=(),
        chart_data=chart,
        limit_up_status=limit_up,
        low_level_first_limit_up_status=first_board,
        momentum_overheated_status=overheated,
    )


SCENARIOS = {
    "01_low_first_board": dict(
        name="撼訊",
        stock_id="6150",
        sessions=119,
        start_price=95,
        drift=-0.006,
        seed=3,
        first_board=_TRUE,
    ),
    "02_overheated_beats_first_board": dict(
        name="創意電子",
        stock_id="3443",
        sessions=119,
        start_price=40,
        drift=0.011,
        seed=8,
        first_board=_TRUE,
        overheated=_TRUE,
    ),
    "03_no_marker": dict(
        name="南亞科",
        stock_id="2408",
        sessions=119,
        start_price=60,
        drift=0.001,
        seed=21,
    ),
    "04_short_history": dict(
        name="新上市股份有限公司",
        stock_id="7799",
        sessions=34,
        start_price=30,
        drift=0.002,
        seed=5,
    ),
    "05_one_price_limit_up_high_price": dict(
        name="台積電",
        stock_id="2330",
        sessions=119,
        start_price=1180,
        drift=0.002,
        seed=13,
        one_price=True,
        vol_mult=14.0,
    ),
    "06_insufficient_marker_data": dict(
        name="聯發科",
        stock_id="2454",
        sessions=119,
        start_price=1400,
        drift=-0.002,
        seed=17,
        first_board=_UNKNOWN,
        overheated=_UNKNOWN,
    ),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--out", type=Path, default=Path("chart_preview"))
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    for key, params in SCENARIOS.items():
        report = _scenario(**params)
        data = render_stock_chart_png(report)
        path = args.out / f"{key}.png"
        path.write_bytes(data)
        with Image.open(path) as image:
            phone = image.resize((300, 225), Image.Resampling.LANCZOS)
            phone.save(args.out / f"{key}_phone.png")
        print(f"{key}: {len(data) / 1024:.0f} KB -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
