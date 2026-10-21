import ast
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.charts.stock_chart_renderer import select_chart_marker
from app.clients.line_client import LinePushError
from app.jobs import test_line_flex_push as e2e
from app.reports.flex_builder import FlexBuildError
from app.storage.chart_image_uploader import ChartImageUploader, PermanentStoreError

DAY = dt.date(2026, 10, 15)
BASE = "https://storage.googleapis.com/test-bucket"


class FakeStore:
    def __init__(self, fail=None):
        self.objects = {}
        self.fail = fail

    def upload_if_absent(self, object_name, data, *, content_type, cache_control):
        if self.fail:
            raise self.fail
        if object_name in self.objects:
            return False
        self.objects[object_name] = (data, content_type)
        return True


class FakeClient:
    def __init__(self, error=None, success=True):
        self.calls = []
        self.error = error
        self.success = success

    def push_messages(self, *, target_id, messages):
        self.calls.append((target_id, messages))
        if self.error:
            raise self.error
        return SimpleNamespace(
            success=self.success, status_code=200, request_id="req-1"
        )


def uploader(store):
    return ChartImageUploader(store, public_base_url=BASE, backoff_seconds=0)


def serving(store, status=200, content_type="image/png"):
    def get(url):
        name = url.removeprefix(BASE + "/")
        if name not in store.objects:
            return httpx.Response(404)
        return httpx.Response(
            status, headers={"content-type": content_type}, content=b"x"
        )

    return get


def run(store=None, client=None, **kwargs):
    store = store or FakeStore()
    kwargs.setdefault("http_get", serving(store))
    return e2e.run_e2e(
        uploader=uploader(store),
        trading_date=DAY,
        client=client,
        target_id="U-test" if client else None,
        **kwargs,
    )


def test_fixture_stocks_are_synthetic_drawable_and_cover_each_marker():
    stocks = e2e.make_fixture_stocks(DAY)
    assert [s.stock_id for s in stocks] == ["E2E001", "E2E002", "E2E003"]
    assert all(s.stock_name.startswith("【測試】") for s in stocks)
    assert all(s.chart_data is not None for s in stocks)
    assert [select_chart_marker(s) for s in stocks] == ["低檔首板", "漲多過熱", None]
    assert stocks[2].is_one_price_limit_up


def test_fixture_is_deterministic():
    first = e2e.make_fixture_stocks(DAY)
    second = e2e.make_fixture_stocks(DAY)
    assert first == second


def test_happy_path_pushes_headline_and_one_flex_carousel_to_target_only():
    client = FakeClient()
    checks, heroes = run(client=client)

    assert all(c.passed for c in checks), e2e.format_report(checks)
    assert len(heroes) == 3
    assert len(client.calls) == 1
    target, messages = client.calls[0]
    assert target == "U-test"
    # the headline text first, then the carousel, in ONE request
    assert [m["type"] for m in messages] == ["text", "flex"]
    assert messages[0]["text"] == "每日漲停股量化觀察 10/15｜3 檔"
    contents = messages[1]["contents"]
    assert contents["type"] == "carousel"
    assert len(contents["contents"]) == 3
    assert all(b["hero"]["url"] in heroes.values() for b in contents["contents"])


def test_a_chart_attached_to_the_wrong_stock_fails(monkeypatch):
    real = e2e.build_flex_carousels

    def swapped(*args, **kwargs):
        carousels = real(*args, **kwargs)
        first, second = carousels[0].contents["contents"][:2]
        first["hero"]["url"], second["hero"]["url"] = (
            second["hero"]["url"],
            first["hero"]["url"],
        )
        return carousels

    monkeypatch.setattr(e2e, "build_flex_carousels", swapped)
    client = FakeClient()
    checks, _ = run(client=client)
    assert not checks[-1].passed
    assert "hero_ok=False" in checks[-1].detail
    assert client.calls == []


def test_no_send_builds_everything_but_does_not_call_line():
    checks, heroes = run(client=None)
    assert all(c.passed for c in checks)
    assert any("skipped" in c.detail for c in checks)
    assert len(heroes) == 3


def test_a_failed_upload_is_a_failure_not_a_card_without_a_hero():
    client = FakeClient()
    checks, _ = run(store=FakeStore(fail=PermanentStoreError("403")), client=client)
    assert not checks[-1].passed
    assert checks[-1].name == "every chart rendered and uploaded"
    assert client.calls == []


def test_a_private_bucket_fails_before_anything_is_sent():
    store = FakeStore()
    client = FakeClient()
    checks, _ = run(store=store, client=client, http_get=serving(store, status=403))
    assert not checks[-1].passed
    assert "403" in checks[-1].detail
    assert client.calls == []


def test_a_flex_build_error_fails_instead_of_falling_back_to_text(monkeypatch):
    def boom(*args, **kwargs):
        raise FlexBuildError("too big")

    monkeypatch.setattr(e2e, "build_flex_carousels", boom)
    client = FakeClient()
    checks, _ = run(client=client)
    assert not checks[-1].passed
    assert checks[-1].name == "build Flex carousel"
    assert client.calls == []


def test_a_line_error_is_reported_not_raised():
    checks, _ = run(client=FakeClient(error=LinePushError("HTTP 400")))
    assert not checks[-1].passed
    assert "HTTP 400" in checks[-1].detail


def test_an_unsuccessful_push_result_fails():
    checks, _ = run(client=FakeClient(success=False))
    assert not checks[-1].passed


def test_main_requires_line_settings_unless_no_send(monkeypatch, capsys):
    for name in ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_TARGET_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(e2e, "load_env_file", lambda: None)
    monkeypatch.setattr(e2e, "build_uploader_from_env", lambda: object())
    assert e2e.main([]) == 2
    assert "LINE_TARGET_ID" in capsys.readouterr().err


def test_main_requires_a_bucket(monkeypatch, capsys):
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "t")
    monkeypatch.setenv("LINE_TARGET_ID", "U")
    monkeypatch.setattr(e2e, "load_env_file", lambda: None)
    monkeypatch.setattr(e2e, "build_uploader_from_env", lambda: None)
    assert e2e.main([]) == 2
    assert "CHART_GCS_BUCKET" in capsys.readouterr().err


@pytest.mark.parametrize("passed, code", [(True, 0), (False, 1)], ids=["ok", "fail"])
def test_main_exit_code_follows_the_checks(monkeypatch, capsys, passed, code):
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_TARGET_ID", raising=False)
    monkeypatch.setattr(e2e, "load_env_file", lambda: None)
    monkeypatch.setattr(e2e, "build_uploader_from_env", lambda: object())
    monkeypatch.setattr(
        e2e, "run_e2e", lambda **kw: ([e2e.Check("x", passed, "d")], {})
    )
    assert e2e.main(["--no-send"]) == code
    assert ("PASSED" if passed else "FAILED") in capsys.readouterr().out


def test_the_tool_cannot_broadcast_or_touch_the_delivery_database():
    source = Path(e2e.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any(
        m.startswith(("app.delivery", "app.db", "sqlalchemy")) for m in imported
    )
    assert ".broadcast" not in source and "broadcast_flex" not in source
