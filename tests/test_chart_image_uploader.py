import datetime as dt
import logging
import os
from io import BytesIO
from types import SimpleNamespace

import pytest
from google.api_core import exceptions as gexc
from PIL import Image
from requests import exceptions as requests_exc

from app.storage.chart_image_uploader import (
    APP_MAX_IMAGE_BYTES,
    CACHE_CONTROL,
    LINE_MAX_IMAGE_BYTES,
    ChartImageUploadError,
    ChartImageUploader,
    GcsObjectStore,
    PermanentStoreError,
    TransientStoreError,
    build_object_name,
    build_public_url,
    build_uploader_from_env,
    validate_bucket_name,
    validate_png,
)


def make_png(width=1024, height=768, color="white", *, noise=False, frames=1):
    if noise:
        image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    else:
        image = Image.new("RGB", (width, height), color)
    buffer = BytesIO()
    if frames > 1:
        others = [Image.new("RGB", (width, height), "black") for _ in range(frames - 1)]
        image.save(buffer, format="PNG", save_all=True, append_images=others)
    else:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


PNG = make_png()
DAY = dt.date(2026, 10, 8)
BASE = "https://storage.googleapis.com/my-bucket"


class FakeStore:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [True])
        self.calls = []

    def upload_if_absent(self, object_name, data, *, content_type, cache_control):
        self.calls.append((object_name, data, content_type, cache_control))
        outcome = self.outcomes.pop(0) if self.outcomes else True
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_uploader(store, **kw):
    sleeps = []
    uploader = ChartImageUploader(
        store, public_base_url=BASE, sleep=sleeps.append, **kw
    )
    return uploader, sleeps


def upload(uploader, png=PNG, stock_id="2330"):
    return uploader.upload_chart(stock_id=stock_id, trading_date=DAY, png_bytes=png)


# ---- naming ---------------------------------------------------------------


def test_object_name_embeds_date_code_and_hash():
    name = build_object_name("2330", DAY, PNG)
    assert name.startswith("charts/2026/10/08/2330_20261008_")
    assert name.endswith(".png")
    assert len(name.rsplit("_", 1)[1]) == 16 + len(".png")


def test_same_bytes_same_name_different_bytes_different_name():
    assert build_object_name("2330", DAY, PNG) == build_object_name("2330", DAY, PNG)
    assert build_object_name("2330", DAY, PNG) != build_object_name(
        "2330", DAY, PNG + b"x"
    )


@pytest.mark.parametrize("bad", ["", "../x", "23/30", "a b", "12345678901", "台積電"])
def test_unsafe_stock_id_is_rejected(bad):
    with pytest.raises(ChartImageUploadError):
        build_object_name(bad, DAY, PNG)


def test_public_url_is_https_and_quoted():
    assert build_public_url(BASE + "/", "charts/a b.png") == BASE + "/charts/a%20b.png"


# ---- uploader -------------------------------------------------------------


def test_success_returns_url_and_uploads_with_headers():
    store = FakeStore([True])
    uploader, _ = make_uploader(store)
    result = upload(uploader)
    assert result.url == f"{BASE}/{result.object_name}"
    assert result.created is True
    assert result.size_bytes == len(PNG)
    name, data, content_type, cache = store.calls[0]
    assert (name, data, content_type, cache) == (
        result.object_name,
        PNG,
        "image/png",
        CACHE_CONTROL,
    )


def test_already_existing_object_is_success():
    uploader, _ = make_uploader(FakeStore([False]))
    result = upload(uploader)
    assert result.created is False
    assert result.url.startswith("https://")


