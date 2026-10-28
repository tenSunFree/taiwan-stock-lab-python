"""
10-session institutional detail image (the page behind the card's 📊 明細).

LAYER CONTRACT (same spirit as stock_chart_renderer): this module only
DRAWS. It reads exactly these things from the report model and nothing
else:

    report.stock_name / report.stock_id
    report.institutional_detail_10d      (already-decided, already-rounded lots)

It never computes a signal, never re-aggregates, never touches scoring.
The chart, the table and the footer all read the SAME detail object the
Flex summary reads, so the three views cannot disagree.

OUTPUT: a 1024 x 1500 portrait PNG.
    top     title, window, unit, data cut-off
    middle  three small bar panels (外資 / 投信 / 自營商) sharing one date
            axis, each with its OWN y scale so a large 外資 bar never hides
            a small 投信 bar
    bottom  10-row table (date x 外資 / 投信 / 自營商 / 合計), a 10 日合計
            row, the buy/sell day counts, and the category definition

Colours follow the Taiwan convention used everywhere else in this
project: red = net buy, green = net sell.

Fonts, the PNG encoder and ChartRenderError are shared with
stock_chart_renderer so CJK handling is configured in exactly one place.
"""

from __future__ import annotations

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from PIL import Image

from app.charts.stock_chart_renderer import (
    ChartRenderError,
    _encode_png,
    _font,
    _resolve_fonts,
)
from app.domain.institutional_detail import (
    CATEGORIES,
    CATEGORY_LABELS,
    InstitutionalDetail10D,
    format_lots,
)
from app.reports.text_renderer import ReportStockView

DETAIL_WIDTH_PX = 1024
DETAIL_HEIGHT_PX = 1500
_DPI = 100

_UP = "#E5322B"
_DOWN = "#1FA84F"
_FLAT = "#8A8F98"
_TEXT = "#1F2933"
_MUTED = "#6B7280"
_GRID = "#E5E9EF"
_ROW_ALT = "#F5F7FA"
_RULE = "#CBD2DB"

_PANEL_LEFT = 196
_PANEL_RIGHT = DETAIL_WIDTH_PX - 24
_PANEL_TOP = 196
_PANEL_HEIGHT = 150
_PANEL_GAP = 12

_TABLE_TOP = 724
_ROW_HEIGHT = 46
# right edges of the numeric columns; the date column is left aligned
_COL_DATE_X = 40
_COL_RIGHT_X = {
    "foreign": 372,
    "trust": 560,
    "dealer": 748,
    "total": 984,
}


def _color(value: int) -> str:
    if value > 0:
        return _UP
    if value < 0:
        return _DOWN
    return _FLAT


def _md(day) -> str:
    return f"{day:%m/%d}"


