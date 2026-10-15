import ast
import datetime as dt
import io
import os
from pathlib import Path

import pytest
from PIL import Image

import app.charts.stock_chart_renderer as renderer
from app.charts.stock_chart_renderer import (
    CHART_HEIGHT_PX,
    CHART_WIDTH_PX,
    TARGET_MAX_BYTES,
    ChartRenderError,
    render_stock_chart,
    render_stock_chart_png,
    select_chart_marker,
)
from app.domain.chart_data import build_stock_chart_data
from app.domain.feature_builder import HistoricalPricePoint
from app.domain.signal_status import SignalStatus
from app.reports.text_renderer import ReportStockView

TRUE = SignalStatus.TRUE
FALSE = SignalStatus.FALSE
UNKNOWN = SignalStatus.INSUFFICIENT_DATA


def _font_available() -> bool:
    try:
        renderer._resolve_fonts()
        return True
    except ChartRenderError:
        return False


# Rendering needs a CJK font (CI installs fonts-noto-cjk; Windows has
# Microsoft JhengHei). Outside CI, tests that draw are skipped where no font
# exists so a bare dev machine can still run the rest of the suite. IN CI they
# are NEVER skipped: a missing font then fails loudly (see test_ci_has_cjk_font)
# instead of letting ~25 rendering tests silently turn green.
_IN_CI = bool(os.environ.get("CI"))
requires_font = pytest.mark.skipif(
    not _font_available() and not _IN_CI,
    reason="no CJK font installed (apt install fonts-noto-cjk)",
)


@pytest.mark.skipif(not _IN_CI, reason="CI-only: the rendering tests must really run")
def test_ci_has_cjk_font():
    assert _font_available(), (
        "CI must install a CJK font (apt-get install fonts-noto-cjk); "
        "the chart rendering tests must not silently skip in CI"
    )


def _history(n, *, start_price=100.0, step=0.4, end=dt.date(2026, 9, 30)):
    days: list[dt.date] = []
    current = end
    while len(days) < n:
        if current.weekday() < 5:
            days.append(current)
        current -= dt.timedelta(days=1)
    days.reverse()
    points = []
    for i, day in enumerate(days):
        close = start_price + step * i * (1 if i % 7 else -1)
        points.append(
            HistoricalPricePoint(
                trading_date=day,
                open=close - 0.3,
                high=close + 1.1,
                low=close - 1.2,
                close=close,
                volume=1_000_000.0 + 1000 * i,
                turnover=1.0,
            )
        )
    return points


def _today(prev_close, *, one_price=False):
    close = round(prev_close * 1.098, 2)
    high = low = open_ = close
    if not one_price:
        open_ = round(prev_close * 1.03, 2)
        low = round(prev_close * 1.02, 2)
    return HistoricalPricePoint(
        trading_date=dt.date(2026, 10, 1),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=9_000_000.0,
        turnover=1.0,
    )


def _report(
    *,
    sessions=119,
    limit_up=TRUE,
    first_board=FALSE,
    overheated=FALSE,
    one_price=False,
    name="撼訊",
    stock_id="6150",
    chart="auto",
):
    if chart == "auto":
        history = _history(sessions)
        prev_close = history[-1].close if history else 100.0
        chart = build_stock_chart_data(
            history=history, today=_today(prev_close, one_price=one_price)
        )
    return ReportStockView(
        rank=1,
        stock_id=stock_id,
        stock_name=name,
        total_score=70.0,
        data_completeness=0.9,
        top_factor_names=(),
        risk_flags=(),
        chart_data=chart,
        limit_up_status=limit_up,
        low_level_first_limit_up_status=first_board,
        momentum_overheated_status=overheated,
    )


# --- pure presentation rules (no font needed) ----------------------------------


@pytest.mark.parametrize(
    "first_board, overheated, expected",
    [
        (TRUE, TRUE, "漲多過熱"),  # spec §22: the risk warning wins
        (FALSE, TRUE, "漲多過熱"),
        (TRUE, FALSE, "低檔首板"),
        (TRUE, UNKNOWN, "低檔首板"),
        (FALSE, FALSE, None),
        (UNKNOWN, UNKNOWN, None),  # unknown draws nothing — never a marker
        (UNKNOWN, FALSE, None),
    ],
)
def test_select_chart_marker_priority(first_board, overheated, expected):
    report = _report(chart=None, first_board=first_board, overheated=overheated)
    assert select_chart_marker(report) == expected


def test_font_sizes_meet_the_phone_legibility_minimums():
    """Spec §8 — shrinking any of these re-opens the 'unreadable in the LINE
    carousel' problem."""
    assert renderer._FS_TITLE >= 56
    assert renderer._FS_BADGE >= 48
    assert renderer._FS_LATEST >= 40
    assert renderer._FS_DATE >= 32
    assert renderer._FS_LEGEND >= 30
    assert renderer._FS_TICK >= 28
    assert renderer._FS_RANGE_LABEL >= 28


def test_canvas_is_4_to_3_1024_by_768():
    assert (CHART_WIDTH_PX, CHART_HEIGHT_PX) == (1024, 768)
    assert CHART_WIDTH_PX / CHART_HEIGHT_PX == pytest.approx(4 / 3)


