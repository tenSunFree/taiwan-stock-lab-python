"""
Prepare hero images for the Flex carousel: render each stock's K-line
chart, host it, and return stock_id -> public HTTPS URL.

Failure policy (LINE chart spec §33, §35): this function NEVER raises for
a single stock. One stock whose chart cannot be drawn or uploaded is
simply absent from the result, and the Flex builder gives that stock a
bubble without a hero image. One failure never affects another stock and
never fails the push.

Logging categories (one line per failing stock, greppable):
    chart_render_failed   the chart could not be drawn
    image_upload_failed   hosting failed (logged by the uploader itself
                          for the failures it knows; unexpected errors
                          are logged here under the same category)

Layering: this module is the only place that connects the pure renderer
(app.charts) with the storage layer (app.storage). Neither of those knows
about the other, and neither knows about Flex.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Callable, Sequence

from app.charts.stock_chart_renderer import render_stock_chart_png
from app.reports.text_renderer import ReportStockView
from app.storage.chart_image_uploader import (
    ChartImageUploader,
    ChartImageUploadError,
)

logger = logging.getLogger("chart_images")


def prepare_chart_heroes(
    stocks: Sequence[ReportStockView],
    *,
    trading_date: dt.date,
    uploader: ChartImageUploader | None,
    render: Callable[[ReportStockView], bytes] = render_stock_chart_png,
) -> dict[str, str]:
    """stock_id -> hosted chart URL, for the stocks whose chart made it.

    uploader None (image hosting not configured) -> {} without rendering
    anything: a chart nobody can display is not worth drawing.
    """
    if uploader is None:
        return {}

    heroes: dict[str, str] = {}
    for stock in stocks:
        if stock.chart_data is None:
            continue  # no valid series -> "K 線資料不足" bubble, nothing to log

        try:
            png = render(stock)
        except Exception:  # noqa: BLE001 - ChartRenderError and anything unexpected
            logger.exception(
                "chart_render_failed stock_id=%s report_date=%s",
                stock.stock_id,
                trading_date,
            )
            continue

        try:
            uploaded = uploader.upload_chart(
                stock_id=stock.stock_id, trading_date=trading_date, png_bytes=png
            )
        except ChartImageUploadError:
            continue  # already logged as image_upload_failed by the uploader
        except Exception:  # noqa: BLE001 - never let one stock break the push
            logger.exception(
                "image_upload_failed stock_id=%s report_date=%s",
                stock.stock_id,
                trading_date,
            )
            continue

        heroes[stock.stock_id] = uploaded.url
    return heroes
