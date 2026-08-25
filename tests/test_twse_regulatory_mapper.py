import datetime as dt

import pytest

from app.ingestion.regulatory_mapper import RegulatorySourceFormatError
from app.ingestion.twse_regulatory_mapper import (
    build_twse_attention_statuses,
    build_twse_disposition_statuses,
)

# --- Attention (notice.html) -------------------------------------------------
#
# REAL_EMPTY_NOTICE_HTML is the ACTUAL raw HTML returned by
# https://www.twse.com.tw/announcement/notice?response=html on
# 2026-08-22 (captured via Invoke-WebRequest -UseBasicParsing with
# correct UTF-8 decoding) — a day with zero currently-flagged
# attention stocks, so <tbody> is genuinely empty. Not synthesized.

REAL_EMPTY_NOTICE_HTML = """<!doctype html>
<html lang="zh">
<head>
    <meta http-equiv="Content-Type" content="text/html; charset=utf-8">
    <title> 報表 - TWSE 臺灣證券交易所</title>
</head>
<body>
<div>
        <table>
            <thead>
            <tr>
                <th colspan='8'>
                    <div>公布注意有價證券資訊 (115年08月22日 至 115年08月22日 全部上市有價證券)</div>
                </th>
            </tr>
            <tr>
                    <th>編號</th>
                    <th>證券代號</th>
                    <th>證券名稱</th>
                    <th>累計次數</th>
                    <th>注意交易資訊</th>
                    <th>日期</th>
                    <th>收盤價</th>
                    <th>本益比</th>
            </tr>
            </thead>
            <tbody>
            </tbody>
        </table>
</div>
</body>
</html>"""


def test_build_twse_attention_statuses_real_empty_tbody_is_legitimate_zero():
    """The exact real HTML captured from a live fetch on 2026-08-22 —
    structurally intact table, zero rows. Must return {} without
    raising; this is what "genuinely no attention stocks today" looks
    like from this source, not a parsing failure."""
    result = build_twse_attention_statuses(
        target_date=dt.date(2026, 8, 22), html_text=REAL_EMPTY_NOTICE_HTML
    )
    assert result == {}


def test_build_twse_attention_statuses_target_date_outside_title_window_raises():
    """The real fixture's title window is 2026-08-22 ~ 2026-08-22.
    Querying a date outside that window means the response can't be
    trusted for this target_date at all — must raise, not silently
    return {} (which would look identical to "genuinely zero")."""
    with pytest.raises(RegulatorySourceFormatError, match="does not cover"):
        build_twse_attention_statuses(
            target_date=dt.date(2026, 8, 1), html_text=REAL_EMPTY_NOTICE_HTML
        )


def _notice_html_with_rows(
    rows_html: str, *, title_range: str = "115年08月20日 至 115年08月22日"
) -> str:
    """Builds an HTML document with the SAME confirmed-real structure
    as REAL_EMPTY_NOTICE_HTML above, just with injected <tbody> rows —
    used for the header/data-parsing tests that need actual rows,
    which the one real captured fixture didn't have."""
    return f"""<!doctype html>
<html lang="zh"><body><div>
<table>
<thead>
<tr><th colspan='8'><div>公布注意有價證券資訊 ({title_range} 全部上市有價證券)</div></th></tr>
<tr>
<th>編號</th><th>證券代號</th><th>證券名稱</th><th>累計次數</th>
<th>注意交易資訊</th><th>日期</th><th>收盤價</th><th>本益比</th>
</tr>
</thead>
<tbody>
{rows_html}
</tbody>
</table>
</div></body></html>"""


def test_build_twse_attention_statuses_matches_only_exact_target_date():
    """Same cross-date-window regression as TPEx's equivalent test —
    a stock announced on more than one date within the query window
    must only match its OWN date's row."""
    html_text = _notice_html_with_rows(
        """
        <tr><td>1</td><td>2330</td><td>台積電</td><td>3</td><td>近期異常</td><td>115/08/22</td><td>600.00</td><td>20.5</td></tr>
        <tr><td>2</td><td>2330</td><td>台積電</td><td>2</td><td>OLD 原因</td><td>115/08/21</td><td>590.00</td><td>20.1</td></tr>
        """
    )
    result = build_twse_attention_statuses(
        target_date=dt.date(2026, 8, 22), html_text=html_text
    )
    assert set(result.keys()) == {"2330"}
    assert result["2330"].attention_reason == "近期異常"


