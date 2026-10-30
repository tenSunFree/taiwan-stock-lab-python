"""
Developer preview: draw the 明細 image from made-up data so the layout can
be checked by eye before anything is wired into the pipeline.

    python -m app.charts.institutional_detail_preview out/detail.png
    python -m app.charts.institutional_detail_preview out/detail_zero.png --scenario quiet

The numbers are fabricated and exist only for this preview.
"""

from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import replace
from pathlib import Path

from app.charts.institutional_detail_renderer import render_institutional_detail_png
from app.domain.institutional_detail import (
    InstitutionalDayLots,
    InstitutionalDetail10D,
)
from app.reports.text_renderer import ReportStockView

_SCENARIOS: dict[str, list[tuple[int, int, int]]] = {
    # (外資, 投信, 自營商) in lots, oldest -> newest
    "mixed": [
        (320, 40, -15),
        (850, 60, 20),
        (-120, 80, -30),
        (-640, 120, 45),
        (1500, 210, -10),
        (2100, 330, 60),
        (1750, 280, -90),
        (3200, 410, 120),
        (2630, 450, -110),
        (2480, 520, -80),
    ],
    "quiet": [(0, 0, 0)] * 9 + [(3, -1, 0)],
}


def _weekday_sessions(count: int, *, last: dt.date) -> list[dt.date]:
    """Synthetic sessions: the `count` weekdays ending at `last`. Holidays
    are ignored — production dates come from the real price-history
    calendar; this only keeps the preview from showing weekends."""
    days: list[dt.date] = []
    cursor = last
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= dt.timedelta(days=1)
    return sorted(days)


def sample_detail(name: str = "mixed") -> InstitutionalDetail10D:
    values = _SCENARIOS[name]
    sessions = _weekday_sessions(len(values), last=dt.date(2026, 10, 20))
    return InstitutionalDetail10D(
        days=tuple(
            InstitutionalDayLots(trading_date=day, foreign=f, trust=t, dealer=d)
            for day, (f, t, d) in zip(sessions, values)
        )
    )


def sample_report(name: str = "mixed") -> ReportStockView:
    base = ReportStockView(
        rank=1,
        stock_id="2330",
        stock_name="台積電",
        total_score=82.5,
        data_completeness=0.9,
        top_factor_names=(),
        risk_flags=(),
    )
    return replace(base, institutional_detail_10d=sample_detail(name))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--scenario", choices=sorted(_SCENARIOS), default="mixed")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(
        render_institutional_detail_png(sample_report(args.scenario))
    )
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
