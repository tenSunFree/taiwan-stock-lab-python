"""End-to-end failure scenarios for the Flex delivery path (Step 6).

Each test pins one rung of the fallback ladder or one log category:

    chart cannot be drawn / uploaded -> that bubble has no hero   (chart_render_failed / image_upload_failed)
    Flex cannot be built             -> plain-text report         (flex_build_failed)
    LINE refuses / is unreachable    -> the run fails, no fallback (line_send_failed)
"""

import dataclasses
import functools
import json
import logging
from types import SimpleNamespace

import httpx

from app.db.models import MessageDelivery
from app.jobs.daily_ranking import build_outbound_messages, run
from app.reports.chart_images import prepare_chart_heroes
from app.reports.flex_builder import NOTE_CHART_UNAVAILABLE
from app.storage.chart_image_uploader import ChartImageUploadError
from tests.test_daily_ranking import (
    HERO_URL,
    _flex_run,
    _recording_handler,
    _report_stock,
)
from tests.test_flex_builder import texts


def many_stocks(count, **overrides):
    return [
        dataclasses.replace(
            _report_stock(),
            rank=i,
            stock_id=str(2000 + i),
            stock_name=f"S{i}",
            **overrides,
        )
        for i in range(1, count + 1)
    ]


def with_chart(stock):
    """A stock that has chart data, so a chart is attempted for it."""
    return dataclasses.replace(stock, chart_data=SimpleNamespace())


class RecordingUploader:
    def __init__(self, failing=()):
        self.failing = set(failing)
        self.uploaded = []

    def upload_chart(self, *, stock_id, trading_date, png_bytes):
        if stock_id in self.failing:
            raise ChartImageUploadError("403")
        self.uploaded.append(stock_id)
        return SimpleNamespace(url=f"https://img.example.com/{stock_id}.png")


def patch_charts(monkeypatch, *, render):
    """Run the REAL prepare_chart_heroes with a stand-in renderer."""
    monkeypatch.setattr(
        "app.jobs.daily_ranking.prepare_chart_heroes",
        functools.partial(prepare_chart_heroes, render=render),
    )


# ---- Flex cannot be built -> text report ---------------------------------------


