"""
Daily K-line chart renderer for the LINE "每日漲停股量化觀察" Flex card.

LAYER CONTRACT (LINE chart spec §二, §四十):
    This module only DRAWS. It reads exactly these things from the report
    model and nothing else:

        report.stock_name / report.stock_id
        report.chart_data                      (bars, MAs, volume avg, 20D range)
        report.limit_up_status                 (SignalStatus)
        report.low_level_first_limit_up_status (SignalStatus)
        report.momentum_overheated_status      (SignalStatus)

    It never decides low-level / first-board / overheated / limit-up, never
    reads return_5d, never recomputes the 20D range, and never imports a
    rule module (an architecture test pins the import list). The only
    "decision" here is the spec's UI display priority (§22): at most ONE
    extra marker, 漲多過熱 wins over 低檔首板.

OUTPUT: a 1024x768 (4:3) PNG, designed to stay readable after LINE shrinks
a carousel bubble to phone width — hence the big fonts and thick strokes.
Sizes below are in PIXELS of the 1024x768 canvas (converted to points
internally); they follow spec §8.

Dependencies are deliberately light: matplotlib's object-oriented API with
the Agg canvas — no pyplot and no GUI backend, which keeps rendering
isolated and suitable for headless CI — plus Pillow (already a matplotlib
dependency) for encoding. (Matplotlib's font manager is still process-wide
state, so this module makes no thread-safety promise.)

FONTS: CJK text needs a CJK-capable font. Resolution order:
    1. CHART_FONT_PATH (+ optional CHART_FONT_BOLD_PATH) environment variables
    2. a TC-capable system family (Microsoft JhengHei, PingFang TC,
       Noto Sans TC / CJK TC, ...)
    3. Noto Sans CJK JP, WenQuanYi, Droid Sans Fallback
Note: matplotlib registers only the FIRST face of a .ttc collection, which
for Debian/Ubuntu's fonts-noto-cjk is the JP face (it provides broad
Traditional Chinese glyph coverage, but some glyph shapes follow Japanese
conventions). Point CHART_FONT_PATH at a standalone Noto Sans TC file to
get the TC shapes. No font found -> ChartRenderError (the caller then
sends the card without a hero image; it never aborts the whole push).
"""

from __future__ import annotations

import functools
import io
import math
import os
from dataclasses import dataclass
from pathlib import Path

from matplotlib import font_manager as fm
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle
from matplotlib.ticker import MaxNLocator
from PIL import Image

from app.domain.chart_data import StockChartData
from app.domain.signal_status import SignalStatus
from app.reports.text_renderer import ReportStockView

# --- canvas ------------------------------------------------------------------

CHART_WIDTH_PX = 1024
CHART_HEIGHT_PX = 768  # 4:3
_DPI = 100

# Soft performance target (spec §7), NOT a hard limit: clarity wins.
TARGET_MAX_BYTES = 300 * 1024

# --- layout (pixels, origin top-left) ----------------------------------------

_MARGIN_LEFT = 14
# Right-hand margin: price ticks + the latest-close tag. The tag is a FIXED
# 40px (spec §8 minimum) and is never shrunk to fit; the widest realistic
# label ("888.8", ~122px incl. its box) leaves ~24px of slack here so a
# font with wider digits still fits. A test measures this for the resolved font.
_MARGIN_RIGHT = 154
_PLOT_LEFT = _MARGIN_LEFT
_PLOT_WIDTH = CHART_WIDTH_PX - _MARGIN_LEFT - _MARGIN_RIGHT

_TITLE_CENTER_Y = 44
_LEGEND_CENTER_Y = 98
_PRICE_TOP = 128
_PRICE_HEIGHT = 396  # ~70% of price+volume
_VOLUME_TOP = _PRICE_TOP + _PRICE_HEIGHT + 10
_VOLUME_HEIGHT = 168  # ~30% of price+volume
_XLABEL_CENTER_Y = _VOLUME_TOP + _VOLUME_HEIGHT + 34

# --- font sizes in px (spec §8 minimums) -------------------------------------

_FS_TITLE = 56
_FS_DATE = 32
_FS_BADGE = 48
_FS_LATEST = 40
_FS_LEGEND = 30
_FS_TICK = 28
_FS_RANGE_LABEL = 28
_FS_VOLUME_LABEL = 28

