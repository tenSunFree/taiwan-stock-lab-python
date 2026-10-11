"""
REPORT_DRY_RUN preview of the Flex carousel (no network, no side effect).

A dry run must not touch the outside world, so this module never uploads
anything and never talks to LINE. It does the part of the production path
that is local and deterministic:

    1. render each stock's K-line chart to a PNG file in the preview
       directory (open them to check the chart itself);
    2. build the exact Flex carousel(s) production would send, with
       PLACEHOLDER hero URLs (`https://preview.invalid/charts/<file>`)
       standing in for the hosted images;
    3. write each carousel as JSON — `carousel_NN.json` (the container,
       paste it into LINE's Flex Message Simulator) and `message_NN.json`
       (the full message object, `type/altText/contents`);
    4. print a short summary: sizes against LINE's limits, which stocks
       got a chart, which did not.

The placeholder host ends in `.invalid` (RFC 6761), so a preview URL can
never resolve by accident: in the simulator the hero shows as a broken
image, which is expected — the PNG next to the JSON is the real chart.

Failure policy matches production: one stock's chart failing is logged as
`chart_render_failed` and that bubble is previewed without a hero; a
Flex that cannot be built is logged as `flex_build_failed` and the
summary says production would fall back to the text report. Neither
raises — a preview problem must not fail a dry run.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from app.charts.stock_chart_renderer import render_stock_chart_png
from app.reports.flex_builder import (
    MAX_BUBBLES_PER_CAROUSEL,
    MAX_FLEX_BYTES,
    build_flex_carousels,
)
from app.reports.text_renderer import ReportStockView

logger = logging.getLogger("flex_preview")

PREVIEW_DIR_ENV = "REPORT_PREVIEW_DIR"
DEFAULT_PREVIEW_DIR = "flex_preview"
PLACEHOLDER_HERO_BASE = "https://preview.invalid/charts/"

_UNSAFE_FILENAME_CHARS = re.compile(r"[^0-9A-Za-z_-]")


@dataclass(frozen=True)
class CarouselPreview:
    index: int
    alt_text: str
    size_bytes: int
    stock_ids: tuple[str, ...]
    container_path: Path
    message_path: Path


@dataclass(frozen=True)
class FlexPreview:
    output_dir: Path
    carousels: tuple[CarouselPreview, ...]
    png_paths: dict[str, Path] = field(default_factory=dict)
    # stocks that have chart data but whose PNG could not be drawn
    render_failed_stock_ids: tuple[str, ...] = ()
    # stocks with no valid chart series at all ("K 線資料不足")
    no_chart_data_stock_ids: tuple[str, ...] = ()


def _png_filename(stock_id: str, trading_date: dt.date) -> str:
    safe_id = _UNSAFE_FILENAME_CHARS.sub("_", stock_id)
    return f"{safe_id}_{trading_date:%Y%m%d}.png"


def _clear_previous_json(output_dir: Path) -> None:
    """A rerun that produces fewer carousels must not leave a stale
    carousel_02.json behind that looks like part of this run."""
    for pattern in ("carousel_*.json", "message_*.json"):
        for stale in output_dir.glob(pattern):
            stale.unlink()


def _write_json(path: Path, document: dict) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_flex_preview(
    stocks: Sequence[ReportStockView],
    *,
    trading_date: dt.date,
    output_dir: Path,
    render: Callable[[ReportStockView], bytes] = render_stock_chart_png,
) -> FlexPreview:
    """Write PNGs + carousel JSON into `output_dir` and describe them.

    Raises FlexBuildError (from build_flex_carousels) when no valid Flex
    can be built; per-stock chart failures never raise.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    _clear_previous_json(output_dir)

    png_paths: dict[str, Path] = {}
    hero_urls: dict[str, str] = {}
    render_failed: list[str] = []
    no_chart_data: list[str] = []

    for stock in stocks:
        if stock.chart_data is None:
            no_chart_data.append(stock.stock_id)
            continue
        filename = _png_filename(stock.stock_id, trading_date)
        try:
            png = render(stock)
            (output_dir / filename).write_bytes(png)
        except Exception:  # noqa: BLE001 - one chart must not stop the preview
            logger.exception(
                "chart_render_failed stock_id=%s report_date=%s",
                stock.stock_id,
                trading_date,
            )
            render_failed.append(stock.stock_id)
            continue
        png_paths[stock.stock_id] = output_dir / filename
        hero_urls[stock.stock_id] = PLACEHOLDER_HERO_BASE + filename

    carousels = build_flex_carousels(
        stocks, trading_date=trading_date, hero_urls=hero_urls
    )

    previews: list[CarouselPreview] = []
    for index, carousel in enumerate(carousels, start=1):
        container_path = output_dir / f"carousel_{index:02d}.json"
        message_path = output_dir / f"message_{index:02d}.json"
        _write_json(container_path, carousel.contents)
        # Same shape as app.clients.line_client.build_flex_message; written out
        # here so this module stays free of transport imports.
        message = {
            "type": "flex",
            "altText": carousel.alt_text,
            "contents": carousel.contents,
        }
        _write_json(message_path, message)
        previews.append(
            CarouselPreview(
                index=index,
                alt_text=carousel.alt_text,
                size_bytes=len(
                    json.dumps(carousel.contents, ensure_ascii=False).encode("utf-8")
                ),
                stock_ids=carousel.stock_ids,
                container_path=container_path,
                message_path=message_path,
            )
        )

    return FlexPreview(
        output_dir=output_dir,
        carousels=tuple(previews),
        png_paths=png_paths,
        render_failed_stock_ids=tuple(render_failed),
        no_chart_data_stock_ids=tuple(no_chart_data),
    )