def test_chart_layer_never_imports_a_rule_module():
    """Architecture guard (spec §二/§四十): the renderer may only depend on
    the chart data model, the three-state enum and the report model."""
    source = Path(renderer.__file__).read_text(encoding="utf-8")
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.")
    }
    assert imported == {
        "app.domain.chart_data",
        "app.domain.signal_status",
        "app.reports.text_renderer",
    }


@pytest.mark.parametrize(
    "token",
    [
        "return_5d",
        "return_20d",
        "RANGE_POSITION",
        "low_level_position",
        "HIGH_FIVE_DAY_RETURN",
        "momentum_score",
        "risk_flags",
        "build_range_20d",
        "build_low_level_position",
    ],
)
def test_chart_layer_source_does_not_mention_strategy_inputs(token):
    source = Path(renderer.__file__).read_text(encoding="utf-8")
    # Allowed only inside the module docstring's "never reads X" prose.
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("#", '"'))
    )
    body = code.split('"""', 2)[-1]
    assert token not in body


def test_missing_chart_data_raises_a_render_error():
    with pytest.raises(ChartRenderError):
        render_stock_chart_png(_report(chart=None))


def test_no_cjk_font_raises_a_clear_error(monkeypatch):
    renderer._resolve_fonts.cache_clear()
    monkeypatch.delenv("CHART_FONT_PATH", raising=False)
    monkeypatch.setattr(renderer, "_find_family", lambda family, *, bold: None)
    try:
        with pytest.raises(ChartRenderError, match="CJK"):
            renderer._resolve_fonts()
    finally:
        renderer._resolve_fonts.cache_clear()


def test_font_path_env_must_exist(monkeypatch, tmp_path):
    renderer._resolve_fonts.cache_clear()
    monkeypatch.setenv("CHART_FONT_PATH", str(tmp_path / "nope.ttf"))
    try:
        with pytest.raises(ChartRenderError, match="CHART_FONT_PATH"):
            renderer._resolve_fonts()
    finally:
        renderer._resolve_fonts.cache_clear()


# --- real rendering ------------------------------------------------------------


@requires_font
def test_output_is_a_1024_by_768_png():
    data = render_stock_chart_png(_report())
    image = Image.open(io.BytesIO(data))
    assert image.format == "PNG"
    assert image.size == (1024, 768)


@requires_font
def test_rendering_is_deterministic():
    assert render_stock_chart_png(_report()) == render_stock_chart_png(_report())


@requires_font
def test_typical_chart_meets_the_300kb_performance_target():
    assert len(render_stock_chart_png(_report())) <= TARGET_MAX_BYTES


@requires_font
def test_render_stock_chart_writes_the_file(tmp_path):
    path = render_stock_chart(_report(), tmp_path / "nested" / "6150.png")
    assert path.is_file() and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


@requires_font
def test_markers_change_the_image_but_unknown_equals_false():
    """FALSE and INSUFFICIENT_DATA both draw nothing on the chart (the card
    body is where ❌ / ⚪ differ) — and TRUE must visibly draw something."""
    plain = render_stock_chart_png(_report(first_board=FALSE, overheated=FALSE))
    unknown = render_stock_chart_png(_report(first_board=UNKNOWN, overheated=UNKNOWN))
    first_board = render_stock_chart_png(_report(first_board=TRUE))
    overheated = render_stock_chart_png(_report(overheated=TRUE))
    assert plain == unknown
    assert first_board != plain and overheated != plain
    assert first_board != overheated


@requires_font
def test_limit_up_badge_follows_limit_up_status_only():
    on = render_stock_chart_png(_report(limit_up=TRUE))
    off = render_stock_chart_png(_report(limit_up=FALSE))
    unknown = render_stock_chart_png(_report(limit_up=UNKNOWN))
    assert on != off
    assert off == unknown


@requires_font
def test_both_markers_true_renders_the_same_as_overheated_alone():
    assert render_stock_chart_png(
        _report(first_board=TRUE, overheated=TRUE)
    ) == render_stock_chart_png(_report(first_board=FALSE, overheated=TRUE))


@requires_font
def test_the_20d_range_lines_come_from_chart_data():
    report = _report()
    with_range = render_stock_chart_png(report)
    from dataclasses import replace

    without_range = render_stock_chart_png(
        replace(report, chart_data=replace(report.chart_data, range_20d=None))
    )
    assert with_range != without_range


@requires_font
@pytest.mark.parametrize("sessions", [0, 4, 20, 34, 80, 119, 250])
def test_any_history_length_renders(sessions):
    assert render_stock_chart_png(_report(sessions=sessions)).startswith(b"\x89PNG")


@requires_font
def test_one_price_limit_up_bar_renders():
    assert render_stock_chart_png(_report(one_price=True)).startswith(b"\x89PNG")


