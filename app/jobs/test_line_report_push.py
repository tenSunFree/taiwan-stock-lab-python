"""
Sends a real LINE push using the production report renderer, with
fixture data — no database write, no real market data fetch. Lets you
eyeball exactly what the final report will look like on a phone
before wiring this into the daily job.

Requires LINE_CHANNEL_ACCESS_TOKEN and LINE_TARGET_ID (see .env or
shell environment).

Run: python -m app.jobs.test_line_report_push
"""

from __future__ import annotations

import datetime as dt
import os
import sys

from app.clients.line_client import (
    LineMessagingClient,
    LineNonRetryableError,
    LinePushError,
)
from app.jobs.test_line_push import load_env_file
from app.reports.text_renderer import ReportStockView, render_daily_report


def main() -> int:
    load_env_file()

    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    target_id = os.environ.get("LINE_TARGET_ID")
    if not token or not target_id:
        print(
            "ERROR: LINE_CHANNEL_ACCESS_TOKEN / LINE_TARGET_ID not set", file=sys.stderr
        )
        return 1

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

    report = render_daily_report(
        trading_date=dt.date.today(),
        data_updated_at=dt.datetime.now().strftime("%H:%M"),
        total_limit_up_count=18,
        qualified_count=12,
        strategy_version="rule-v1.0.0",
        ranked_stocks=stocks,
    )

    print("----- report preview -----")
    print(report)
    print("---------------------------")

    client = LineMessagingClient(channel_access_token=token)
    try:
        result = client.push_text(target_id=target_id, text=report)
    except (LineNonRetryableError, LinePushError) as exc:
        print(f"LINE push failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"success={result.success} status={result.status_code} request_id={result.request_id}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