def test_build_twse_attention_statuses_bare_roc_date_format_also_accepted():
    """Row-level date format isn't independently confirmed for this
    page (see module docstring) — both slash and bare ROC formats must
    parse, since we don't yet know for certain which TWSE actually
    sends."""
    html_text = _notice_html_with_rows(
        """
        <tr><td>1</td><td>2330</td><td>台積電</td><td>1</td><td>原因</td><td>1150822</td><td>600.00</td><td>20.5</td></tr>
        """
    )
    result = build_twse_attention_statuses(
        target_date=dt.date(2026, 8, 22), html_text=html_text
    )
    assert "2330" in result


def test_build_twse_attention_statuses_raises_on_missing_required_column():
    html_text = """<html><body><table><thead>
    <tr><th colspan='2'><div>公布注意有價證券資訊 (115年08月20日 至 115年08月22日)</div></th></tr>
    <tr><th>編號</th><th>證券代號</th></tr>
    </thead><tbody></tbody></table></body></html>"""
    with pytest.raises(RegulatorySourceFormatError, match="注意交易資訊"):
        build_twse_attention_statuses(
            target_date=dt.date(2026, 8, 22), html_text=html_text
        )


def test_build_twse_attention_statuses_raises_when_no_table_found():
    with pytest.raises(RegulatorySourceFormatError):
        build_twse_attention_statuses(
            target_date=dt.date(2026, 8, 22),
            html_text="<html><body>error page</body></html>",
        )


# --- Disposition (punish.html) -----------------------------------------------
#
# The overall <table>/<thead>/<tbody> structure below mirrors
# REAL_EMPTY_NOTICE_HTML's CONFIRMED real structure (same TWSE report-
# generator template family) with punish's own CONFIRMED real column
# headers and title date format (verified via a live fetch showing
# "公布處置有價證券資訊 (115/08/22 至 115/08/24)" and slash-formatted
# row dates like "115/08/24～115/08/28" for 處置起迄時間) — but the
# exact tag-level markup for a punish.html DATA row (as opposed to its
# headers, which came through the markdown-rendered fetch) is not
# independently confirmed the way notice.html's raw markup is. Recheck
# against a real raw fetch of punish.html's tag structure if issues
# come up.


def _punish_html_with_rows(
    rows_html: str, *, title_range: str = "115/08/19 至 115/08/24"
) -> str:
    return f"""<!doctype html>
<html lang="zh"><body><div>
<table>
<thead>
<tr><th colspan='10'><div>公布處置有價證券資訊 ({title_range})</div></th></tr>
<tr>
<th>編號</th><th>公布日期</th><th>證券代號</th><th>證券名稱</th><th>累計</th>
<th>處置條件</th><th>處置起迄時間</th><th>處置措施</th><th>處置內容</th><th>備註</th>
</tr>
</thead>
<tbody>
{rows_html}
</tbody>
</table>
</div></body></html>"""


def test_build_twse_disposition_statuses_matches_active_period_not_announcement_date():
    html_text = _punish_html_with_rows(
        """
        <tr><td>1</td><td>115/08/21</td><td>6225</td><td>天瀚</td><td>2</td>
        <td>連續三次</td><td>115/08/24～115/08/28</td><td>人工管制撮合</td>
        <td>約每二十五分鐘撮合一次</td><td>備註內容</td></tr>
        """
    )
    # announcement date itself, but before the active period starts
    result_on_announce_day = build_twse_disposition_statuses(
        target_date=dt.date(2026, 8, 21), html_text=html_text
    )
    assert "6225" not in result_on_announce_day

    # inside the active period
    result_during_period = build_twse_disposition_statuses(
        target_date=dt.date(2026, 8, 26), html_text=html_text
    )
    assert "6225" in result_during_period
    assert result_during_period["6225"].disposition_start_date == dt.date(2026, 8, 24)
    assert result_during_period["6225"].disposition_end_date == dt.date(2026, 8, 28)
    assert result_during_period["6225"].disposition_reason == "連續三次"
    assert result_during_period["6225"].disposition_measure == "約每二十五分鐘撮合一次"


