import datetime as dt

import pytest

from app.ingestion.regulatory_mapper import (
    RegulatorySourceFormatError,
    build_tpex_attention_statuses,
    build_tpex_disposition_statuses,
)

# --- Attention -------------------------------------------------------------
#
# A trimmed-down but REAL slice of the fixture captured from
# https://www.tpex.org.tw/www/zh-tw/bulletin/attention — same field
# names, same cross-date-window shape (stock 30811 appears on THREE
# different 公告日期 within one response), not synthesized.

ATTENTION_FIELDS = [
    "編號",
    "證券代號",
    "證券名稱",
    "累計",
    "注意交易資訊",
    "公告日期",
    "收盤價",
    "本益比",
    "link",
]

ATTENTION_PAYLOAD = {
    "tables": [
        {
            "fields": ATTENTION_FIELDS,
            "data": [
                [
                    1,
                    "30811",
                    "聯亞一",
                    30,
                    "最近六個營業日累積漲幅達32.74%",
                    "115/08/21",
                    "205.00",
                    "N/A",
                    "link1",
                ],
                [
                    1,
                    "3259",
                    "鑫創",
                    30,
                    "最近六個營業日累積跌幅達34.65%",
                    "115/08/21",
                    "9.81",
                    "N/A",
                    "link2",
                ],
                [
                    2,
                    "30811",
                    "聯亞一",
                    18,
                    "最近六個營業日累積漲幅達29.19%",
                    "115/08/20",
                    "198.00",
                    "N/A",
                    "link3",
                ],
                [
                    3,
                    "30811",
                    "聯亞一",
                    20,
                    "最近六個營業日累積漲幅達27.62%",
                    "115/08/19",
                    "195.00",
                    "N/A",
                    "link4",
                ],
            ],
        }
    ],
    "date": "20260813~20260822",
    "stat": "ok",
}


def test_build_tpex_attention_statuses_matches_only_exact_target_date():
    """
    The core regression this design exists for: stock 30811 appears on
    THREE different dates in the same response (confirmed via a real
    fixture). Querying target_date=2026-08-21 must return ONLY that
    day's row — not silently pick up 08-20's or 08-19's older
    "累計=18/20" reason text just because the stock_id matches.
    """
    result = build_tpex_attention_statuses(
        target_date=dt.date(2026, 8, 21), payload=ATTENTION_PAYLOAD
    )

    assert set(result.keys()) == {"30811", "3259"}
    assert result["30811"].is_attention is True
    assert result["30811"].attention_reason == "最近六個營業日累積漲幅達32.74%"
    assert result["30811"].trading_date == dt.date(2026, 8, 21)


def test_build_tpex_attention_statuses_uses_the_prior_days_own_row_not_todays():
    """Querying a DIFFERENT date must pick up THAT date's row, proving
    the exact-match filter isn't accidentally always resolving to the
    newest row in the payload."""
    result = build_tpex_attention_statuses(
        target_date=dt.date(2026, 8, 20), payload=ATTENTION_PAYLOAD
    )

    assert set(result.keys()) == {"30811"}
    assert result["30811"].attention_reason == "最近六個營業日累積漲幅達29.19%"


def test_build_tpex_attention_statuses_no_row_for_target_date_is_empty():
    """A date with no matching announcement at all (e.g. querying a
    date entirely outside the response's coverage) is a legitimate
    empty result, not an error."""
    result = build_tpex_attention_statuses(
        target_date=dt.date(2026, 8, 1), payload=ATTENTION_PAYLOAD
    )
    assert result == {}


def test_build_tpex_attention_statuses_raises_on_missing_required_column():
    payload = {
        "tables": [
            {
                "fields": [
                    "編號",
                    "證券代號",
                    "證券名稱",
                ],  # 注意交易資訊/公告日期 missing
                "data": [],
            }
        ]
    }
    with pytest.raises(RegulatorySourceFormatError, match="注意交易資訊"):
        build_tpex_attention_statuses(target_date=dt.date(2026, 8, 21), payload=payload)


def test_build_tpex_attention_statuses_raises_on_missing_tables_key():
    with pytest.raises(RegulatorySourceFormatError):
        build_tpex_attention_statuses(target_date=dt.date(2026, 8, 21), payload={})


def test_build_tpex_attention_statuses_empty_data_is_legitimate_zero():
    """Structurally intact response (fields present) with zero rows —
    a genuinely legitimate "nothing currently flagged," must NOT raise."""
    payload = {"tables": [{"fields": ATTENTION_FIELDS, "data": []}]}
    result = build_tpex_attention_statuses(
        target_date=dt.date(2026, 8, 21), payload=payload
    )
    assert result == {}


# --- Disposition -------------------------------------------------------------
#
# Trimmed real slice from https://www.tpex.org.tw/www/zh-tw/bulletin/disposal

DISPOSITION_FIELDS = [
    "編號",
    "公布日期",
    "證券代號",
    "證券名稱",
    "累計",
    "處置起訖時間",
    "處置原因",
    "處置內容",
    "收盤價",
    "本益比",
    " ",
]

