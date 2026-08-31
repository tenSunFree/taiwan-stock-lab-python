import datetime as dt

from app.ingestion.eps_mapper import RawCumulativeEps, build_raw_cumulative_eps_points


def _real_twse_row(**overrides) -> dict:
    """
    Base row shape copied directly from a real fetch of
    https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci (台泥, 2026
    Q2), with unrelated income-statement columns trimmed for
    readability — only the columns this mapper actually reads are
    kept plus a couple of representative "noise" columns, to confirm
    the mapper ignores fields it doesn't need rather than choking on
    an unexpectedly-shaped row.
    """
    row = {
        "出表日期": "1150831",
        "年度": "115",
        "季別": "2",
        "公司代號": "1101",
        "公司名稱": "台泥",
        "營業收入": "71289957.00",
        "營業成本": "58298233.00",
        "基本每股盈餘（元）": "0.38",
    }
    row.update(overrides)
    return row


def _real_tpex_row(**overrides) -> dict:
    """
    Base row shape copied directly from a real fetch of
    http://mopsfin.twse.com.tw/opendata/t187ap06_O_ci.csv (茂生農經,
    stock 1240, 2026 Q2) — verified identical field names/format to
    the TWSE endpoint, only the `_L_`/`_O_` segment of the source URL
    differs.
    """
    row = {
        "出表日期": "1150830",
        "年度": "115",
        "季別": "2",
        "公司代號": "1240",
        "公司名稱": "茂生農經",
        "營業收入": "1440672.00",
        "營業成本": "1267704.00",
        "基本每股盈餘（元）": "2.85",
    }
    row.update(overrides)
    return row


def test_real_twse_response_row_parses_correctly():
    rows = [_real_twse_row()]
    points = build_raw_cumulative_eps_points(rows=rows)
    assert points == [
        RawCumulativeEps(
            stock_id="1101",
            fiscal_year=2026,
            quarter=2,
            cumulative_eps=0.38,
            batch_report_date=dt.date(2026, 8, 31),
        )
    ]


def test_real_tpex_response_row_parses_correctly():
    """
    Confirms the SAME mapper (no TPEx-specific adapter needed) covers
    the TPEx general-industry endpoint — verified field-for-field
    identical to TWSE's, differing only in the source URL's `_L_`
    vs `_O_` segment.
    """
    rows = [_real_tpex_row()]
    points = build_raw_cumulative_eps_points(rows=rows)
    assert points == [
        RawCumulativeEps(
            stock_id="1240",
            fiscal_year=2026,
            quarter=2,
            cumulative_eps=2.85,
            batch_report_date=dt.date(2026, 8, 30),
        )
    ]


def test_multiple_companies_same_batch_all_share_batch_report_date():
    """
    Regression-anchoring test for the module's central design
    finding: every row in a single fetch carries the SAME 出表日期
    regardless of company — confirmed against a real multi-company
    response. This is exactly why batch_report_date must never be
    treated as if it were a per-company disclosure date.
    """
    rows = [
        _real_twse_row(公司代號="1101", **{"基本每股盈餘（元）": "0.38"}),
        _real_twse_row(
            公司代號="1102", 公司名稱="亞泥", **{"基本每股盈餘（元）": "2.13"}
        ),
        _real_twse_row(
            公司代號="1590", 公司名稱="亞德客-KY", **{"基本每股盈餘（元）": "29.91"}
        ),
    ]
    points = build_raw_cumulative_eps_points(rows=rows)
    assert len(points) == 3
    assert {point.batch_report_date for point in points} == {dt.date(2026, 8, 31)}
    assert {point.stock_id for point in points} == {"1101", "1102", "1590"}


def test_negative_cumulative_eps_parses_correctly_as_a_loss():
    # Real example from the fetched TPEx response: 濱川 (1569), a
    # cumulative Q2 loss.
    row = _real_tpex_row(
        公司代號="1569", 公司名稱="濱川", **{"基本每股盈餘（元）": "-1.67"}
    )
    points = build_raw_cumulative_eps_points(rows=[row])
    assert points[0].cumulative_eps == -1.67


def test_missing_eps_row_dropped_not_guessed():
    """
    Real-world cause: financial-industry / holding-company rows report
    EPS via a different TWSE/TPEx endpoint entirely and simply don't
    carry this column (or carry an empty string) on the general-
    industry endpoint this mapper targets.
    """
    row = _real_twse_row(**{"基本每股盈餘（元）": ""})
    assert build_raw_cumulative_eps_points(rows=[row]) == []


def test_missing_stock_id_row_dropped():
    row = _real_twse_row(公司代號="")
    assert build_raw_cumulative_eps_points(rows=[row]) == []


def test_missing_fiscal_year_row_dropped():
    row = _real_twse_row(年度="")
    assert build_raw_cumulative_eps_points(rows=[row]) == []


def test_missing_quarter_row_dropped():
    row = _real_twse_row(季別="")
    assert build_raw_cumulative_eps_points(rows=[row]) == []


def test_quarter_out_of_range_row_dropped():
    row = _real_twse_row(季別="5")
    assert build_raw_cumulative_eps_points(rows=[row]) == []


def test_non_numeric_eps_row_dropped_not_raised():
    row = _real_twse_row(**{"基本每股盈餘（元）": "N/A"})
    assert build_raw_cumulative_eps_points(rows=[row]) == []


def test_malformed_batch_report_date_row_dropped():
    row = _real_twse_row(出表日期="not-a-date")
    assert build_raw_cumulative_eps_points(rows=[row]) == []


def test_missing_batch_report_date_row_dropped():
    row = _real_twse_row(出表日期="")
    assert build_raw_cumulative_eps_points(rows=[row]) == []


def test_empty_rows_returns_empty_list():
    assert build_raw_cumulative_eps_points(rows=[]) == []


def test_q1_batch_report_date_parses_correctly():
    row = _real_twse_row(出表日期="1150515", 年度="115", 季別="1")
    points = build_raw_cumulative_eps_points(rows=[row])
    assert points[0].batch_report_date == dt.date(2026, 5, 15)


def test_annual_q4_fiscal_year_boundary_parses_correctly():
    # Year-end batch, published the following ROC year. Parsing this
    # row is still valid at the mapper level (Q4 rows are only
    # rejected later, at the eps_period_converter layer, where the
    # cumulative -> standalone conversion for Q4 is not yet
    # supported).
    row = _real_twse_row(出表日期="1160331", 年度="115", 季別="4")
    points = build_raw_cumulative_eps_points(rows=[row])
    assert points[0].fiscal_year == 2026  # the report's own fiscal year
    assert points[0].batch_report_date == dt.date(2027, 3, 31)  # published in 2027
