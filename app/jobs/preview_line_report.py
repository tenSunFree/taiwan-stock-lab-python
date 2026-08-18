"""
Local preview script — renders a report from fixture data so you can
eyeball the exact text before any LINE token exists.

Run: python -m app.jobs.preview_line_report
"""

from __future__ import annotations

import datetime as dt

from app.reports.text_renderer import ReportStockView, render_daily_report

TAIPEI_TZ = dt.timezone(dt.timedelta(hours=8))


def main() -> None:
    stocks = [
        ReportStockView(
            rank=1,
            stock_id="1234",
            stock_name="範例公司 A",
            total_score=84.2,
            data_completeness=0.96,
            top_factor_names=("流動性", "基本面"),
            risk_flags=("HIGH_FIVE_DAY_RETURN",),
        ),
        ReportStockView(
            rank=2,
            stock_id="5678",
            stock_name="範例公司 B",
            total_score=80.4,
            data_completeness=0.91,
            top_factor_names=("法人籌碼", "流動性"),
            risk_flags=(),
        ),
    ]

    now_taipei = dt.datetime.now(TAIPEI_TZ)

    report = render_daily_report(
        trading_date=now_taipei.date(),
        data_updated_at=now_taipei.strftime("%H:%M"),
        candidate_count=18,
        eligible_count=12,
        strategy_version="rule-v1.0.0",
        ranked_stocks=stocks,
    )

    print(report)


if __name__ == "__main__":
    main()
