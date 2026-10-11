import ast
import datetime as dt
import json
import logging
from pathlib import Path

import pytest

import app.reports.flex_preview as flex_preview
from app.reports.flex_builder import NOTE_CHART_UNAVAILABLE
from app.reports.flex_preview import (
    PLACEHOLDER_HERO_BASE,
    format_preview_summary,
    resolve_preview_dir,
    run_flex_preview,
    write_flex_preview,
)
from tests.test_flex_builder import make_stock, texts

DAY = dt.date(2026, 10, 1)
PNG = b"\x89PNG\r\n\x1a\nfake"


def fake_render(stock):
    return PNG


def stocks(count):
    return [
        make_stock(rank=i, stock_id=str(1000 + i), stock_name=f"S{i}")
        for i in range(1, count + 1)
    ]


def test_writes_pngs_and_carousel_json(tmp_path):
    preview = write_flex_preview(
        stocks(2), trading_date=DAY, output_dir=tmp_path, render=fake_render
    )

    assert sorted(p.name for p in tmp_path.glob("*.png")) == [
        "1001_20261001.png",
        "1002_20261001.png",
    ]
    assert (tmp_path / "1001_20261001.png").read_bytes() == PNG
    assert [c.index for c in preview.carousels] == [1]

    container = json.loads((tmp_path / "carousel_01.json").read_text("utf-8"))
    assert container["type"] == "carousel"
    hero = container["contents"][0]["hero"]
    assert hero["url"] == PLACEHOLDER_HERO_BASE + "1001_20261001.png"
    assert hero["aspectRatio"] == "4:3"
    assert hero["aspectMode"] == "fit"

    message = json.loads((tmp_path / "message_01.json").read_text("utf-8"))
    assert message["type"] == "flex"
    assert message["altText"] == "每日漲停股量化觀察 10/01｜2 檔"
    assert message["contents"] == container


def test_preview_hosts_are_placeholders_that_cannot_resolve():
    assert PLACEHOLDER_HERO_BASE.startswith("https://")
    assert ".invalid/" in PLACEHOLDER_HERO_BASE


def test_one_chart_failing_keeps_the_other_and_is_logged(tmp_path, caplog):
    def render(stock):
        if stock.stock_id == "1001":
            raise RuntimeError("no font")
        return PNG

    with caplog.at_level(logging.ERROR, logger="flex_preview"):
        preview = write_flex_preview(
            stocks(2), trading_date=DAY, output_dir=tmp_path, render=render
        )

    assert preview.render_failed_stock_ids == ("1001",)
    assert list(preview.png_paths) == ["1002"]
    assert not (tmp_path / "1001_20261001.png").exists()
    assert "chart_render_failed stock_id=1001 report_date=2026-10-01" in caplog.text

    bubbles = json.loads((tmp_path / "carousel_01.json").read_text("utf-8"))["contents"]
    assert "hero" not in bubbles[0]
    assert NOTE_CHART_UNAVAILABLE in texts(bubbles[0])
    assert "hero" in bubbles[1]


