"""
Real-bucket smoke test for chart image hosting.

    python -m app.storage.smoke

Everything else about image hosting is tested with fakes; this is the one
check that talks to the REAL bucket, to answer the questions fakes cannot:
can the service account create objects, is the bucket publicly readable,
and does the public URL serve what LINE needs (HTTPS, image/png, cacheable)?

What it does (in order; each line printed as PASS / FAIL):
    1. build the uploader from CHART_GCS_BUCKET (+ optional
       CHART_PUBLIC_BASE_URL) with Application Default Credentials;
    2. upload a small generated PNG (unique per run: it embeds the time);
    3. upload the SAME bytes again -> must report "already existed"
       (create-only upload is idempotent with objectCreator alone);
    4. fetch the public URL WITHOUT credentials and check: HTTP 200,
       Content-Type image/png, Cache-Control contains "immutable", the
       bytes are exactly what was uploaded, 1024x768, URL <= 2000 chars.

Exit code 0 only if every check passed (2 = hosting not configured).

Side effect: one small object per run under
charts/YYYY/MM/DD/SMOKE_<date>_<hash>.png. The pipeline's service account
(objectCreator) cannot delete it; remove it manually as an admin. Do NOT put
a short-TTL lifecycle rule on charts/: production images share that prefix and
old LINE cards would lose their hero images.
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass
from io import BytesIO
from typing import Callable

import httpx
from PIL import Image, ImageDraw

from app.storage.chart_image_uploader import (
    MAX_URL_LENGTH,
    ChartImageUploader,
    ChartImageUploadError,
    build_uploader_from_env,
)

SMOKE_STOCK_ID = "SMOKE"
IMAGE_SIZE = (1024, 768)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""


def make_smoke_png(now: dt.datetime) -> bytes:
    """A valid 1024x768 PNG whose bytes differ every run (it shows `now`)."""
    image = Image.new("RGB", IMAGE_SIZE, "#FFFFFF")
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (20, 20, IMAGE_SIZE[0] - 20, IMAGE_SIZE[1] - 20), outline="#1E88E5", width=6
    )
    draw.text((60, 60), "chart hosting smoke test", fill="#222222")
    draw.text((60, 100), now.strftime("%Y-%m-%d %H:%M:%S.%f UTC"), fill="#222222")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def run_smoke(
    uploader: ChartImageUploader,
    *,
    now: dt.datetime | None = None,
    http_get: Callable[[str], httpx.Response] | None = None,
) -> list[Check]:
    """Run the checks against `uploader`. Never raises for an expected
    failure: it is reported as a failed Check instead."""
    now = now or dt.datetime.now(dt.timezone.utc)
    http_get = http_get or (
        lambda url: httpx.get(url, timeout=30.0, follow_redirects=False)
    )
    checks: list[Check] = []
    png = make_smoke_png(now)

    try:
        first = uploader.upload_chart(
            stock_id=SMOKE_STOCK_ID, trading_date=now.date(), png_bytes=png
        )
    except ChartImageUploadError as exc:
        checks.append(Check("upload a PNG (create-only)", False, str(exc)))
        return checks
    checks.append(
        Check(
            "upload a PNG (create-only)",
            True,
            f"{first.object_name} ({first.size_bytes} bytes)",
        )
    )

    try:
        second = uploader.upload_chart(
            stock_id=SMOKE_STOCK_ID, trading_date=now.date(), png_bytes=png
        )
        checks.append(
            Check(
                "same bytes again -> already existed",
                first.created and not second.created,
                f"first.created={first.created} second.created={second.created}",
            )
        )
    except ChartImageUploadError as exc:
        checks.append(Check("same bytes again -> already existed", False, str(exc)))

    checks.append(
        Check(
            "URL is https and <= 2000 chars",
            first.url.startswith("https://") and len(first.url) <= MAX_URL_LENGTH,
            f"{len(first.url)} chars",
        )
    )

    try:
        response = http_get(first.url)
    except httpx.HTTPError as exc:
        checks.append(Check("anonymous GET of the public URL", False, repr(exc)))
        return checks

    checks.append(
        Check(
            "anonymous GET -> HTTP 200",
            response.status_code == 200,
            f"HTTP {response.status_code} (403 = bucket not public; 404 = wrong base URL)",
        )
    )
    if response.status_code != 200:
        return checks

    content_type = response.headers.get("content-type", "")
    checks.append(
        Check(
            "Content-Type is image/png",
            content_type.split(";")[0].strip() == "image/png",
            content_type,
        )
    )
    cache_control = response.headers.get("cache-control", "")
    checks.append(
        Check(
            "Cache-Control contains immutable",
            "immutable" in cache_control,
            cache_control,
        )
    )
    checks.append(
        Check(
            "served bytes are exactly what was uploaded",
            response.content == png,
            f"{len(response.content)} bytes",
        )
    )
    try:
        size = Image.open(BytesIO(response.content)).size
        checks.append(Check("served image is 1024x768", size == IMAGE_SIZE, str(size)))
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        checks.append(Check("served image is 1024x768", False, repr(exc)))
    return checks


def format_report(checks: list[Check]) -> str:
    lines = []
    for check in checks:
        mark = "PASS" if check.passed else "FAIL"
        suffix = f"  -- {check.detail}" if check.detail else ""
        lines.append(f"[{mark}] {check.name}{suffix}")
    return "\n".join(lines)


def main() -> int:
    uploader = build_uploader_from_env()
    if uploader is None:
        print(
            "Image hosting is not configured: set CHART_GCS_BUCKET to a valid "
            "bucket name (and have Application Default Credentials available)."
        )
        return 2
    checks = run_smoke(uploader)
    print(format_report(checks))
    ok = all(check.passed for check in checks)
    print("\nSMOKE TEST " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
