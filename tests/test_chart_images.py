import datetime as dt
import logging
from types import SimpleNamespace

from app.reports.chart_images import prepare_chart_heroes
from app.storage.chart_image_uploader import ChartImageUploadError
from tests.test_flex_builder import make_stock

DAY = dt.date(2026, 10, 1)


class FakeUploader:
    def __init__(self, outcomes=None):
        self.outcomes = dict(outcomes or {})
        self.calls = []

    def upload_chart(self, *, stock_id, trading_date, png_bytes):
        self.calls.append((stock_id, trading_date, png_bytes))
        outcome = self.outcomes.get(stock_id)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(url=f"https://img.example.com/{stock_id}.png")


def fake_render(stock):
    return f"png-{stock.stock_id}".encode()


def two_stocks():
    return [make_stock(stock_id="1111"), make_stock(rank=2, stock_id="2222")]


def test_all_charts_rendered_and_hosted():
    uploader = FakeUploader()
    heroes = prepare_chart_heroes(
        two_stocks(), trading_date=DAY, uploader=uploader, render=fake_render
    )
    assert heroes == {
        "1111": "https://img.example.com/1111.png",
        "2222": "https://img.example.com/2222.png",
    }
    assert uploader.calls == [("1111", DAY, b"png-1111"), ("2222", DAY, b"png-2222")]


def test_no_uploader_means_no_heroes_and_nothing_is_rendered():
    rendered = []

    def render(stock):
        rendered.append(stock.stock_id)
        return b"x"

    assert (
        prepare_chart_heroes(
            two_stocks(), trading_date=DAY, uploader=None, render=render
        )
        == {}
    )
    assert rendered == []


def test_stock_without_chart_data_is_skipped_silently(caplog):
    uploader = FakeUploader()
    with caplog.at_level(logging.WARNING):
        heroes = prepare_chart_heroes(
            [
                make_stock(stock_id="1111", chart_data=None),
                make_stock(rank=2, stock_id="2222"),
            ],
            trading_date=DAY,
            uploader=uploader,
            render=fake_render,
        )
    assert list(heroes) == ["2222"]
    assert [call[0] for call in uploader.calls] == ["2222"]
    assert caplog.text == ""


def test_render_failure_drops_only_that_stock_and_is_logged(caplog):
    def render(stock):
        if stock.stock_id == "1111":
            raise RuntimeError("no font")
        return b"png"

    uploader = FakeUploader()
    with caplog.at_level(logging.ERROR, logger="chart_images"):
        heroes = prepare_chart_heroes(
            two_stocks(), trading_date=DAY, uploader=uploader, render=render
        )
    assert list(heroes) == ["2222"]
    assert "chart_render_failed stock_id=1111 report_date=2026-10-01" in caplog.text
    assert [call[0] for call in uploader.calls] == ["2222"]


def test_upload_failure_drops_only_that_stock_without_double_logging(caplog):
    uploader = FakeUploader({"1111": ChartImageUploadError("403")})
    with caplog.at_level(logging.ERROR, logger="chart_images"):
        heroes = prepare_chart_heroes(
            two_stocks(), trading_date=DAY, uploader=uploader, render=fake_render
        )
    assert list(heroes) == ["2222"]
    # the uploader logs its own image_upload_failed line; this module must not repeat it
    assert caplog.text == ""


def test_unexpected_upload_error_is_contained_and_logged(caplog):
    uploader = FakeUploader({"1111": ValueError("boom")})
    with caplog.at_level(logging.ERROR, logger="chart_images"):
        heroes = prepare_chart_heroes(
            two_stocks(), trading_date=DAY, uploader=uploader, render=fake_render
        )
    assert list(heroes) == ["2222"]
    assert "image_upload_failed stock_id=1111" in caplog.text


def test_every_chart_failing_returns_an_empty_mapping():
    def render(stock):
        raise RuntimeError("nope")

    assert (
        prepare_chart_heroes(
            two_stocks(), trading_date=DAY, uploader=FakeUploader(), render=render
        )
        == {}
    )
