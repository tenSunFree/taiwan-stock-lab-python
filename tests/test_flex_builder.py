import ast
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest

import app.reports.flex_builder as flex_builder
from app.domain.chart_data import StockChartData
from app.domain.signal_status import SignalStatus
from app.reports.flex_builder import (
    MAX_BUBBLES_PER_CAROUSEL,
    NOTE_CHART_INSUFFICIENT,
    NOTE_CHART_UNAVAILABLE,
    FlexBuildError,
    build_alt_text,
    build_flex_carousels,
    build_stock_bubble,
    status_label,
)
from app.reports.text_renderer import (
    DISCLAIMER,
    FIRST_BOARD_APPROXIMATION_NOTE,
    ReportStockView,
)

DAY = dt.date(2026, 10, 1)
URL = "https://storage.googleapis.com/b/charts/2026/10/01/6150_20261001_abcd.png"
EMPTY_CHART = StockChartData(
    bars=(), ma5=(), ma20=(), ma60=(), avg_volume_20=(), range_20d=None
)

TRUE = SignalStatus.TRUE
FALSE = SignalStatus.FALSE
UNKNOWN = SignalStatus.INSUFFICIENT_DATA


def make_stock(**overrides) -> ReportStockView:
    values = dict(
        rank=1,
        stock_id="6150",
        stock_name="撼訊",
        total_score=73.61,
        data_completeness=0.9,
        top_factor_names=(),
        risk_flags=(),
        close_price=Decimal("69.90"),
        change_percent=9.91,
        volume_ratio_20d=14.13,
        return_5d=0.137,
        chart_data=EMPTY_CHART,
        low_level_first_limit_up_status=FALSE,
        momentum_overheated_status=FALSE,
    )
    values.update(overrides)
    return ReportStockView(**values)


