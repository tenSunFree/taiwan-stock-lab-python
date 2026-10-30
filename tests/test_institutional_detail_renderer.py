import ast
import io
from pathlib import Path

import pytest
from PIL import Image

import app.charts.institutional_detail_renderer as renderer
from app.charts.institutional_detail_preview import sample_report
from app.charts.institutional_detail_renderer import (
    DETAIL_HEIGHT_PX,
    DETAIL_WIDTH_PX,
    render_institutional_detail_png,
)
from app.charts.stock_chart_renderer import ChartRenderError
from app.reports.text_renderer import ReportStockView
from app.storage.chart_image_uploader import DETAIL_MAX_IMAGE_HEIGHT, validate_png


@pytest.mark.parametrize("scenario", ["mixed", "quiet"])
def test_renders_a_png_our_uploader_accepts(scenario):
    png = render_institutional_detail_png(sample_report(scenario))
    with Image.open(io.BytesIO(png)) as image:
        assert image.size == (DETAIL_WIDTH_PX, DETAIL_HEIGHT_PX)
    assert validate_png(png, max_height=DETAIL_MAX_IMAGE_HEIGHT) == (
        DETAIL_WIDTH_PX,
        DETAIL_HEIGHT_PX,
    )


def test_same_input_gives_identical_bytes():
    # The hosted object name is a hash of these bytes: a re-render of the
    # same data must land on the SAME name (re-run = "already exists").
    report = sample_report()
    assert render_institutional_detail_png(report) == render_institutional_detail_png(
        report
    )


def test_different_numbers_give_different_bytes():
    from dataclasses import replace

    base = sample_report("mixed")
    other = replace(
        base, institutional_detail_10d=sample_report("quiet").institutional_detail_10d
    )
    assert render_institutional_detail_png(base) != render_institutional_detail_png(
        other
    )


def test_no_detail_is_a_render_error_not_a_blank_image():
    report = ReportStockView(
        rank=1,
        stock_id="2330",
        stock_name="台積電",
        total_score=1.0,
        data_completeness=1.0,
        top_factor_names=(),
        risk_flags=(),
    )
    with pytest.raises(ChartRenderError):
        render_institutional_detail_png(report)


def test_layer_only_depends_on_the_detail_model_and_report_view():
    source = Path(renderer.__file__).read_text(encoding="utf-8")
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.")
    }
    assert imported == {
        "app.charts.stock_chart_renderer",
        "app.domain.institutional_detail",
        "app.reports.text_renderer",
    }


@pytest.mark.parametrize(
    "token",
    ["FACTOR_WEIGHTS", "institutional_net_buy_ratio", "build_institutional"],
)
def test_renderer_never_mentions_scoring_or_builders(token):
    assert token not in Path(renderer.__file__).read_text(encoding="utf-8")