def test_stock_without_chart_data_is_reported_not_logged(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        preview = write_flex_preview(
            [make_stock(stock_id="1001", chart_data=None)],
            trading_date=DAY,
            output_dir=tmp_path,
            render=fake_render,
        )
    assert preview.no_chart_data_stock_ids == ("1001",)
    assert preview.png_paths == {}
    assert caplog.text == ""


def test_more_than_twelve_stocks_make_several_carousel_files(tmp_path):
    preview = write_flex_preview(
        stocks(13), trading_date=DAY, output_dir=tmp_path, render=fake_render
    )
    assert [len(c.stock_ids) for c in preview.carousels] == [12, 1]
    assert (tmp_path / "carousel_02.json").exists()
    assert (tmp_path / "message_02.json").exists()


def test_a_smaller_rerun_removes_stale_carousel_files(tmp_path):
    write_flex_preview(
        stocks(13), trading_date=DAY, output_dir=tmp_path, render=fake_render
    )
    write_flex_preview(
        stocks(2), trading_date=DAY, output_dir=tmp_path, render=fake_render
    )
    assert sorted(p.name for p in tmp_path.glob("carousel_*.json")) == [
        "carousel_01.json"
    ]
    assert sorted(p.name for p in tmp_path.glob("message_*.json")) == [
        "message_01.json"
    ]


def test_unsafe_stock_id_cannot_escape_the_output_directory(tmp_path):
    out = tmp_path / "out"
    write_flex_preview(
        [make_stock(stock_id="../evil")],
        trading_date=DAY,
        output_dir=out,
        render=fake_render,
    )
    assert [p.name for p in out.glob("*.png")] == ["___evil_20261001.png"]
    assert not (tmp_path / "evil_20261001.png").exists()


def test_summary_reports_sizes_against_the_line_limit(tmp_path):
    preview = write_flex_preview(
        stocks(2), trading_date=DAY, output_dir=tmp_path, render=fake_render
    )
    text = "\n".join(format_preview_summary(preview))
    assert "/ 50000 bytes" in text
    assert "每日漲停股量化觀察 10/01｜2 檔" in text
    assert "placeholders" in text
    assert "nothing was sent to LINE" in text


# ---- run_flex_preview: never raises -------------------------------------------


def collect():
    lines: list[str] = []
    return lines, lines.append


def test_run_flex_preview_prints_header_and_summary(tmp_path):
    lines, printer = collect()
    preview = run_flex_preview(
        stocks(1),
        trading_date=DAY,
        output_dir=tmp_path,
        printer=printer,
        render=fake_render,
    )
    out = "\n".join(lines)
    assert preview is not None
    assert "REPORT_DRY_RUN Flex preview" in out
    assert "NOT uploaded to GCS / NOT sent to LINE / NOT written to delivery DB" in out
    assert "Chart PNGs: 1" in out


def test_run_flex_preview_with_no_stock_says_text_only(tmp_path):
    lines, printer = collect()
    assert (
        run_flex_preview([], trading_date=DAY, output_dir=tmp_path, printer=printer)
        is None
    )
    assert "plain-text report only" in "\n".join(lines)
    assert list(tmp_path.iterdir()) == []


def test_run_flex_preview_survives_an_unbuildable_flex(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("app.reports.flex_builder.MAX_FLEX_BYTES", 100)
    lines, printer = collect()
    with caplog.at_level(logging.ERROR, logger="flex_preview"):
        result = run_flex_preview(
            stocks(1),
            trading_date=DAY,
            output_dir=tmp_path,
            printer=printer,
            render=fake_render,
        )
    assert result is None
    assert "flex_build_failed report_date=2026-10-01 stock_count=1" in caplog.text
    assert "fall back to the plain-text report" in "\n".join(lines)


def test_run_flex_preview_survives_an_unwritable_directory(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    lines, printer = collect()
    assert (
        run_flex_preview(
            stocks(1),
            trading_date=DAY,
            output_dir=blocker / "sub",
            printer=printer,
            render=fake_render,
        )
        is None
    )
    assert "fall back to the plain-text report" in "\n".join(lines)


# ---- configuration + layering --------------------------------------------------


def test_preview_dir_defaults_and_follows_the_environment():
    assert resolve_preview_dir({}) == Path("flex_preview")
    assert resolve_preview_dir({"REPORT_PREVIEW_DIR": "  "}) == Path("flex_preview")
    assert resolve_preview_dir({"REPORT_PREVIEW_DIR": "out/x"}) == Path("out/x")


@pytest.mark.parametrize(
    "forbidden", ["app.storage", "app.clients", "app.delivery", "httpx"]
)
def test_preview_module_has_no_upload_or_transport_imports(forbidden):
    tree = ast.parse(Path(flex_preview.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [node])
        if (node.module if isinstance(node, ast.ImportFrom) else alias.name)
    }
    assert not [m for m in imported if m.startswith(forbidden)]