DISPOSITION_PAYLOAD = {
    "tables": [
        {
            "fields": DISPOSITION_FIELDS,
            "data": [
                [
                    1,
                    "115/08/21",
                    "30811",
                    "聯亞一(../../mainboard/listed/company-detail.html?code=30811)",
                    7,
                    "115/08/24~115/08/28",
                    "連續3個營業日達注意標準",
                    "改以人工管制之撮合終端機執行撮合作業（約每2分鐘撮合一次）",
                    "205.00",
                    "N/A",
                    "link",
                ],
                [
                    2,
                    "115/08/20",
                    "700811",
                    "興櫃債5C購1",
                    1,
                    "115/08/21~115/08/27",
                    "連續3個營業日達注意標準",
                    "改以人工管制之撮合終端機執行撮合作業",
                    "2.07",
                    "N/A",
                    "link",
                ],
            ],
        }
    ],
    "date": "20260819~20260824",
    "stat": "ok",
}


def test_build_tpex_disposition_statuses_matches_active_period_not_announcement_date():
    """
    The key design difference from attention: 公布日期 (2026-08-21) is
    NOT when we check membership — the ACTIVE PERIOD is
    2026-08-24~2026-08-28. A target_date equal to the announcement
    date but BEFORE the period starts must NOT match.
    """
    # target_date == 公布日期, but before the active period starts
    result_on_announcement_day = build_tpex_disposition_statuses(
        target_date=dt.date(2026, 8, 21), payload=DISPOSITION_PAYLOAD
    )
    assert "30811" not in result_on_announcement_day

    # target_date inside the active period
    result_during_period = build_tpex_disposition_statuses(
        target_date=dt.date(2026, 8, 26), payload=DISPOSITION_PAYLOAD
    )
    assert "30811" in result_during_period
    assert result_during_period["30811"].is_disposition is True
    assert result_during_period["30811"].disposition_start_date == dt.date(2026, 8, 24)
    assert result_during_period["30811"].disposition_end_date == dt.date(2026, 8, 28)
    assert result_during_period["30811"].disposition_reason == "連續3個營業日達注意標準"
    assert (
        result_during_period["30811"].disposition_measure
        == "改以人工管制之撮合終端機執行撮合作業（約每2分鐘撮合一次）"
    )


def test_build_tpex_disposition_statuses_period_boundaries_are_inclusive():
    result_start = build_tpex_disposition_statuses(
        target_date=dt.date(2026, 8, 24), payload=DISPOSITION_PAYLOAD
    )
    assert "30811" in result_start

    result_end = build_tpex_disposition_statuses(
        target_date=dt.date(2026, 8, 28), payload=DISPOSITION_PAYLOAD
    )
    assert "30811" in result_end

    result_after = build_tpex_disposition_statuses(
        target_date=dt.date(2026, 8, 29), payload=DISPOSITION_PAYLOAD
    )
    assert "30811" not in result_after


def test_build_tpex_disposition_statuses_prefers_newest_announcement_when_overlapping():
    """Two rows for the same stock both covering target_date -> the
    one with the later 公布日期 wins, mirroring
    valuation_mapper's "prefer the newest applicable record"
    principle."""
    payload = {
        "tables": [
            {
                "fields": DISPOSITION_FIELDS,
                "data": [
                    [
                        1,
                        "115/08/10",
                        "1234",
                        "測試",
                        1,
                        "115/08/12~115/08/20",
                        "OLD reason",
                        "OLD measure",
                        "100.00",
                        "N/A",
                        "link",
                    ],
                    [
                        2,
                        "115/08/15",
                        "1234",
                        "測試",
                        2,
                        "115/08/16~115/08/22",
                        "NEW reason",
                        "NEW measure",
                        "105.00",
                        "N/A",
                        "link",
                    ],
                ],
            }
        ]
    }
    # 2026-08-17 falls inside BOTH overlapping periods
    result = build_tpex_disposition_statuses(
        target_date=dt.date(2026, 8, 17), payload=payload
    )
    assert result["1234"].disposition_reason == "NEW reason"
    assert result["1234"].disposition_measure == "NEW measure"


def test_build_tpex_disposition_statuses_raises_on_missing_required_column():
    payload = {
        "tables": [
            {"fields": ["編號", "證券代號"], "data": []}  # missing several required
        ]
    }
    with pytest.raises(RegulatorySourceFormatError):
        build_tpex_disposition_statuses(
            target_date=dt.date(2026, 8, 21), payload=payload
        )


def test_build_tpex_disposition_statuses_skips_row_with_unparseable_period():
    payload = {
        "tables": [
            {
                "fields": DISPOSITION_FIELDS,
                "data": [
                    [
                        1,
                        "115/08/21",
                        "9999",
                        "測試",
                        1,
                        "not a valid period",
                        "reason",
                        "measure",
                        "1.00",
                        "N/A",
                        "link",
                    ]
                ],
            }
        ]
    }
    result = build_tpex_disposition_statuses(
        target_date=dt.date(2026, 8, 21), payload=payload
    )
    assert result == {}