def texts(node) -> list[str]:
    """Every text string in a Flex tree, in document order."""
    found: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "text":
            found.append(node["text"])
        for value in node.values():
            found.extend(texts(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(texts(item))
    return found


def body_texts(bubble: dict) -> list[str]:
    return texts(bubble["body"])


# ---- hero ------------------------------------------------------------------


def test_hero_is_4_by_3_fit_with_uri_action():
    bubble = build_stock_bubble(make_stock(), total_shown=4, hero_url=URL)
    assert bubble["hero"] == {
        "type": "image",
        "url": URL,
        "size": "full",
        "aspectRatio": "4:3",
        "aspectMode": "fit",
        "backgroundColor": "#FFFFFF",
        "action": {"type": "uri", "uri": URL},
    }
    assert NOTE_CHART_UNAVAILABLE not in body_texts(bubble)
    assert NOTE_CHART_INSUFFICIENT not in body_texts(bubble)


def test_no_hero_url_gives_unavailable_note_when_chart_data_exists():
    bubble = build_stock_bubble(make_stock(), total_shown=4, hero_url=None)
    assert "hero" not in bubble
    assert NOTE_CHART_UNAVAILABLE in body_texts(bubble)


def test_no_chart_data_gives_insufficient_note():
    bubble = build_stock_bubble(
        make_stock(chart_data=None), total_shown=4, hero_url=None
    )
    assert "hero" not in bubble
    assert NOTE_CHART_INSUFFICIENT in body_texts(bubble)


@pytest.mark.parametrize(
    "bad_url",
    ["http://example.com/a.png", "ftp://x/a.png", "", "https://x/" + "a" * 1000],
)
def test_invalid_hero_urls_are_dropped_not_sent_to_line(bad_url):
    bubble = build_stock_bubble(make_stock(), total_shown=1, hero_url=bad_url)
    assert "hero" not in bubble
    assert NOTE_CHART_UNAVAILABLE in body_texts(bubble)


# ---- body content ----------------------------------------------------------


def test_body_matches_the_spec_layout():
    bubble = build_stock_bubble(make_stock(), total_shown=4, hero_url=URL)
    assert body_texts(bubble) == [
        "撼訊 6150",
        "69.90｜+9.91%",
        "綜合 73.61｜1/4",
        "量比 14.13×｜5D +13.7%",
        "低檔首板：❌ 否",
        "漲多過熱：❌ 否",
        "資料完整度：90%",
        "⚠️ 追價風險／開板風險",
    ]


def test_footer_carries_the_required_disclaimer_and_approximation_note():
    bubble = build_stock_bubble(make_stock(), total_shown=1, hero_url=URL)
    footer = texts(bubble["footer"])
    assert DISCLAIMER in footer
    assert FIRST_BOARD_APPROXIMATION_NOTE in footer


@pytest.mark.parametrize(
    "status, label",
    [(TRUE, "✅ 是"), (FALSE, "❌ 否"), (UNKNOWN, "⚪ 資料不足")],
)
def test_three_state_labels(status, label):
    assert status_label(status) == label
    bubble = build_stock_bubble(
        make_stock(
            low_level_first_limit_up_status=status, momentum_overheated_status=status
        ),
        total_shown=1,
        hero_url=URL,
    )
    assert f"低檔首板：{label}" in body_texts(bubble)
    assert f"漲多過熱：{label}" in body_texts(bubble)


def test_insufficient_data_is_never_rendered_as_no():
    bubble = build_stock_bubble(
        make_stock(
            low_level_first_limit_up_status=UNKNOWN, momentum_overheated_status=UNKNOWN
        ),
        total_shown=1,
        hero_url=URL,
    )
    assert "低檔首板：❌ 否" not in body_texts(bubble)
    assert "漲多過熱：❌ 否" not in body_texts(bubble)


def test_missing_numbers_show_data_insufficient_not_zero():
    bubble = build_stock_bubble(
        make_stock(
            volume_ratio_20d=None, return_5d=None, close_price=None, change_percent=None
        ),
        total_shown=1,
        hero_url=URL,
    )
    assert "量比 資料不足｜5D 資料不足" in body_texts(bubble)
    assert "—" in body_texts(bubble)


def test_price_color_follows_direction_red_up_green_down():
    def price_color(change):
        bubble = build_stock_bubble(
            make_stock(change_percent=change), total_shown=1, hero_url=URL
        )
        return next(
            c["color"]
            for c in bubble["body"]["contents"]
            if c.get("type") == "text" and "｜" in c["text"] and "%" in c["text"]
        )

    assert price_color(9.9) == "#D32F2F"
    assert price_color(-1.0) == "#2E7D32"
    assert price_color(0.0) == "#666666"


def test_risk_line_adds_flag_labels_without_duplicates():
    bubble = build_stock_bubble(
        make_stock(
            risk_flags=(
                "ONE_PRICE_LIMIT_UP",
                "DISPOSITION_STOCK",
                "UNKNOWN_FLAG",
                "ONE_PRICE_LIMIT_UP",
            )
        ),
        total_shown=1,
        hero_url=URL,
    )
    assert body_texts(bubble)[-1] == "⚠️ 追價風險／開板風險／一字漲停／處置股"


def test_high_five_day_return_flag_is_not_worded_as_overheated():
    bubble = build_stock_bubble(
        make_stock(risk_flags=("HIGH_FIVE_DAY_RETURN",)), total_shown=1, hero_url=URL
    )
    risk_line = body_texts(bubble)[-1]
    assert "近 5 日漲幅偏高" in risk_line
    assert "過熱" not in risk_line


def test_unconfirmed_regulatory_status_is_shown():
    bubble = build_stock_bubble(
        make_stock(risk_missing_inputs=("is_attention",)), total_shown=1, hero_url=URL
    )
    assert "⚪ 注意／處置／全額交割狀態未確認" in body_texts(bubble)
    clean = build_stock_bubble(
        make_stock(risk_missing_inputs=("consecutive_limit_up_days",)),
        total_shown=1,
        hero_url=URL,
    )
    assert "⚪ 注意／處置／全額交割狀態未確認" not in body_texts(clean)


# ---- carousel --------------------------------------------------------------


def stocks(count: int) -> list[ReportStockView]:
    return [
        make_stock(rank=i + 1, stock_id=f"{1000 + i}", stock_name=f"股{i}")
        for i in range(count)
    ]


def test_alt_text_format():
    assert build_alt_text(DAY, 4) == "每日漲停股量化觀察 10/01｜4 檔"


def test_single_carousel_in_rank_order():
    carousels = build_flex_carousels(stocks(4), trading_date=DAY)
    assert len(carousels) == 1
    carousel = carousels[0]
    assert carousel.alt_text == "每日漲停股量化觀察 10/01｜4 檔"
    assert carousel.contents["type"] == "carousel"
    assert carousel.stock_ids == ("1000", "1001", "1002", "1003")
    assert [texts(b["body"])[0] for b in carousel.contents["contents"]] == [
        f"股{i} {1000 + i}" for i in range(4)
    ]


def test_more_than_twelve_stocks_split_into_several_carousels():
    carousels = build_flex_carousels(
        stocks(MAX_BUBBLES_PER_CAROUSEL + 1), trading_date=DAY
    )
    assert [len(c.stock_ids) for c in carousels] == [12, 1]
    # rank denominators still reflect the whole list, not the chunk
    last_bubble = carousels[1].contents["contents"][0]
    assert "13/13" in "".join(body_texts(last_bubble))


def test_hero_urls_are_matched_by_stock_id():
    carousel = build_flex_carousels(
        stocks(2), trading_date=DAY, hero_urls={"1001": URL}
    )[0]
    first, second = carousel.contents["contents"]
    assert "hero" not in first
    assert second["hero"]["url"] == URL


def test_no_stocks_is_a_build_error():
    with pytest.raises(FlexBuildError):
        build_flex_carousels([], trading_date=DAY)


def test_oversized_message_is_a_build_error(monkeypatch):
    monkeypatch.setattr(flex_builder, "MAX_FLEX_BYTES", 500)
    with pytest.raises(FlexBuildError, match="byte"):
        build_flex_carousels(stocks(3), trading_date=DAY)


def test_realistic_top_ten_is_well_under_the_line_size_limit():
    carousel = build_flex_carousels(
        stocks(10), trading_date=DAY, hero_urls={f"{1000 + i}": URL for i in range(10)}
    )[0]
    size = len(json.dumps(carousel.contents, ensure_ascii=False).encode("utf-8"))
    assert size < 30_000


# ---- idempotency fingerprint -----------------------------------------------


def test_fingerprint_ignores_hero_urls_and_chart_notes():
    batch = stocks(3)
    with_images = build_flex_carousels(
        batch, trading_date=DAY, hero_urls={s.stock_id: URL for s in batch}
    )[0]
    without_images = build_flex_carousels(batch, trading_date=DAY)[0]
    some_images = build_flex_carousels(
        batch, trading_date=DAY, hero_urls={batch[0].stock_id: URL + "?other"}
    )[0]
    assert with_images.contents != without_images.contents
    assert (
        with_images.fingerprint == without_images.fingerprint == some_images.fingerprint
    )


def test_fingerprint_changes_when_meaningful_content_changes():
    base = build_flex_carousels(stocks(2), trading_date=DAY)[0].fingerprint
    changed_score = stocks(2)
    changed_score[0] = make_stock(
        rank=1, stock_id="1000", stock_name="股0", total_score=50.0
    )
    changed_signal = stocks(2)
    changed_signal[1] = make_stock(
        rank=2, stock_id="1001", stock_name="股1", momentum_overheated_status=TRUE
    )
    assert build_flex_carousels(changed_score, trading_date=DAY)[0].fingerprint != base
    assert build_flex_carousels(changed_signal, trading_date=DAY)[0].fingerprint != base
    assert (
        build_flex_carousels(stocks(2), trading_date=dt.date(2026, 10, 2))[
            0
        ].fingerprint
        != base
    )


def test_fingerprint_is_deterministic():
    assert (
        build_flex_carousels(stocks(3), trading_date=DAY)[0].fingerprint
        == build_flex_carousels(stocks(3), trading_date=DAY)[0].fingerprint
    )


# ---- layering --------------------------------------------------------------


def test_flex_builder_does_not_import_chart_storage_or_transport_layers():
    tree = ast.parse(Path(flex_builder.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [node])
        if (node.module if isinstance(node, ast.ImportFrom) else alias.name)
    }
    forbidden = ("app.charts", "app.storage", "app.clients", "app.delivery", "httpx")
    assert not [m for m in imported if m.startswith(forbidden)]


def test_flex_builder_does_not_re_derive_a_signal():
    source = Path(flex_builder.__file__).read_text(encoding="utf-8")
    code = source.split('"""', 2)[-1]
    for token in ("0.30", "range_position", "RANGE_POSITION", "HIGH_FIVE_DAY_RETURN >"):
        assert token not in code