# --- colours (Taiwan convention: red up, green down) -------------------------

_UP = "#E5322B"
_DOWN = "#1FA84F"
_FLAT = "#8A8F98"
_UP_DARK = "#7A0C0C"  # outline of today's limit-up candle
_MA_COLORS = {"MA5": "#F2A900", "MA20": "#1E88E5", "MA60": "#8E44AD"}
_RANGE_COLOR = "#5F6B7A"
_VOLUME_AVG_COLOR = "#37474F"
_GRID = "#EDF0F4"
_SPINE = "#CBD2DB"
_TEXT = "#1F2933"
_TEXT_MUTED = "#6B7280"
_BADGE_LIMIT_UP = "#D32F2F"
_BADGE_OVERHEATED = "#E65100"
_BADGE_FIRST_BOARD = "#1565C0"

_CANDLE_BODY_WIDTH = 0.68
_LIMIT_UP_BODY_WIDTH = 0.9  # today's limit-up bar is drawn a little wider
_BADGE_PAD = 0.25  # boxstyle pad, in units of the badge font size

_TC_FONT_FAMILIES = (
    "Microsoft JhengHei",
    "PingFang TC",
    "Noto Sans TC",
    "Noto Sans CJK TC",
    "Heiti TC",
)
_FALLBACK_FONT_FAMILIES = (
    "Noto Sans CJK JP",
    "WenQuanYi Zen Hei",
    "WenQuanYi Micro Hei",
    "Droid Sans Fallback",
    "Noto Sans CJK SC",
)


class ChartRenderError(Exception):
    """The chart cannot be produced. Callers log `chart_render_failed`
    and send the card without a hero image."""


@dataclass(frozen=True)
class _Fonts:
    regular: str
    bold: str


def _pt(px: float) -> float:
    """Pixels on the 1024x768 canvas -> typographic points."""
    return px * 72.0 / _DPI


def _find_family(family: str, *, bold: bool) -> str | None:
    prop = fm.FontProperties(family=family, weight="bold" if bold else "regular")
    try:
        path = fm.findfont(prop, fallback_to_default=False)
    except ValueError:
        return None
    return path if Path(path).is_file() else None


@functools.lru_cache(maxsize=1)
def _resolve_fonts() -> _Fonts:
    env_regular = os.environ.get("CHART_FONT_PATH", "").strip()
    if env_regular:
        if not Path(env_regular).is_file():
            raise ChartRenderError(f"CHART_FONT_PATH does not exist: {env_regular}")
        env_bold = os.environ.get("CHART_FONT_BOLD_PATH", "").strip()
        if env_bold and not Path(env_bold).is_file():
            raise ChartRenderError(f"CHART_FONT_BOLD_PATH does not exist: {env_bold}")
        return _Fonts(regular=env_regular, bold=env_bold or env_regular)

    for family in (*_TC_FONT_FAMILIES, *_FALLBACK_FONT_FAMILIES):
        regular = _find_family(family, bold=False)
        if regular is not None:
            return _Fonts(
                regular=regular, bold=_find_family(family, bold=True) or regular
            )
    raise ChartRenderError(
        "no CJK-capable font found; install fonts-noto-cjk (Linux) or set "
        "CHART_FONT_PATH to a .ttf/.otf file"
    )


def _font(px: float, *, bold: bool = False) -> fm.FontProperties:
    fonts = _resolve_fonts()
    return fm.FontProperties(fname=fonts.bold if bold else fonts.regular, size=_pt(px))


# --- presentation-only helpers (no strategy) ----------------------------------


def select_chart_marker(report: ReportStockView) -> str | None:
    """The ONE optional model marker drawn on the chart (spec §22).

    This is UI display priority, not a model judgement: 漲多過熱 (a risk
    warning) outranks 低檔首板 when both are TRUE. FALSE and
    INSUFFICIENT_DATA draw nothing (the card body shows ❌ / ⚪ instead).
    """
    if report.momentum_overheated_status is SignalStatus.TRUE:
        return "漲多過熱"
    if report.low_level_first_limit_up_status is SignalStatus.TRUE:
        return "低檔首板"
    return None


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