def format_preview_summary(preview: FlexPreview) -> list[str]:
    lines = [
        f"Flex carousels: {len(preview.carousels)} "
        f"(max {MAX_BUBBLES_PER_CAROUSEL} bubbles / {MAX_FLEX_BYTES} bytes each)",
    ]
    for carousel in preview.carousels:
        lines.append(
            f"  #{carousel.index} {len(carousel.stock_ids)} bubble(s), "
            f"{carousel.size_bytes} / {MAX_FLEX_BYTES} bytes, "
            f"altText={carousel.alt_text!r}"
        )
        lines.append(f"     {carousel.container_path}")
        lines.append(f"     {carousel.message_path}")
    lines.append(f"Chart PNGs: {len(preview.png_paths)} in {preview.output_dir}")
    if preview.render_failed_stock_ids:
        lines.append(
            "  chart_render_failed (bubble previewed without hero): "
            + ", ".join(preview.render_failed_stock_ids)
        )
    if preview.no_chart_data_stock_ids:
        lines.append(
            "  K 線資料不足 (no chart drawn): "
            + ", ".join(preview.no_chart_data_stock_ids)
        )
    lines.append(
        "Hero URLs in the JSON are placeholders (preview.invalid) — nothing was "
        "uploaded and nothing was sent to LINE."
    )
    return lines


def resolve_preview_dir(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get(PREVIEW_DIR_ENV, "").strip()
    return Path(configured or DEFAULT_PREVIEW_DIR)


def run_flex_preview(
    stocks: Sequence[ReportStockView],
    *,
    trading_date: dt.date,
    output_dir: Path | None = None,
    printer: Callable[[str], None] = print,
    render: Callable[[ReportStockView], bytes] = render_stock_chart_png,
) -> FlexPreview | None:
    """The dry-run entry point: write the preview and print its summary.

    Never raises. Returns None when there is nothing to preview or the
    Flex could not be built (the text report would be sent instead).
    """
    printer("=" * 72)
    printer("REPORT_DRY_RUN Flex preview")
    printer("NOT uploaded to GCS / NOT sent to LINE / NOT written to delivery DB")
    printer("=" * 72)

    if not stocks:
        printer(
            "No qualified stock: production sends the plain-text report only (no Flex)."
        )
        printer("=" * 72)
        return None

    try:
        preview = write_flex_preview(
            stocks,
            trading_date=trading_date,
            output_dir=output_dir or resolve_preview_dir(),
            render=render,
        )
    except Exception:  # noqa: BLE001 - a preview problem must not fail a dry run
        logger.exception(
            "flex_build_failed report_date=%s stock_count=%d",
            trading_date,
            len(stocks),
        )
        printer(
            "Flex could not be built: production would fall back to the "
            "plain-text report (see the log for flex_build_failed)."
        )
        printer("=" * 72)
        return None

    for line in format_preview_summary(preview):
        printer(line)
    printer("=" * 72)
    return preview