def test_oversized_flex_falls_back_to_the_text_report(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr("app.reports.flex_builder.MAX_FLEX_BYTES", 200)
    bodies = []
    with caplog.at_level(logging.ERROR):
        result, session = _flex_run(
            monkeypatch, tmp_path, _recording_handler(bodies), stocks=many_stocks(2)
        )

    assert result == 0
    assert "flex_build_failed report_date=2026-08-07 stock_count=2" in caplog.text
    assert bodies and all(
        m["type"] == "text" for body in bodies for m in body["messages"]
    )
    versions = [r.message_version for r in session.query(MessageDelivery).all()]
    assert versions and all(v.startswith("text-v16:") for v in versions)


def test_uploader_setup_crashing_still_sends_the_text_report(monkeypatch, caplog):
    def boom():
        raise RuntimeError("adc exploded")

    monkeypatch.setattr("app.jobs.daily_ranking.build_uploader_from_env", boom)
    with caplog.at_level(logging.ERROR):
        messages, version = build_outbound_messages(
            report_stocks=many_stocks(1),
            report_messages=["fallback text"],
            target_date=__import__("datetime").date(2026, 8, 7),
            chart_uploader=None,
        )
    assert version == "text-v16"
    assert [m.payload for m in messages] == [{"type": "text", "text": "fallback text"}]
    assert "flex_build_failed" in caplog.text


def test_an_invalid_bucket_name_disables_images_but_not_the_card(monkeypatch, caplog):
    monkeypatch.setenv("CHART_GCS_BUCKET", "Not A Bucket!")
    with caplog.at_level(logging.ERROR):
        messages, version = build_outbound_messages(
            report_stocks=many_stocks(1),
            report_messages=["fallback text"],
            target_date=__import__("datetime").date(2026, 8, 7),
            chart_uploader=None,
        )
    assert version == "flex-v2"
    bubble = messages[0].payload["contents"]["contents"][0]
    assert "hero" not in bubble
    assert "image_upload_failed" in caplog.text


# ---- one chart failing never affects another -----------------------------------


def test_one_render_failure_leaves_the_other_bubble_with_its_hero(
    monkeypatch, tmp_path, caplog
):
    def render(stock):
        if stock.stock_id == "2001":
            raise RuntimeError("no font")
        return b"\x89PNG"

    patch_charts(monkeypatch, render=render)
    uploader = RecordingUploader()
    bodies = []
    with caplog.at_level(logging.ERROR):
        result, _ = _flex_run(
            monkeypatch,
            tmp_path,
            _recording_handler(bodies),
            stocks=[with_chart(s) for s in many_stocks(2)],
            run_kwargs={"chart_uploader": uploader},
        )

    assert result == 0
    first, second = bodies[0]["messages"][0]["contents"]["contents"]
    assert "hero" not in first
    assert NOTE_CHART_UNAVAILABLE in texts(first)
    assert second["hero"]["url"] == "https://img.example.com/2002.png"
    assert uploader.uploaded == ["2002"]
    assert "chart_render_failed stock_id=2001" in caplog.text


def test_one_upload_failure_leaves_the_other_bubble_with_its_hero(
    monkeypatch, tmp_path
):
    patch_charts(monkeypatch, render=lambda stock: b"\x89PNG")
    uploader = RecordingUploader(failing={"2002"})
    bodies = []
    result, _ = _flex_run(
        monkeypatch,
        tmp_path,
        _recording_handler(bodies),
        stocks=[with_chart(s) for s in many_stocks(2)],
        run_kwargs={"chart_uploader": uploader},
    )

    assert result == 0
    first, second = bodies[0]["messages"][0]["contents"]["contents"]
    assert first["hero"]["url"] == "https://img.example.com/2001.png"
    assert "hero" not in second
    assert NOTE_CHART_UNAVAILABLE in texts(second)


# ---- several carousels: resume after a partial failure -------------------------


def test_second_carousel_failing_is_resumed_without_resending_the_first(
    monkeypatch, tmp_path
):
    """13 stocks -> carousel 1 (12 bubbles) + carousel 2 (1 bubble). LINE
    accepts the first and rejects the second; the run fails. The rerun must
    send ONLY the second carousel, and finish."""
    stocks = many_stocks(13)
    seen: list[int] = []

    def rejecting_second(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bubbles = len(body["messages"][0]["contents"]["contents"])
        seen.append(bubbles)
        status = 400 if bubbles == 1 else 200
        return httpx.Response(
            status, headers={"x-line-request-id": "req"}, text="rejected"
        )

    first_run, session = _flex_run(
        monkeypatch, tmp_path, rejecting_second, stocks=stocks
    )
    assert first_run == 1
    assert seen == [12, 1]

    rows = {r.message_version: r.status for r in session.query(MessageDelivery).all()}
    assert rows == {
        "flex-v2:p01-of-02": "SUCCESS",
        "flex-v2:p02-of-02": "FAILED",
    }

    resent: list[int] = []

    def accepting(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        resent.append(len(body["messages"][0]["contents"]["contents"]))
        return httpx.Response(200, headers={"x-line-request-id": "req"}, text="ok")

    second_run, _ = _flex_run(
        monkeypatch, tmp_path, accepting, stocks=stocks, session=session
    )
    assert second_run == 0
    assert resent == [1]
    assert {r.status for r in session.query(MessageDelivery).all()} == {"SUCCESS"}


# ---- LINE refuses: the run fails, no silent fallback --------------------------


def test_line_5xx_fails_the_run_and_does_not_fall_back_to_text(
    monkeypatch, tmp_path, caplog
):
    bodies = []
    with caplog.at_level(logging.ERROR):
        result, session = _flex_run(
            monkeypatch,
            tmp_path,
            _recording_handler(bodies, status=500),
            stocks=[_report_stock()],
        )

    assert result == 1
    assert "line_send_failed report_date=2026-08-07" in caplog.text
    assert "message_version=flex-v2" in caplog.text
    # every attempt was the SAME Flex message; nothing was re-sent as text
    assert bodies and all(b["messages"][0]["type"] == "flex" for b in bodies)
    assert [r.status for r in session.query(MessageDelivery).all()] == ["FAILED"]


# ---- dry run: previews without side effects ------------------------------------


def test_dry_run_writes_the_preview_files_and_sends_nothing(
    monkeypatch, tmp_path, capsys
):
    preview_dir = tmp_path / "preview"
    monkeypatch.setenv("REPORT_PREVIEW_DIR", str(preview_dir))
    bodies = []
    result, session = _flex_run(
        monkeypatch,
        tmp_path,
        _recording_handler(bodies),
        stocks=[_report_stock()],
        env={"LINE_DELIVERY_MODE": "off", "REPORT_DRY_RUN": "true"},
    )

    assert result == 0
    assert bodies == []
    assert session.query(MessageDelivery).count() == 0
    out = capsys.readouterr().out
    assert "REPORT_DRY_RUN Flex preview" in out
    assert "NOT uploaded to GCS" in out
    message = json.loads((preview_dir / "message_01.json").read_text("utf-8"))
    assert message["type"] == "flex"
    assert message["altText"] == "每日漲停股量化觀察 08/07｜1 檔"


def test_dry_run_without_a_qualified_stock_says_text_only(
    monkeypatch, tmp_path, capsys
):
    preview_dir = tmp_path / "preview"
    monkeypatch.setenv("REPORT_PREVIEW_DIR", str(preview_dir))
    result, _ = _flex_run(
        monkeypatch,
        tmp_path,
        _recording_handler([]),
        env={"LINE_DELIVERY_MODE": "off", "REPORT_DRY_RUN": "true"},
    )
    assert result == 0
    assert "plain-text report only" in capsys.readouterr().out
    assert not preview_dir.exists()