def _directions(bars) -> list[int]:
    """+1 up / -1 down / 0 flat per bar: candle body first (紅K/黑K); a
    body-less bar (e.g. a one-price limit-up) falls back to close vs the
    previous displayed close."""
    result: list[int] = []
    previous_close: float | None = None
    for bar in bars:
        direction = _sign(bar.close - bar.open)
        if direction == 0 and previous_close is not None:
            direction = _sign(bar.close - previous_close)
        result.append(direction)
        previous_close = bar.close
    return result


def _direction_color(direction: int) -> str:
    return _UP if direction > 0 else _DOWN if direction < 0 else _FLAT


def _price_decimals(reference_price: float) -> int:
    """Choose ONE consistent display precision for a chart (ticks and the
    latest-close tag must agree): 2 decimals below 100, 1 below 1000, else 0.

    Presentation formatting only. It deliberately does NOT implement or
    duplicate TWSE tick-size / pricing rules — those belong to the domain
    layer, and the chart must never look like it understands them."""
    if reference_price < 100:
        return 2
    if reference_price < 1000:
        return 1
    return 0


def _format_price(value: float, decimals: int) -> str:
    return f"{value:.{decimals}f}"


def _tick_label_positions(count: int) -> list[int]:
    """Indices of the (at most 4) date labels, spread over the interior."""
    if count <= 1:
        return [0]
    if count <= 4:
        return list(range(count)) if count == 2 else [0, count // 2, count - 1]
    raw = [0.07 + i * (0.93 - 0.07) / 3 for i in range(4)]
    return sorted({round(fraction * (count - 1)) for fraction in raw})


# --- drawing ------------------------------------------------------------------


def _rect(left: float, top: float, width: float, height: float) -> list[float]:
    return [
        left / CHART_WIDTH_PX,
        1.0 - (top + height) / CHART_HEIGHT_PX,
        width / CHART_WIDTH_PX,
        height / CHART_HEIGHT_PX,
    ]


def _fig_text(fig, x_px, y_px, text, *, size, bold=False, color=_TEXT, ha="left"):
    return fig.text(
        x_px / CHART_WIDTH_PX,
        1.0 - y_px / CHART_HEIGHT_PX,
        text,
        fontproperties=_font(size, bold=bold),
        color=color,
        ha=ha,
        va="center",
    )


def _draw_legend_row(fig, renderer, items, *, x_px, y_px, font_px):
    """items: (label, color, kind) with kind 'line' or 'swatch'. Laid out
    left to right using the REAL measured text widths."""
    cursor = float(x_px)
    for label, color, kind in items:
        sample_width = 40 if kind == "line" else 26
        x0 = cursor / CHART_WIDTH_PX
        x1 = (cursor + sample_width) / CHART_WIDTH_PX
        y = 1.0 - y_px / CHART_HEIGHT_PX
        fig.add_artist(
            Line2D(
                [x0, x1],
                [y, y],
                transform=fig.transFigure,
                color=color,
                linewidth=_pt(5 if kind == "line" else 14),
                solid_capstyle="butt",
            )
        )
        text = _fig_text(
            fig, cursor + sample_width + 8, y_px, label, size=font_px, color=_TEXT
        )
        width = text.get_window_extent(renderer).width
        cursor += sample_width + 8 + width + 26
    return cursor


def _fit_title(fig, renderer, name: str, date_text: str) -> None:
    date_artist = _fig_text(
        fig,
        CHART_WIDTH_PX - 16,
        _TITLE_CENTER_Y + 8,
        date_text,
        size=_FS_DATE,
        color=_TEXT_MUTED,
        ha="right",
    )
    date_left = date_artist.get_window_extent(renderer).x0
    title = _fig_text(
        fig, 16, _TITLE_CENTER_Y, name, size=_FS_TITLE, bold=True, color=_TEXT
    )
    # Never shrink below the spec minimum (56px): truncate the NAME instead.
    shown = name
    while len(shown) > 3 and title.get_window_extent(renderer).x1 > date_left - 16:
        shown = shown[:-1]
        title.set_text(shown.rstrip() + "…")


def _render(report: ReportStockView, chart: StockChartData) -> Image.Image:
    bars = chart.bars
    count = len(bars)
    xs = list(range(count))
    directions = _directions(bars)
    colors = [_direction_color(d) for d in directions]

    fig = Figure(
        figsize=(CHART_WIDTH_PX / _DPI, CHART_HEIGHT_PX / _DPI),
        dpi=_DPI,
        facecolor="white",
    )
    canvas = FigureCanvasAgg(fig)
    renderer = canvas.get_renderer()

    ax_price = fig.add_axes(_rect(_PLOT_LEFT, _PRICE_TOP, _PLOT_WIDTH, _PRICE_HEIGHT))
    ax_volume = fig.add_axes(
        _rect(_PLOT_LEFT, _VOLUME_TOP, _PLOT_WIDTH, _VOLUME_HEIGHT), sharex=ax_price
    )

    # ---- header: name + code (left), data date (right) ----
    _fit_title(
        fig,
        renderer,
        f"{report.stock_name} {report.stock_id}",
        bars[-1].trading_date.strftime("%Y/%m/%d"),
    )
    _draw_legend_row(
        fig,
        renderer,
        [(label, color, "line") for label, color in _MA_COLORS.items()],
        x_px=16,
        y_px=_LEGEND_CENTER_Y,
        font_px=_FS_LEGEND,
    )

    # ---- price y-range: candles + the T-20..T-1 range; headroom for badges ----
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    range_20d = chart.range_20d
    if range_20d is not None:
        highs.append(range_20d.high)
        lows.append(range_20d.low)
    data_high, data_low = max(highs), min(lows)
    span = data_high - data_low
    if span <= 0:
        span = max(data_high * 0.02, 0.01)

    limit_up = report.limit_up_status is SignalStatus.TRUE
    marker = select_chart_marker(report)
    has_badges = limit_up or marker is not None
    # Room under the plot for the "20D Low" caption; room above for ONE row
    # of badges (they sit side by side, so two badges cost no extra height).
    bottom_frac = 0.12 if range_20d is not None else 0.05
    top_frac = 0.22 if has_badges else 0.06
    total = span / (1.0 - bottom_frac - top_frac)
    y_min = data_low - bottom_frac * total
    y_max = data_high + top_frac * total
    ax_price.set_ylim(y_min, y_max)
    ax_price.set_xlim(-0.8, count - 1 + 0.8)
    price_per_px = (y_max - y_min) / _PRICE_HEIGHT

    # ---- right-hand price ticks (drop any that would touch the latest tag) ----
    last = bars[-1]
    decimals = _price_decimals(last.close)
    raw_ticks = MaxNLocator(nbins=5, steps=[1, 2, 2.5, 5, 10]).tick_values(
        data_low, data_high
    )
    keep_px = (_FS_LATEST + _FS_TICK) / 2 + 6
    ticks = [
        t
        for t in raw_ticks
        if y_min <= t <= data_high + 0.02 * total
        and abs(t - last.close) / price_per_px > keep_px
    ]
    ax_price.set_yticks(ticks)
    ax_price.set_yticklabels([_format_price(t, decimals) for t in ticks])
    for label in ax_price.get_yticklabels():
        label.set_fontproperties(_font(_FS_TICK))
        label.set_color(_TEXT_MUTED)
    ax_price.yaxis.tick_right()
    ax_price.tick_params(axis="y", length=6, width=1.5, color=_SPINE, pad=6)
    ax_price.tick_params(axis="x", length=0, labelbottom=False)
    ax_price.grid(axis="y", color=_GRID, linewidth=_pt(1.5), zorder=0)
    ax_price.set_axisbelow(True)

    # ---- 20D High / Low dashed lines (T-20 .. T), values straight from Domain ----
    if range_20d is not None:
        x_start = max(0, count - 21) - 0.4
        for value, label, above in (
            (range_20d.high, "20D High", True),
            (range_20d.low, "20D Low", False),
        ):
            ax_price.hlines(
                value,
                x_start,
                count - 1 + 0.4,
                colors=_RANGE_COLOR,
                linewidth=_pt(3),
                linestyles=[(0, (2.6, 1.8))],
                zorder=2,
            )
            ax_price.annotate(
                label,
                xy=(x_start, value),
                xytext=(4, 5 if above else -5),
                textcoords="offset pixels",
                ha="left",
                va="bottom" if above else "top",
                fontproperties=_font(_FS_RANGE_LABEL),
                color=_RANGE_COLOR,
                bbox={
                    "boxstyle": "round,pad=0.12",
                    "fc": "white",
                    "ec": "none",
                    "alpha": 0.8,
                },
                zorder=6,
                annotation_clip=False,
            )

    # ---- moving averages (None -> NaN gap; never padded) ----
    nan = float("nan")
    for label, series in (
        ("MA5", chart.ma5),
        ("MA20", chart.ma20),
        ("MA60", chart.ma60),
    ):
        ax_price.plot(
            xs,
            [nan if v is None else v for v in series],
            color=_MA_COLORS[label],
            linewidth=_pt(3.2),
            solid_capstyle="round",
            zorder=3,
        )

    # ---- candles ----
    for index, bar in enumerate(bars):
        is_today_limit_up = limit_up and index == count - 1
        color = _UP if is_today_limit_up else colors[index]
        outline_color = _UP_DARK if is_today_limit_up else color
        ax_price.vlines(
            index,
            bar.low,
            bar.high,
            colors=outline_color,
            linewidth=_pt(3.4 if is_today_limit_up else 2.4),
            zorder=4,
        )
        body_width = _LIMIT_UP_BODY_WIDTH if is_today_limit_up else _CANDLE_BODY_WIDTH
        body_height = abs(bar.close - bar.open)
        if body_height <= 1e-9:
            # Doji / one-price bar: draw a horizontal stroke AT the traded
            # price. Inflating a fake body would imply prices that never
            # traded; a thick line stays visible without inventing a range.
            ax_price.hlines(
                bar.close,
                index - body_width / 2,
                index + body_width / 2,
                colors=outline_color,
                linewidth=_pt(5.0 if is_today_limit_up else 3.0),
                zorder=5,
            )
            continue
        ax_price.add_patch(
            Rectangle(
                (index - body_width / 2, min(bar.open, bar.close)),
                body_width,
                body_height,
                facecolor=color,
                # Spec §20: a bold OUTLINE marks the limit-up bar — no
                # background fill behind it.
                edgecolor=outline_color,
                linewidth=_pt(3.6 if is_today_limit_up else 0.8),
                zorder=5,
            )
        )

    # ---- model badges: ONE row in the headroom, right-aligned to the plot ----
    # 漲停 sits rightmost (nearest today's bar); the optional marker is to its
    # left on the same row, so two badges cost no extra vertical space.
    badges: list[tuple[str, str]] = []
    if limit_up:
        badges.append(("漲停", _BADGE_LIMIT_UP))
    if marker is not None:
        badges.append(
            (marker, _BADGE_OVERHEATED if marker == "漲多過熱" else _BADGE_FIRST_BOARD)
        )
    gap_px = 10.0
    # The rounded box extends `pad` beyond the right-aligned text, so shift
    # left by that much: the box's edge then sits flush with the plot edge
    # and can never touch the latest-close tag in the right margin.
    offset_x = -(_BADGE_PAD * _FS_BADGE) - 2.0
    for text, background in badges:
        artist = ax_price.annotate(
            text,
            xy=(count - 1 + 0.5, data_high),
            xytext=(offset_x, gap_px),
            textcoords="offset pixels",
            ha="right",
            va="bottom",
            fontproperties=_font(_FS_BADGE, bold=True),
            color="white",
            bbox={
                "boxstyle": f"round,pad={_BADGE_PAD},rounding_size=0.3",
                "fc": background,
                "ec": "none",
            },
            zorder=10,
            annotation_clip=False,
        )
        offset_x -= (
            artist.get_window_extent(renderer).width
            + 2 * _BADGE_PAD * _FS_BADGE
            + gap_px
        )
    # When today's high sits well below the badge row, a thin leader line ties
    # the 漲停 badge to the bar it describes.
    if limit_up and (data_high - last.high) / price_per_px > 14:
        ax_price.vlines(
            count - 1,
            last.high,
            data_high + gap_px * price_per_px,
            colors=_BADGE_LIMIT_UP,
            linewidth=_pt(2.4),
            zorder=4,
        )

    # ---- latest close tag: in the right margin, so it never covers the bar ----
    latest_color = _UP if limit_up else colors[-1]
    ax_price.annotate(
        _format_price(last.close, decimals),
        xy=(1.0, last.close),
        xycoords=("axes fraction", "data"),
        xytext=(8, 0),
        textcoords="offset pixels",
        ha="left",
        va="center",
        fontproperties=_font(_FS_LATEST, bold=True),
        color="white",
        bbox={"boxstyle": "round,pad=0.18", "fc": latest_color, "ec": "none"},
        zorder=10,
        annotation_clip=False,
    )

    # ---- volume panel ----
    volumes = [b.volume for b in bars]
    avg = [nan if v is None else v for v in chart.avg_volume_20]
    ax_volume.bar(
        xs, volumes, width=_CANDLE_BODY_WIDTH, color=colors, linewidth=0, zorder=3
    )
    ax_volume.plot(
        xs,
        avg,
        color=_VOLUME_AVG_COLOR,
        linewidth=_pt(3.2),
        solid_capstyle="round",
        zorder=4,
    )
    finite_avg = [v for v in avg if not math.isnan(v)]
    volume_top = max([*volumes, *finite_avg, 1.0]) * 1.42
    ax_volume.set_ylim(0, volume_top)
    ax_volume.set_yticks([])
    _draw_legend_row(
        fig,
        renderer,
        [("成交量", _FLAT, "swatch"), ("20日均量", _VOLUME_AVG_COLOR, "line")],
        x_px=_PLOT_LEFT + 10,
        y_px=_VOLUME_TOP + 24,
        font_px=_FS_VOLUME_LABEL,
    )

    # ---- shared X axis: equally spaced sessions, ~4 MM/DD labels ----
    positions = _tick_label_positions(count)
    ax_volume.set_xticks(positions)
    ax_volume.set_xticklabels(
        [bars[i].trading_date.strftime("%m/%d") for i in positions]
    )
    for position, label in zip(positions, ax_volume.get_xticklabels()):
        label.set_fontproperties(_font(_FS_TICK))
        label.set_color(_TEXT_MUTED)
        label.set_horizontalalignment(
            "left" if position < 3 else "right" if position > count - 4 else "center"
        )
    ax_volume.tick_params(axis="x", length=6, width=1.5, color=_SPINE, pad=8)
    ax_volume.grid(False)

    for axis in (ax_price, ax_volume):
        for spine in axis.spines.values():
            spine.set_color(_SPINE)
            spine.set_linewidth(_pt(1.5))
        axis.set_facecolor("white")

    canvas.draw()
    rgba = Image.frombuffer(
        "RGBA",
        canvas.get_width_height(),
        bytes(canvas.buffer_rgba()),
        "raw",
        "RGBA",
        0,
        1,
    )
    return rgba.convert("RGB")


def _encode_png(image: Image.Image) -> bytes:
    """Lossless, optimised PNG. If that misses the soft 300KB target the
    chart is re-encoded with an adaptive 256-colour palette (no dithering —
    a flat-colour chart loses nothing visible); the smaller one wins."""
    lossless = io.BytesIO()
    image.save(lossless, format="PNG", optimize=True)
    data = lossless.getvalue()
    if len(data) <= TARGET_MAX_BYTES:
        return data
    palette = io.BytesIO()
    image.quantize(
        colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE
    ).save(palette, format="PNG", optimize=True)
    return palette.getvalue() if len(palette.getvalue()) < len(data) else data


def render_stock_chart_png(report: ReportStockView) -> bytes:
    """Render the report's chart to PNG bytes (1024x768).

    Raises ChartRenderError when no chart can be drawn (no chart_data, no
    CJK font, or any drawing failure) — never returns a partial image.
    """
    chart = report.chart_data
    if chart is None or not chart.bars:
        raise ChartRenderError("chart_data is unavailable for this stock")
    _resolve_fonts()  # fail fast with a clear message
    try:
        return _encode_png(_render(report, chart))
    except ChartRenderError:
        raise
    except Exception as exc:  # noqa: BLE001 - wrap everything: callers fall back
        raise ChartRenderError(f"chart rendering failed: {exc}") from exc


def render_stock_chart(report: ReportStockView, output_path: str | Path) -> Path:
    """Convenience wrapper: render and write the PNG, return its path."""
    data = render_stock_chart_png(report)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    return output_path
