import datetime as dt
import hashlib
from io import BytesIO

import httpx
import pytest
from PIL import Image

from app.storage import smoke
from app.storage.chart_image_uploader import (
    CACHE_CONTROL,
    ChartImageUploader,
    PermanentStoreError,
)
from app.storage.smoke import format_report, make_smoke_png, run_smoke

NOW = dt.datetime(2026, 10, 12, 5, 30, tzinfo=dt.timezone.utc)
BASE = "https://storage.googleapis.com/test-bucket"


class FakeStore:
    """Create-only store like GCS: second upload of a name returns False."""

    def __init__(self, fail=None):
        self.objects = {}
        self.fail = fail

    def upload_if_absent(self, object_name, data, *, content_type, cache_control):
        if self.fail:
            raise self.fail
        if object_name in self.objects:
            return False
        self.objects[object_name] = (data, content_type, cache_control)
        return True


def uploader(store):
    return ChartImageUploader(store, public_base_url=BASE, backoff_seconds=0)


def serving(store, **overrides):
    """An http_get that serves what the store holds, like a public bucket."""

    def get(url):
        name = url.removeprefix(BASE + "/")
        if name not in store.objects:
            return httpx.Response(404)
        data, content_type, cache_control = store.objects[name]
        headers = {"content-type": content_type, "cache-control": cache_control}
        headers.update(overrides.get("headers", {}))
        return httpx.Response(
            overrides.get("status", 200), headers=headers, content=data
        )

    return get


def by_name(checks):
    return {c.name: c for c in checks}


def test_all_checks_pass_against_a_correct_bucket():
    store = FakeStore()
    checks = run_smoke(uploader(store), now=NOW, http_get=serving(store))
    assert all(c.passed for c in checks), format_report(checks)
    assert len(checks) == 8


def test_smoke_png_is_valid_unique_per_run_and_small():
    first = make_smoke_png(NOW)
    second = make_smoke_png(NOW + dt.timedelta(microseconds=1))
    assert first != second
    assert Image.open(BytesIO(first)).size == (1024, 768)
    assert len(first) < 100_000


def test_a_private_bucket_is_reported_as_403():
    store = FakeStore()
    checks = by_name(
        run_smoke(uploader(store), now=NOW, http_get=serving(store, status=403))
    )
    get_check = checks["anonymous GET -> HTTP 200"]
    assert not get_check.passed
    assert "403" in get_check.detail
    # nothing after a failed GET is evaluated against a non-image response
    assert "Content-Type is image/png" not in checks


def test_missing_immutable_cache_control_is_a_failure():
    store = FakeStore()
    checks = by_name(
        run_smoke(
            uploader(store),
            now=NOW,
            http_get=serving(store, headers={"cache-control": "no-cache"}),
        )
    )
    assert not checks["Cache-Control contains immutable"].passed


def test_wrong_content_type_is_a_failure():
    store = FakeStore()
    checks = by_name(
        run_smoke(
            uploader(store),
            now=NOW,
            http_get=serving(
                store, headers={"content-type": "application/octet-stream"}
            ),
        )
    )
    assert not checks["Content-Type is image/png"].passed


def test_upload_permission_error_stops_early_and_is_reported(caplog):
    store = FakeStore(fail=PermanentStoreError("403 Forbidden"))
    checks = run_smoke(uploader(store), now=NOW, http_get=serving(store))
    assert [c.passed for c in checks] == [False]
    assert "403 Forbidden" in checks[0].detail


def test_network_error_on_get_is_reported_not_raised():
    store = FakeStore()

    def boom(url):
        raise httpx.ConnectError("no route")

    checks = by_name(run_smoke(uploader(store), now=NOW, http_get=boom))
    assert not checks["anonymous GET of the public URL"].passed


def test_cache_control_constant_satisfies_the_check():
    assert "immutable" in CACHE_CONTROL


def test_main_without_a_bucket_exits_2_and_says_why(monkeypatch, capsys):
    monkeypatch.delenv("CHART_GCS_BUCKET", raising=False)
    assert smoke.main() == 2
    assert "CHART_GCS_BUCKET" in capsys.readouterr().out


@pytest.mark.parametrize("passed, code", [(True, 0), (False, 1)], ids=["ok", "fail"])
def test_main_exit_code_follows_the_checks(monkeypatch, capsys, passed, code):
    monkeypatch.setattr(smoke, "build_uploader_from_env", lambda: object())
    monkeypatch.setattr(smoke, "run_smoke", lambda up: [smoke.Check("x", passed, "d")])
    assert smoke.main() == code
    assert ("PASSED" if passed else "FAILED") in capsys.readouterr().out


def test_report_format():
    text = format_report([smoke.Check("a", True), smoke.Check("b", False, "why")])
    assert text == "[PASS] a\n[FAIL] b  -- why"


def test_hash_helper_matches_object_name_digest():
    # the smoke object name embeds the same sha256 prefix the uploader uses
    store = FakeStore()
    result = uploader(store).upload_chart(
        stock_id="SMOKE", trading_date=NOW.date(), png_bytes=make_smoke_png(NOW)
    )
    digest = hashlib.sha256(make_smoke_png(NOW)).hexdigest()[:16]
    assert result.object_name.endswith(f"_{digest}.png")