@requires_font
def test_a_completely_flat_price_series_does_not_crash():
    flat = [
        HistoricalPricePoint(
            trading_date=dt.date(2026, 8, 1) + dt.timedelta(days=i),
            open=50.0,
            high=50.0,
            low=50.0,
            close=50.0,
            volume=1000.0,
            turnover=1.0,
        )
        for i in range(30)
    ]
    today = HistoricalPricePoint(
        trading_date=dt.date(2026, 10, 1),
        open=55.0,
        high=55.0,
        low=55.0,
        close=55.0,
        volume=9000.0,
        turnover=1.0,
    )
    chart = build_stock_chart_data(history=flat, today=today)
    assert render_stock_chart_png(_report(chart=chart)).startswith(b"\x89PNG")


@requires_font
def test_a_very_long_stock_name_is_truncated_not_overlapping_the_date():
    data = render_stock_chart_png(_report(name="超級無敵長的公司名稱股份有限公司"))
    assert Image.open(io.BytesIO(data)).size == (1024, 768)


@requires_font
def test_every_preview_scenario_renders():
    from app.charts.preview import SCENARIOS, _scenario

    for params in SCENARIOS.values():
        assert render_stock_chart_png(_scenario(**params)).startswith(b"\x89PNG")


# --- fixed-size text: nothing is silently shrunk below the mobile minimums -----


def _spy_on_fonts(monkeypatch):
    requested: list[tuple[float, bool]] = []
    real = renderer._font

    def spy(px, *, bold=False):
        requested.append((px, bold))
        return real(px, bold=bold)

    monkeypatch.setattr(renderer, "_font", spy)
    return requested


@requires_font
def test_rendered_text_uses_exactly_the_spec_font_sizes(monkeypatch):
    """Behavioural, not just constants: record every font actually requested
    while drawing. Bold = name 56 / badges 48 / latest price 40; regular =
    date 32 / legend 30 / ticks+captions 28. Any 'fit to space' shrinking
    would introduce a size outside these sets."""
    requested = _spy_on_fonts(monkeypatch)
    render_stock_chart_png(_report(first_board=TRUE, overheated=TRUE))
    assert {px for px, bold in requested if bold} == {56, 48, 40}
    assert {px for px, bold in requested if not bold} == {32, 30, 28}


@requires_font
def test_latest_price_is_never_shrunk_even_when_space_is_tight(monkeypatch):
    """Regression for the old 'while too wide: size -= 2' loop (which could
    silently drop the tag to 30px). Squeeze the margin: the size must hold."""
    monkeypatch.setattr(renderer, "_MARGIN_RIGHT", 60)
    requested = _spy_on_fonts(monkeypatch)
    render_stock_chart_png(_report())
    assert {px for px, bold in requested if bold} == {56, 48, 40}


@requires_font
@pytest.mark.parametrize("label", ["99.99", "69.90", "888.8", "999.9", "8888"])
def test_widest_latest_price_tags_fit_the_right_margin(label):
    """The tag is fixed at 40px, so the margin must be wide enough for the
    RESOLVED font. If a different font has wider digits this fails loudly
    (widen _MARGIN_RIGHT) rather than clipping the price off the canvas."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(figsize=(10.24, 7.68), dpi=100)
    canvas = FigureCanvasAgg(fig)
    text = fig.text(
        0.5, 0.5, label, fontproperties=renderer._font(renderer._FS_LATEST, bold=True)
    )
    box_pad = 2 * 0.18 * renderer._FS_LATEST
    width = text.get_window_extent(canvas.get_renderer()).width + box_pad
    assert width <= renderer._MARGIN_RIGHT - 8


# --- honest candles: no invented price range for one-price / doji bars ---------


def _record_rectangles(monkeypatch):
    created = []
    real = renderer.Rectangle

    def spy(*args, **kwargs):
        rectangle = real(*args, **kwargs)
        created.append(rectangle)
        return rectangle

    monkeypatch.setattr(renderer, "Rectangle", spy)
    return created


@requires_font
def test_one_price_limit_up_bar_is_a_line_not_a_fabricated_body(monkeypatch):
    created = _record_rectangles(monkeypatch)
    report = _report(one_price=True)
    render_stock_chart_png(report)
    bars = len(report.chart_data.bars)
    assert len(created) == bars - 1  # every bar but today's one-price bar
    centres = [r.get_x() + r.get_width() / 2 for r in created]
    assert all(abs(c - (bars - 1)) > 1e-6 for c in centres)


@requires_font
def test_a_normal_limit_up_bar_still_draws_a_body(monkeypatch):
    created = _record_rectangles(monkeypatch)
    report = _report(one_price=False)
    render_stock_chart_png(report)
    assert len(created) == len(report.chart_data.bars)


@requires_font
def test_a_doji_bar_is_a_line_not_a_fabricated_body(monkeypatch):
    history = _history(80)
    doji_index = 70
    original = history[doji_index]
    history[doji_index] = HistoricalPricePoint(
        trading_date=original.trading_date,
        open=original.close,  # open == close, but a real high/low range
        high=original.high,
        low=original.low,
        close=original.close,
        volume=original.volume,
        turnover=original.turnover,
    )
    chart = build_stock_chart_data(history=history, today=_today(history[-1].close))
    created = _record_rectangles(monkeypatch)
    render_stock_chart_png(_report(chart=chart))
    assert len(created) == len(chart.bars) - 1