def test_transient_errors_retry_with_backoff_then_succeed():
    store = FakeStore([TransientStoreError("503"), TransientStoreError("503"), True])
    uploader, sleeps = make_uploader(store)
    assert upload(uploader).created is True
    assert len(store.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_transient_errors_exhaust_attempts(caplog):
    store = FakeStore([TransientStoreError("503")] * 3)
    uploader, sleeps = make_uploader(store)
    with caplog.at_level(logging.ERROR, logger="chart_image_uploader"):
        with pytest.raises(ChartImageUploadError, match="3 attempts"):
            upload(uploader)
    assert len(store.calls) == 3
    assert sleeps == [1.0, 2.0]  # no sleep after the last attempt
    assert sum("image_upload_failed" in r.message for r in caplog.records) == 1


def test_permanent_error_is_not_retried(caplog):
    store = FakeStore([PermanentStoreError("403 Forbidden")])
    uploader, sleeps = make_uploader(store)
    with caplog.at_level(logging.ERROR, logger="chart_image_uploader"):
        with pytest.raises(ChartImageUploadError, match="403"):
            upload(uploader)
    assert len(store.calls) == 1
    assert sleeps == []
    assert "image_upload_failed stock_id=2330" in caplog.text


# Payloads are built by factories and every case has an explicit id: pytest
# would otherwise put the repr of the (multi-MB) bytes into the test id, and
# on Windows that id is exported as PYTEST_CURRENT_TEST, whose environment
# variable cannot exceed 32767 characters.
@pytest.mark.parametrize(
    "build_payload, message",
    [
        pytest.param(lambda: b"", "empty", id="empty"),
        pytest.param(lambda: b"GIF89a....", "not a PNG", id="not-png"),
        pytest.param(
            lambda: b"\x89PNG\r\n\x1a\n" + b"truncated",
            "cannot be decoded",
            id="truncated",
        ),
        pytest.param(lambda: make_png(1025, 100), "1024x1024", id="too-wide"),
        pytest.param(lambda: make_png(100, 1025), "1024x1024", id="too-tall"),
        pytest.param(lambda: make_png(64, 64, frames=2), "animated", id="animated"),
        pytest.param(
            lambda: make_png(1024, 1024, noise=True), "policy", id="over-app-policy"
        ),
        pytest.param(
            lambda: PNG + b"\0" * LINE_MAX_IMAGE_BYTES, "LINE's", id="over-line-limit"
        ),
    ],
)
def test_invalid_images_are_rejected_without_calling_store(build_payload, message):
    store = FakeStore()
    uploader, _ = make_uploader(store)
    with pytest.raises(ChartImageUploadError, match=message):
        upload(uploader, png=build_payload())
    assert store.calls == []


def test_policy_limit_is_stricter_than_line_limit():
    assert APP_MAX_IMAGE_BYTES < LINE_MAX_IMAGE_BYTES


def test_validate_png_returns_dimensions():
    assert validate_png(make_png(1024, 768)) == (1024, 768)
    assert validate_png(make_png(1024, 1024)) == (1024, 1024)


def test_overlong_url_is_rejected():
    store = FakeStore()
    uploader = ChartImageUploader(store, public_base_url="https://" + "a" * 2000)
    with pytest.raises(ChartImageUploadError, match="URL"):
        upload(uploader)
    assert store.calls == []


def test_http_base_url_is_refused_at_construction():
    with pytest.raises(ValueError):
        ChartImageUploader(FakeStore(), public_base_url="http://example.com")


# ---- GcsObjectStore (fake client, no network / credentials) ---------------


class FakeBlob:
    def __init__(self, error=None):
        self.error = error
        self.cache_control = None
        self.kwargs = None

    def upload_from_string(self, data, **kwargs):
        self.kwargs = {"data": data, **kwargs}
        if self.error:
            raise self.error


class FakeClient:
    def __init__(self, blob):
        self.blob_obj = blob
        self.names = []

    def bucket(self, name):
        self.names.append(name)
        return SimpleNamespace(
            blob=lambda object_name: (self.names.append(object_name), self.blob_obj)[1]
        )


def gcs_store(error=None):
    blob = FakeBlob(error)
    return GcsObjectStore("my-bucket", client=FakeClient(blob)), blob


def test_gcs_store_uploads_create_only():
    store, blob = gcs_store()
    assert (
        store.upload_if_absent(
            "charts/a.png", b"x", content_type="image/png", cache_control="c"
        )
        is True
    )
    assert blob.kwargs["if_generation_match"] == 0
    assert blob.kwargs["content_type"] == "image/png"
    assert blob.cache_control == "c"
    assert blob.kwargs["timeout"] > 0
    assert blob.kwargs["retry"] is None  # retry is owned by the uploader


def test_gcs_precondition_failed_means_already_exists():
    store, _ = gcs_store(gexc.PreconditionFailed("exists"))
    assert (
        store.upload_if_absent("a", b"x", content_type="image/png", cache_control="c")
        is False
    )


@pytest.mark.parametrize(
    "error",
    [
        gexc.ServiceUnavailable("503"),
        gexc.TooManyRequests("429"),
        gexc.InternalServerError("500"),
        requests_exc.ConnectionError("reset"),
        requests_exc.Timeout("slow"),
    ],
)
def test_gcs_transient_errors_are_mapped(error):
    store, _ = gcs_store(error)
    with pytest.raises(TransientStoreError):
        store.upload_if_absent("a", b"x", content_type="image/png", cache_control="c")


@pytest.mark.parametrize(
    "error", [gexc.Forbidden("403"), gexc.NotFound("no bucket"), ValueError("boom")]
)
def test_gcs_permanent_errors_are_mapped(error):
    store, _ = gcs_store(error)
    with pytest.raises(PermanentStoreError):
        store.upload_if_absent("a", b"x", content_type="image/png", cache_control="c")


# ---- env wiring -----------------------------------------------------------


def test_env_without_bucket_disables_hosting():
    assert build_uploader_from_env({}) is None
    assert build_uploader_from_env({"CHART_GCS_BUCKET": "  "}) is None


def test_env_with_bucket_builds_uploader_with_default_url():
    uploader = build_uploader_from_env({"CHART_GCS_BUCKET": "my-bucket"})
    assert isinstance(uploader, ChartImageUploader)
    assert uploader._base_url == BASE


def test_env_custom_public_base_url():
    uploader = build_uploader_from_env(
        {
            "CHART_GCS_BUCKET": "my-bucket",
            "CHART_PUBLIC_BASE_URL": "https://img.example.com",
        }
    )
    assert uploader._base_url == "https://img.example.com"


def test_env_invalid_bucket_disables_hosting():
    assert build_uploader_from_env({"CHART_GCS_BUCKET": "Bad Bucket!"}) is None


def test_env_non_https_base_url_disables_hosting_instead_of_crashing(caplog):
    with caplog.at_level(logging.ERROR, logger="chart_image_uploader"):
        uploader = build_uploader_from_env(
            {
                "CHART_GCS_BUCKET": "my-bucket",
                "CHART_PUBLIC_BASE_URL": "http://img.example.com",
            }
        )
    assert uploader is None
    assert "image_upload_failed" in caplog.text


@pytest.mark.parametrize(
    "name",
    [
        "my-bucket",
        "taiwan-stock-lab-charts-508205",
        "a.b-c_d",
        "abc",
        "a" * 63,
        ".".join(["a" * 63, "b" * 63, "c" * 63]),
        "1abc",
    ],
)
def test_valid_bucket_names(name):
    assert validate_bucket_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "ab",
        "a" * 64,
        "My-Bucket",
        "bad bucket!",
        "-abc",
        "abc-",
        "a..b",
        "a.-b",
        "192.168.5.4",
        "goog-charts",
        "my-google-bucket",
        "gs://bucket",
        ".".join(["a" * 64, "b"]),
        "a" * 223,
    ],
)
def test_invalid_bucket_names(name):
    assert not validate_bucket_name(name)


# ---- layering / integration -----------------------------------------------


def test_storage_layer_does_not_import_charts_or_domain():
    import ast
    from pathlib import Path

    import app.storage.chart_image_uploader as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = {
        n.module if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in (n.names if isinstance(n, ast.Import) else [n])
        if (n.module if isinstance(n, ast.ImportFrom) else a.name)
    }
    assert not [
        m for m in imported if m.startswith(("app.charts", "app.domain", "app.reports"))
    ]


def test_real_renderer_output_passes_upload_validation():
    """The Step 3 renderer and the Step 4 validator must agree on limits."""
    from app.charts.preview import SCENARIOS, _scenario
    from app.charts.stock_chart_renderer import ChartRenderError, render_stock_chart_png

    for params in SCENARIOS.values():
        try:
            png = render_stock_chart_png(_scenario(**params))
        except ChartRenderError as exc:
            if os.environ.get("CI") == "true":
                raise
            pytest.skip(f"no CJK font available locally: {exc}")
        assert validate_png(png) == (1024, 768)
        assert len(png) <= APP_MAX_IMAGE_BYTES