def _render(report: ReportStockView, detail: InstitutionalDetail10D) -> Image.Image:
    fig = Figure(
        figsize=(DETAIL_WIDTH_PX / _DPI, DETAIL_HEIGHT_PX / _DPI),
        dpi=_DPI,
        facecolor="white",
    )
    canvas = FigureCanvasAgg(fig)

    # One full-canvas axes in PIXEL coordinates (origin top-left) for all
    # text, rules and table shading; the three bar panels sit on top.
    draw = fig.add_axes([0, 0, 1, 1])
    draw.set_xlim(0, DETAIL_WIDTH_PX)
    draw.set_ylim(DETAIL_HEIGHT_PX, 0)
    draw.axis("off")

    def text(x, y, value, *, size, bold=False, color=_TEXT, ha="left"):
        draw.text(
            x,
            y,
            value,
            fontproperties=_font(size, bold=bold),
            color=color,
            ha=ha,
            va="center",
        )

    # ---- header ----
    text(24, 46, f"{report.stock_name}（{report.stock_id}）", size=50, bold=True)
    text(24, 108, "近10交易日三大法人買賣超", size=34, bold=True)
    text(
        24,
        156,
        f"{_md(detail.start_date)} ～ {_md(detail.as_of_date)}　"
        f"單位：張　資料截至 {_md(detail.as_of_date)}",
        size=26,
        color=_MUTED,
    )

    # ---- three bar panels, shared date axis ----
    count = len(detail.days)
    xs = list(range(count))
    for index, category in enumerate(CATEGORIES):
        top = _PANEL_TOP + index * (_PANEL_HEIGHT + _PANEL_GAP)
        values = [day.value(category) for day in detail.days]
        peak = max(abs(v) for v in values)

        ax = fig.add_axes(
            [
                _PANEL_LEFT / DETAIL_WIDTH_PX,
                1.0 - (top + _PANEL_HEIGHT) / DETAIL_HEIGHT_PX,
                (_PANEL_RIGHT - _PANEL_LEFT) / DETAIL_WIDTH_PX,
                _PANEL_HEIGHT / DETAIL_HEIGHT_PX,
            ]
        )
        ax.bar(xs, values, width=0.68, color=[_color(v) for v in values], zorder=3)
        ax.axhline(0, color=_RULE, linewidth=1.6, zorder=4)
        limit = peak * 1.18 if peak else 1
        ax.set_ylim(-limit, limit)
        ax.set_xlim(-0.6, count - 0.4)
        ax.set_xticks([])
        if peak:
            ax.set_yticks([-peak, 0, peak])
            ax.set_yticklabels(
                [format_lots(-peak), "0", format_lots(peak)],
                fontproperties=_font(20),
                color=_MUTED,
            )
        else:
            ax.set_yticks([0])
            ax.set_yticklabels(["0"], fontproperties=_font(20), color=_MUTED)
        ax.grid(axis="y", color=_GRID, linewidth=1, zorder=0)
        for side in ("top", "right", "bottom", "left"):
            ax.spines[side].set_visible(False)
        ax.tick_params(length=0, pad=8)

        total = detail.category_total(category)
        text(24, top + 54, CATEGORY_LABELS[category], size=32, bold=True)
        text(24, top + 100, format_lots(total), size=26, color=_color(total))

    axis_y = _PANEL_TOP + 3 * _PANEL_HEIGHT + 2 * _PANEL_GAP + 28
    plot_width = _PANEL_RIGHT - _PANEL_LEFT
    for position, day in enumerate(detail.days):
        center = _PANEL_LEFT + (position + 0.6) / (count + 0.2) * plot_width
        text(center, axis_y, _md(day.trading_date), size=20, color=_MUTED, ha="center")

    # ---- table ----
    header_y = _TABLE_TOP + _ROW_HEIGHT / 2
    text(_COL_DATE_X, header_y, "日期", size=27, bold=True, color=_MUTED)
    for key, label in (
        ("foreign", "外資"),
        ("trust", "投信"),
        ("dealer", "自營商"),
        ("total", "合計"),
    ):
        text(
            _COL_RIGHT_X[key],
            header_y,
            label,
            size=27,
            bold=True,
            color=_MUTED,
            ha="right",
        )
    rule_y = _TABLE_TOP + _ROW_HEIGHT
    draw.plot([24, DETAIL_WIDTH_PX - 24], [rule_y, rule_y], color=_RULE, linewidth=1.6)

    for position, day in enumerate(detail.days):
        row_top = rule_y + position * _ROW_HEIGHT
        if position % 2 == 1:
            draw.fill_between(
                [24, DETAIL_WIDTH_PX - 24],
                row_top,
                row_top + _ROW_HEIGHT,
                color=_ROW_ALT,
                linewidth=0,
            )
        y = row_top + _ROW_HEIGHT / 2
        text(_COL_DATE_X, y, _md(day.trading_date), size=28)
        for key, value in (
            ("foreign", day.foreign),
            ("trust", day.trust),
            ("dealer", day.dealer),
            ("total", day.total),
        ):
            text(
                _COL_RIGHT_X[key],
                y,
                format_lots(value),
                size=28,
                color=_color(value),
                ha="right",
            )

    sum_top = rule_y + count * _ROW_HEIGHT
    draw.plot([24, DETAIL_WIDTH_PX - 24], [sum_top, sum_top], color=_TEXT, linewidth=2)
    sum_y = sum_top + _ROW_HEIGHT / 2 + 6
    text(_COL_DATE_X, sum_y, "10日合計", size=28, bold=True)
    for key, value in (
        ("foreign", detail.category_total("foreign")),
        ("trust", detail.category_total("trust")),
        ("dealer", detail.category_total("dealer")),
        ("total", detail.grand_total),
    ):
        text(
            _COL_RIGHT_X[key],
            sum_y,
            format_lots(value),
            size=28,
            bold=True,
            color=_color(value),
            ha="right",
        )

    # ---- footer ----
    footer_y = sum_top + _ROW_HEIGHT + 56
    day_counts = f"買超 {detail.total_buy_days} 日／賣超 {detail.total_sell_days} 日"
    if detail.total_flat_days:
        day_counts += f"／持平 {detail.total_flat_days} 日"
    text(
        24,
        footer_y,
        f"三大法人合計：{format_lots(detail.grand_total)}張",
        size=32,
        bold=True,
        color=_color(detail.grand_total),
    )
    text(24, footer_y + 48, f"合計{day_counts}", size=26)
    text(
        24,
        footer_y + 104,
        "外資＝外資及陸資＋外資自營商；自營商＝自行買賣＋避險；合計為三者相加。",
        size=21,
        color=_MUTED,
    )
    text(
        24,
        footer_y + 140,
        "單位換算：1 張＝1,000 股。資料僅供參考，不構成投資建議。",
        size=21,
        color=_MUTED,
    )

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


def render_institutional_detail_png(report: ReportStockView) -> bytes:
    """PNG bytes (1024x1500) for the report's 10-session detail.

    Raises ChartRenderError when there is no detail to draw, no CJK font
    is available, or drawing fails — never returns a partial image.
    """
    detail = report.institutional_detail_10d
    if detail is None or not detail.days:
        raise ChartRenderError("institutional_detail_10d is unavailable")
    _resolve_fonts()  # fail fast with the shared, clear message
    try:
        return _encode_png(_render(report, detail))
    except ChartRenderError:
        raise
    except Exception as exc:  # noqa: BLE001 - callers fall back to "no 明細 link"
        raise ChartRenderError(f"institutional detail rendering failed: {exc}") from exc
