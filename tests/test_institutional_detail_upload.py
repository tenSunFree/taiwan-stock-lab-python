import datetime as dt

import pytest

from app.reports.chart_images import prepare_institutional_detail_links
from app.storage.chart_image_uploader import (
    DETAIL_MAX_IMAGE_HEIGHT,
    ChartImageUploadError,
    build_detail_object_name,
    validate_png,
)
from tests.test_chart_image_uploader import (
    BASE,
    DAY,
    FakeStore,
    make_png,
    make_uploader,
)
from tests.test_flex_builder import make_stock
from tests.test_flex_builder import make_detail

TALL = make_png(1024, 1500)


def test_object_name_is_immutable_and_separate_from_hero_charts():
    name = build_detail_object_name("2330", DAY, TALL)
    assert name.startswith("institutional/2026/10/08/2330_20261008_")
    assert name.endswith(".png")
    assert not name.startswith("charts/")
    assert name != build_detail_object_name("2330", DAY, make_png(1024, 1501))
    assert name == build_detail_object_name("2330", DAY, TALL)


def test_detail_upload_accepts_tall_image_but_hero_upload_still_does_not():
    uploader, _ = make_uploader(FakeStore())
    result = uploader.upload_institutional_detail(
        stock_id="2330", trading_date=DAY, png_bytes=TALL
    )
    assert result.url.startswith(f"{BASE}/institutional/2026/10/08/2330_20261008_")
    assert result.created is True
    with pytest.raises(ChartImageUploadError, match="1024x1024"):
        uploader.upload_chart(stock_id="2330", trading_date=DAY, png_bytes=TALL)


def test_detail_upload_still_enforces_its_own_height_cap():
    too_tall = make_png(1024, DETAIL_MAX_IMAGE_HEIGHT + 1)
    with pytest.raises(ChartImageUploadError, match="limit"):
        validate_png(too_tall, max_height=DETAIL_MAX_IMAGE_HEIGHT)


def test_detail_upload_logs_its_kind(caplog):
    uploader, _ = make_uploader(FakeStore())
    with pytest.raises(ChartImageUploadError):
        uploader.upload_institutional_detail(
            stock_id="bad id!", trading_date=DAY, png_bytes=TALL
        )
    assert "image_upload_failed kind=institutional_detail" in caplog.text


# --- prepare_institutional_detail_links --------------------------------------


class FakeDetailUploader:
    def __init__(self, outcomes=None):
        self.outcomes = dict(outcomes or {})
        self.calls = []

    def upload_institutional_detail(self, *, stock_id, trading_date, png_bytes):
        self.calls.append(stock_id)
        outcome = self.outcomes.get(stock_id)
        if isinstance(outcome, Exception):
            raise outcome
        from types import SimpleNamespace

        return SimpleNamespace(url=f"https://img.example.com/{stock_id}_detail.png")


def with_detail(**kw):
    return make_stock(institutional_detail_10d=make_detail(), **kw)


def fake_render(stock):
    return b"png"


def test_links_only_for_stocks_that_have_a_detail():
    stocks = [with_detail(stock_id="1111"), make_stock(rank=2, stock_id="2222")]
    uploader = FakeDetailUploader()
    links = prepare_institutional_detail_links(
        stocks,
        trading_date=dt.date(2026, 10, 21),
        uploader=uploader,
        render=fake_render,
    )
    assert links == {"1111": "https://img.example.com/1111_detail.png"}
    assert uploader.calls == ["1111"]  # no image is drawn or uploaded for 2222


def test_no_uploader_means_no_links_and_no_rendering():
    def boom(stock):
        raise AssertionError("must not render without an uploader")

    assert (
        prepare_institutional_detail_links(
            [with_detail()], trading_date=DAY, uploader=None, render=boom
        )
        == {}
    )


def test_one_failure_never_affects_another_stock(caplog):
    stocks = [
        with_detail(stock_id="1111"),
        with_detail(rank=2, stock_id="2222"),
        with_detail(rank=3, stock_id="3333"),
    ]

    def render(stock):
        if stock.stock_id == "1111":
            raise RuntimeError("draw failed")
        return b"png"

    uploader = FakeDetailUploader({"2222": ChartImageUploadError("denied")})
    links = prepare_institutional_detail_links(
        stocks, trading_date=DAY, uploader=uploader, render=render
    )
    assert set(links) == {"3333"}
    assert "institutional_detail_render_failed stock_id=1111" in caplog.text


def test_unexpected_upload_error_is_contained_and_logged(caplog):
    uploader = FakeDetailUploader({"1111": RuntimeError("sdk exploded")})
    links = prepare_institutional_detail_links(
        [with_detail(stock_id="1111")],
        trading_date=DAY,
        uploader=uploader,
        render=fake_render,
    )
    assert links == {}
    assert "image_upload_failed kind=institutional_detail stock_id=1111" in caplog.text


# --- end to end through build_outbound_messages (real renderer) --------------


class BothKindsUploader:
    def __init__(self, *, fail_detail=False):
        self.fail_detail = fail_detail
        self.detail_bytes = []

    def upload_chart(self, *, stock_id, trading_date, png_bytes):
        raise ChartImageUploadError("no hero in this test")

    def upload_institutional_detail(self, *, stock_id, trading_date, png_bytes):
        if self.fail_detail:
            raise ChartImageUploadError("storage down")
        self.detail_bytes.append(png_bytes)
        from types import SimpleNamespace

        return SimpleNamespace(
            url=f"https://img.example.com/institutional/{stock_id}.png"
        )


def _outbound(stocks, uploader):
    from app.jobs.daily_ranking import MESSAGE_VERSION, build_outbound_messages

    messages, version = build_outbound_messages(
        report_stocks=stocks,
        report_messages=["text fallback"],
        target_date=dt.date(2026, 10, 21),
        chart_uploader=uploader,
    )
    return messages, version, MESSAGE_VERSION


def test_card_links_to_the_uploaded_detail_image():
    import json

    uploader = BothKindsUploader()
    messages, version, expected = _outbound([with_detail(stock_id="2330")], uploader)
    assert version == expected == "flex-v4"
    flex = json.dumps(messages[-1].payload, ensure_ascii=False)
    assert "📊 明細" in flex
    assert "https://img.example.com/institutional/2330.png" in flex
    assert uploader.detail_bytes[0].startswith(b"\x89PNG")


def test_detail_upload_failure_keeps_the_card_and_drops_only_the_button():
    import json

    messages, version, expected = _outbound(
        [with_detail(stock_id="2330")], BothKindsUploader(fail_detail=True)
    )
    assert version == expected  # still the Flex carousel, not the text fallback
    flex = json.dumps(messages[-1].payload, ensure_ascii=False)
    assert "法人籌碼｜近10交易日" in flex
    assert "📊 明細" not in flex


def test_idempotency_fingerprint_is_the_same_with_or_without_the_link():
    ok, _, _ = _outbound([with_detail(stock_id="2330")], BothKindsUploader())
    failed, _, _ = _outbound(
        [with_detail(stock_id="2330")], BothKindsUploader(fail_detail=True)
    )
    assert ok[-1].fingerprint == failed[-1].fingerprint
