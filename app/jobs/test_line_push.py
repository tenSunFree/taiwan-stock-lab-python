"""
Manual, one-off LINE push test — sends exactly one message to your own
LINE user ID using real credentials, with NO database interaction.

This exists to verify the token works and to eyeball how the report
actually renders on a phone (line breaks, bullet characters, emoji,
etc.) before wiring anything into the real delivery pipeline.

Requires LINE_CHANNEL_ACCESS_TOKEN and LINE_TARGET_ID to be set (see
.env or your shell environment). Never commit these values.

Run: python -m app.jobs.test_line_push
"""

from __future__ import annotations

import os
import sys

from app.clients.line_client import (
    LineMessagingClient,
    LineNonRetryableError,
    LinePushError,
)


def load_env_file(path: str = ".env") -> None:
    """Minimal .env loader so this works without adding a dependency
    like python-dotenv. Only sets variables not already in os.environ,
    so real environment variables (e.g. in CI) always take priority."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    load_env_file()

    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    target_id = os.environ.get("LINE_TARGET_ID")

    if not token:
        print(
            "ERROR: LINE_CHANNEL_ACCESS_TOKEN is not set (check .env)", file=sys.stderr
        )
        return 1
    if not target_id:
        print("ERROR: LINE_TARGET_ID is not set (check .env)", file=sys.stderr)
        return 1

    client = LineMessagingClient(channel_access_token=token)

    test_message = (
        "【台股量化研究 Bot】\n\n"
        "這是一則測試訊息，確認 LINE Messaging API 連線與中文顯示正常。\n\n"
        "測試項目：\n"
        "・換行\n"
        "・全形符號（），、\n"
        "・emoji 👍\n\n"
        "本清單依公開市場資料及固定量化規則產生，"
        "僅供研究與資料整理，不構成買進、賣出或持有建議。"
    )

    try:
        result = client.push_text(target_id=target_id, text=test_message)
    except LineNonRetryableError as exc:
        print(
            f"LINE rejected the request (check token/target_id): {exc}", file=sys.stderr
        )
        return 1
    except LinePushError as exc:
        print(f"LINE push failed after retries: {exc}", file=sys.stderr)
        return 1

    print(
        f"success={result.success} status={result.status_code} request_id={result.request_id}"
    )
    print("Check your phone now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
