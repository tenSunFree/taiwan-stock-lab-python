"""
Chart image hosting (LINE chart spec: immutable HTTPS image URLs).

Responsibility boundary:
    * This module only UPLOADS bytes it is given and returns a public
      HTTPS URL. It never renders a chart (app.charts does) and never
      decides what a Flex card looks like (Step 5 does).
    * Failure is a normal, expected outcome: every failure surfaces as
      ChartImageUploadError. The caller logs nothing extra and simply
      builds the card WITHOUT a hero image — an image problem must never
      fail the whole push. The `image_upload_failed` log line is emitted
      here, once, so the category is greppable in one place.

Object naming (immutable by construction):
    charts/YYYY/MM/DD/{stock_id}_{YYYYMMDD}_{sha256[:16]}.png
    The name embeds a hash of the PNG bytes, so the same name always
    means the same bytes. LINE (and its CDN/clients) may cache a URL
    forever; a re-rendered, different chart gets a NEW name instead of
    overwriting an old one, so a cached image can never go stale.

Image validation (before any network call):
    LINE hard limits (Flex image component): HTTPS URL <= 2000 chars,
    JPEG/PNG, <= 1024x1024 px, <= 10 MB (300 KB if animated).
    Project policy, stricter on purpose: <= 1 MB (faster hero loading) and
    never animated. Both are separate constants so an error message says
    whether LINE or our own policy rejected the image.

Retries:
    This module owns retry (3 attempts, 1s/2s backoff). The SDK's own
    retry is switched OFF (retry=None); with if_generation_match set the
    SDK would otherwise retry internally and the real attempt count would
    no longer be the documented 3.

Idempotency / least privilege:
    Uploads use "create only if absent" (GCS ifGenerationMatch=0). A
    re-run of the same day therefore needs only roles/storage.objectCreator
    (no delete/overwrite right). "Already exists" is success: same name
    implies same bytes.

Configuration (environment):
    CHART_GCS_BUCKET        bucket name; unset/empty -> feature disabled
                            (build_uploader_from_env returns None and the
                            pipeline sends cards without hero images).
    CHART_PUBLIC_BASE_URL   optional public base URL (CDN / custom domain),
                            default https://storage.googleapis.com/{bucket}
    Credentials are Application Default Credentials. In CI they come from
    google-github-actions/auth (Workload Identity Federation), which
    exports GOOGLE_APPLICATION_CREDENTIALS; no key file is ever stored.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import logging
import os
import re
import time
from io import BytesIO
from dataclasses import dataclass
from typing import Callable, Protocol
from urllib.parse import quote

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger("chart_image_uploader")

OBJECT_PREFIX = "charts"
CONTENT_TYPE = "image/png"
# Immutable name -> safe to cache for a year.
CACHE_CONTROL = "public, max-age=31536000, immutable"
HASH_LENGTH = 16
# LINE hard limits for a Flex image URL (official Messaging API model docs).
MAX_URL_LENGTH = 2000
MAX_IMAGE_WIDTH = 1024
MAX_IMAGE_HEIGHT = 1024
LINE_MAX_IMAGE_BYTES = 10 * 1024 * 1024
# OUR policy (not a LINE limit): keep hero images small.
APP_MAX_IMAGE_BYTES = 1024 * 1024
UPLOAD_TIMEOUT_SECONDS = 30.0
DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 1.0

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_STOCK_ID_RE = re.compile(r"^[0-9A-Za-z]{1,10}$")
_BUCKET_CHARS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$")


def validate_bucket_name(name: str) -> bool:
    """GCS bucket naming rules (the API re-checks; this fails early/clearly).

    3-63 chars (up to 222 when it contains dots, each dot-separated part
    <= 63); lowercase letters, digits, '-', '_', '.'; starts and ends with
    a letter or digit; not an IP address; no '..'; must not start with
    'goog' or contain 'google'.
    """
    if not (3 <= len(name) <= 222) or not _BUCKET_CHARS_RE.fullmatch(name):
        return False
    if ".." in name or ".-" in name or "-." in name:
        return False
    parts = name.split(".")
    if len(parts) == 1 and len(name) > 63:
        return False
    if any(not part or len(part) > 63 for part in parts):
        return False
    if name.startswith("goog") or "google" in name:
        return False
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return True
    return False


def validate_png(png_bytes: bytes) -> tuple[int, int]:
    """Return (width, height) of a PNG that LINE and our policy accept.

    Raises ChartImageUploadError with a reason that says WHICH rule failed.
    """
    if not png_bytes:
        raise ChartImageUploadError("image is empty")
    if not png_bytes.startswith(_PNG_SIGNATURE):
        raise ChartImageUploadError("payload is not a PNG")
    if len(png_bytes) > LINE_MAX_IMAGE_BYTES:
        raise ChartImageUploadError(
            f"image is {len(png_bytes)} bytes, over LINE's {LINE_MAX_IMAGE_BYTES} byte limit"
        )
    if len(png_bytes) > APP_MAX_IMAGE_BYTES:
        raise ChartImageUploadError(
            f"image is {len(png_bytes)} bytes, over our {APP_MAX_IMAGE_BYTES} byte policy"
        )
    try:
        with Image.open(BytesIO(png_bytes)) as image:
            image.verify()  # integrity check; the image is unusable afterwards
        with Image.open(BytesIO(png_bytes)) as image:
            width, height = image.size
            frames = getattr(image, "n_frames", 1)
            fmt = image.format
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ChartImageUploadError(f"PNG cannot be decoded: {exc}") from exc
    if fmt != "PNG":
        raise ChartImageUploadError(f"image format is {fmt}, expected PNG")
    if frames > 1:
        raise ChartImageUploadError("animated PNG is not allowed")
    if width > MAX_IMAGE_WIDTH or height > MAX_IMAGE_HEIGHT:
        raise ChartImageUploadError(
            f"image is {width}x{height}, over LINE's {MAX_IMAGE_WIDTH}x{MAX_IMAGE_HEIGHT} limit"
        )
    return width, height


class ChartImageUploadError(Exception):
    """The image could not be hosted. Callers send the card without a
    hero image."""


class TransientStoreError(Exception):
    """Worth retrying (timeout, 429, 5xx, connection reset)."""


class PermanentStoreError(Exception):
    """Retrying cannot help (403, 404 bucket, bad request)."""


class ObjectStore(Protocol):
    def upload_if_absent(
        self,
        object_name: str,
        data: bytes,
        *,
        content_type: str,
        cache_control: str,
    ) -> bool:
        """Create the object unless it exists. True = created, False =
        already existed. Raises TransientStoreError / PermanentStoreError."""
        ...


class GcsObjectStore:
    """ObjectStore backed by google-cloud-storage (imported lazily so the
    rest of the app and the tests never need credentials)."""

    def __init__(self, bucket_name: str, *, client=None) -> None:
        self._bucket_name = bucket_name
        self._client = client

    def _bucket(self):
        if self._client is None:
            from google.cloud import storage  # lazy import

            self._client = storage.Client()
        return self._client.bucket(self._bucket_name)

    def upload_if_absent(
        self,
        object_name: str,
        data: bytes,
        *,
        content_type: str,
        cache_control: str,
    ) -> bool:
        from google.api_core import exceptions as gexc
        from requests import exceptions as requests_exc

        try:
            blob = self._bucket().blob(object_name)
            blob.cache_control = cache_control
            blob.upload_from_string(
                data,
                content_type=content_type,
                if_generation_match=0,
                timeout=UPLOAD_TIMEOUT_SECONDS,
                retry=None,  # retry is owned by ChartImageUploader
            )
            return True
        except gexc.PreconditionFailed:
            return False  # same hash-named object already there
        except (
            gexc.TooManyRequests,
            gexc.InternalServerError,
            gexc.BadGateway,
            gexc.ServiceUnavailable,
            gexc.GatewayTimeout,
            gexc.RetryError,
            requests_exc.RequestException,  # connection reset, timeout...
            ConnectionError,
            TimeoutError,
        ) as exc:
            raise TransientStoreError(f"{type(exc).__name__}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - 403/404, no credentials, bad request
            raise PermanentStoreError(f"{type(exc).__name__}: {exc}") from exc


@dataclass(frozen=True)
class UploadedChartImage:
    url: str  # public HTTPS URL, goes into the Flex hero + action
    object_name: str
    size_bytes: int
    created: bool  # False = identical object already existed (re-run)


def build_object_name(stock_id: str, trading_date: dt.date, png_bytes: bytes) -> str:
    """charts/YYYY/MM/DD/{stock_id}_{YYYYMMDD}_{hash16}.png"""
    if not _STOCK_ID_RE.fullmatch(stock_id):
        raise ChartImageUploadError(f"invalid stock_id for object name: {stock_id!r}")
    digest = hashlib.sha256(png_bytes).hexdigest()[:HASH_LENGTH]
    return (
        f"{OBJECT_PREFIX}/{trading_date:%Y/%m/%d}/"
        f"{stock_id}_{trading_date:%Y%m%d}_{digest}.png"
    )


def build_public_url(base_url: str, object_name: str) -> str:
    return f"{base_url.rstrip('/')}/{quote(object_name, safe='/')}"


class ChartImageUploader:
    def __init__(
        self,
        store: ObjectStore,
        *,
        public_base_url: str,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not public_base_url.startswith("https://"):
            raise ValueError(
                "public_base_url must be an https:// URL (LINE requires HTTPS)"
            )
        if attempts < 1:
            raise ValueError("attempts must be >= 1")
        self._store = store
        self._base_url = public_base_url
        self._attempts = attempts
        self._backoff = backoff_seconds
        self._sleep = sleep

    def upload_chart(
        self, *, stock_id: str, trading_date: dt.date, png_bytes: bytes
    ) -> UploadedChartImage:
        """Host the PNG and return its immutable public URL.

        Raises ChartImageUploadError (after logging `image_upload_failed`)
        for any failure; never returns a URL that was not actually stored.
        """
        try:
            return self._upload(stock_id, trading_date, png_bytes)
        except ChartImageUploadError as exc:
            logger.error(
                "image_upload_failed stock_id=%s trading_date=%s reason=%s",
                stock_id,
                trading_date,
                exc,
            )
            raise

    def _upload(
        self, stock_id: str, trading_date: dt.date, png_bytes: bytes
    ) -> UploadedChartImage:
        validate_png(png_bytes)
        object_name = build_object_name(stock_id, trading_date, png_bytes)
        url = build_public_url(self._base_url, object_name)
        if len(url) > MAX_URL_LENGTH:
            raise ChartImageUploadError(
                f"image URL is {len(url)} chars, over {MAX_URL_LENGTH}"
            )

        last_error: Exception | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                created = self._store.upload_if_absent(
                    object_name,
                    png_bytes,
                    content_type=CONTENT_TYPE,
                    cache_control=CACHE_CONTROL,
                )
                return UploadedChartImage(
                    url=url,
                    object_name=object_name,
                    size_bytes=len(png_bytes),
                    created=created,
                )
            except PermanentStoreError as exc:
                raise ChartImageUploadError(f"permanent storage error: {exc}") from exc
            except TransientStoreError as exc:
                last_error = exc
                if attempt < self._attempts:
                    self._sleep(self._backoff * (2 ** (attempt - 1)))
        raise ChartImageUploadError(
            f"storage failed after {self._attempts} attempts: {last_error}"
        ) from last_error


def build_uploader_from_env(
    env: dict[str, str] | None = None,
) -> ChartImageUploader | None:
    """Uploader from CHART_GCS_BUCKET, or None when hosting is not
    configured (the pipeline then sends cards without hero images)."""
    env = os.environ if env is None else env
    bucket = env.get("CHART_GCS_BUCKET", "").strip()
    if not bucket:
        return None
    if not validate_bucket_name(bucket):
        logger.error("image_upload_failed reason=invalid CHART_GCS_BUCKET %r", bucket)
        return None
    base_url = env.get("CHART_PUBLIC_BASE_URL", "").strip() or (
        f"https://storage.googleapis.com/{bucket}"
    )
    try:
        return ChartImageUploader(GcsObjectStore(bucket), public_base_url=base_url)
    except ValueError as exc:
        # A bad env value must degrade to "no hero images", never crash the push.
        logger.error(
            "image_upload_failed reason=invalid CHART_PUBLIC_BASE_URL: %s", exc
        )
        return None