def test_build_twse_disposition_statuses_period_boundaries_are_inclusive():
    html_text = _punish_html_with_rows(
        """
        <tr><td>1</td><td>115/08/21</td><td>6225</td><td>天瀚</td><td>2</td>
        <td>連續三次</td><td>115/08/24～115/08/28</td><td>人工管制撮合</td>
        <td>內容</td><td></td></tr>
        """
    )
    assert "6225" in build_twse_disposition_statuses(
        target_date=dt.date(2026, 8, 24), html_text=html_text
    )
    assert "6225" in build_twse_disposition_statuses(
        target_date=dt.date(2026, 8, 28), html_text=html_text
    )
    assert "6225" not in build_twse_disposition_statuses(
        target_date=dt.date(2026, 8, 29), html_text=html_text
    )


def test_build_twse_disposition_statuses_raises_on_missing_required_column():
    html_text = """<html><body><table><thead>
    <tr><th colspan='2'><div>公布處置有價證券資訊 (115/08/19 至 115/08/24)</div></th></tr>
    <tr><th>編號</th><th>證券代號</th></tr>
    </thead><tbody></tbody></table></body></html>"""
    with pytest.raises(RegulatorySourceFormatError):
        build_twse_disposition_statuses(
            target_date=dt.date(2026, 8, 21), html_text=html_text
        )


def test_build_twse_disposition_statuses_target_date_far_outside_title_window_still_works():
    """
    Unlike attention, a disposition's active period routinely extends
    PAST the title's own announcement-date query window (e.g.
    announced within the window, but the 5-trading-day disposition
    period runs past its end) — see
    build_twse_disposition_statuses's own docstring for why the
    stronger "target_date must fall inside the title's range" check
    (used for attention) is deliberately NOT applied here. A
    target_date past the title's window must still correctly match a
    row whose own active period covers it.
    """
    html_text = _punish_html_with_rows(
        """
        <tr><td>1</td><td>115/08/21</td><td>6225</td><td>天瀚</td><td>2</td>
        <td>連續三次</td><td>115/08/24～115/08/28</td><td>人工管制撮合</td>
        <td>內容</td><td></td></tr>
        """,
        title_range="115/08/19 至 115/08/24",
    )
    # target_date (08-26) is AFTER the title's own window end (08-24),
    # but still inside this row's active period (08-24~08-28) — must match.
    result = build_twse_disposition_statuses(
        target_date=dt.date(2026, 8, 26), html_text=html_text
    )
    assert "6225" in result


def test_build_twse_disposition_statuses_raises_when_title_itself_is_unparseable():
    """The title must still at least PARSE into a real date range
    (catches a wrong page / template change) — it just doesn't need to
    COVER target_date the way attention's does."""
    html_text = """<html><body><table><thead>
    <tr><th colspan='2'><div>not a real title</div></th></tr>
    <tr><th>編號</th><th>公布日期</th><th>證券代號</th><th>處置條件</th>
    <th>處置起迄時間</th><th>處置內容</th></tr>
    </thead><tbody></tbody></table></body></html>"""
    with pytest.raises(RegulatorySourceFormatError, match="could not parse"):
        build_twse_disposition_statuses(
            target_date=dt.date(2026, 8, 21), html_text=html_text
        )


def test_build_twse_disposition_statuses_empty_tbody_within_valid_window_is_legitimate_zero():
    html_text = _punish_html_with_rows("", title_range="115/08/19 至 115/08/24")
    result = build_twse_disposition_statuses(
        target_date=dt.date(2026, 8, 21), html_text=html_text
    )
    assert result == {}
