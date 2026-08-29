import datetime as dt

from app.domain.feature_builder import HistoricalPricePoint
from app.domain.technical_signal_builder import (
    MOVING_AVERAGE_WINDOW,
    RANGE_POSITION_LOW_THRESHOLD,
    RANGE_WINDOW,
    build_low_with_rising_signal,
)

TARGET_DATE = dt.date(2026, 8, 27)


def _make_history(
    closes: list[float], *, start: dt.date = dt.date(2026, 7, 1)
) -> list[HistoricalPricePoint]:
    return [
        HistoricalPricePoint(
            trading_date=start + dt.timedelta(days=i),
            close=close,
            volume=1000.0,
            turnover=1000.0,
        )
        for i, close in enumerate(closes)
    ]


# --- happy path: low position + fresh MA5 crossover ---------------------------


def test_true_when_low_in_range_and_ma5_crossover_happens():
    """20 天下滑後打平在低檔，昨日收盤仍 <= 昨日 MA5，今日一舉站上今日
    MA5——低檔 + 翻多交叉同時成立，必須是 True。"""
    closes = [
        100,
        98,
        96,
        94,
        92,
        90,
        88,
        86,
        84,
        82,
        80,
        80,
        80,
        80,
        80,
        80,
        80,
        80,
        79,
        79,
    ]
    result = build_low_with_rising_signal(
        target_date=TARGET_DATE, today_close=83, history=_make_history(closes)
    )
    assert result is True


# --- insufficient data ---------------------------------------------------------


def test_none_when_fewer_than_range_window_valid_points():
    closes = [100.0] * (RANGE_WINDOW - 1)
    result = build_low_with_rising_signal(
        target_date=TARGET_DATE, today_close=100.0, history=_make_history(closes)
    )
    assert result is None


def test_not_none_at_exactly_range_window_points():
    """剛好 RANGE_WINDOW 筆有效資料是邊界值，不算「資料不足」，必須能
    正常算出結果（True 或 False 皆可，但不能是 None）。"""
    closes = [90.0] * (RANGE_WINDOW - 1) + [79.0]
    result = build_low_with_rising_signal(
        target_date=TARGET_DATE, today_close=83.0, history=_make_history(closes)
    )
    assert result is not None


# --- low-position-only failures -------------------------------------------------


def test_false_when_price_is_near_the_top_of_the_range():
    """即使今日收盤創新高、動能十足，只要不在區間低檔就不算「低檔且
    起漲」——這個訊號的「低檔」條件是硬性的，不能被強勢動能取代。"""
    closes = [
        100,
        102,
        104,
        106,
        108,
        110,
        112,
        114,
        116,
        118,
        120,
        122,
        124,
        126,
        128,
        130,
        132,
        134,
        136,
        138,
    ]
    result = build_low_with_rising_signal(
        target_date=TARGET_DATE, today_close=140.0, history=_make_history(closes)
    )
    assert result is False


# --- crossover-only failures: not a "just crossed" event -----------------------


def test_false_when_already_sitting_above_ma5_for_a_while():
    """已經站上 5 日均線一段時間的股票是「已經強勢」，不是「剛起漲」。
    這個訊號必須要求前一日仍在均線之下（或等於），不能只看今日
    close > MA5 就判定為 True。"""
    closes = [
        80,
        80,
        80,
        80,
        80,
        85,
        86,
        87,
        88,
        89,
        90,
        91,
        92,
        93,
        94,
        95,
        96,
        97,
        98,
        99,
    ]
    result = build_low_with_rising_signal(
        target_date=TARGET_DATE, today_close=100.0, history=_make_history(closes)
    )
    assert result is False


def test_false_when_still_below_ma5_today():
    """低檔沒錯，但今天還沒站上均線——還沒真正發生交叉，不能提早判定
    為起漲。"""
    closes = [
        100,
        98,
        96,
        94,
        92,
        90,
        88,
        86,
        84,
        82,
        80,
        80,
        80,
        80,
        80,
        80,
        80,
        80,
        79,
        79,
    ]
    result = build_low_with_rising_signal(
        target_date=TARGET_DATE, today_close=79.5, history=_make_history(closes)
    )
    assert result is False


# --- degenerate range (no division by zero) -------------------------------------


def test_flat_range_does_not_raise_and_returns_false():
    """20 天完全走平（最高=最低），區間百分位定義上不能除以零；今日
    收盤等於這個唯一價位時，視為區間下緣（低檔成立），但因為沒有真正
    的均線交叉（本來就持平在均線上），整體仍應為 False，不能拋出例
    外。"""
    closes = [100.0] * RANGE_WINDOW
    result = build_low_with_rising_signal(
        target_date=TARGET_DATE, today_close=100.0, history=_make_history(closes)
    )
    assert result is False


# --- no look-ahead ---------------------------------------------------------------


def test_target_date_row_is_defensively_excluded():
    """target_date 當天（或之後）的歷史資料列，即使被呼叫端誤傳進來，
    也必須被排除，不能偷看未來數據。"""
    closes = [
        100,
        98,
        96,
        94,
        92,
        90,
        88,
        86,
        84,
        82,
        80,
        80,
        80,
        80,
        80,
        80,
        80,
        80,
        79,
        79,
    ]
    history = _make_history(closes)
    history.append(
        HistoricalPricePoint(
            trading_date=TARGET_DATE, close=1.0, volume=1.0, turnover=1.0
        )
    )
    result = build_low_with_rising_signal(
        target_date=TARGET_DATE, today_close=83.0, history=history
    )
    assert result is True  # same as the happy-path case, unaffected by the bad row


def test_non_positive_closes_are_excluded_from_valid_history():
    """收盤價 <= 0（資料異常）的歷史列必須被排除在有效資料之外，不能
    拿去算區間或均線，否則可能扭曲低點或直接造成除以零之類的錯誤。"""
    closes = [
        100,
        98,
        96,
        94,
        92,
        90,
        88,
        86,
        84,
        82,
        80,
        80,
        80,
        80,
        80,
        80,
        80,
        80,
        79,
        79,
    ]
    history = _make_history(closes)
    # Extra bad row on its own distinct, earlier date — should simply
    # be filtered out, not shift or corrupt the real 20-session window.
    history.append(
        HistoricalPricePoint(
            trading_date=dt.date(2026, 6, 15),
            close=-5.0,
            volume=1.0,
            turnover=1.0,
        )
    )
    result = build_low_with_rising_signal(
        target_date=TARGET_DATE, today_close=83.0, history=history
    )
    assert result is True


# --- defensive today_close validation -------------------------------------------


def test_none_when_today_close_is_zero():
    """今日收盤價不合法（<= 0）時必須直接回傳 None，不能讓一個異常值
    偷偷混進區間百分位或均線比較的計算——即使正常 pipeline 裡
    CandidateBuilder 應該已經保證 close 是正值，這個 domain 函式也
    不該完全依賴呼叫端自律。"""
    closes = [
        100,
        98,
        96,
        94,
        92,
        90,
        88,
        86,
        84,
        82,
        80,
        80,
        80,
        80,
        80,
        80,
        80,
        80,
        79,
        79,
    ]
    result = build_low_with_rising_signal(
        target_date=TARGET_DATE, today_close=0.0, history=_make_history(closes)
    )
    assert result is None


def test_none_when_today_close_is_negative():
    closes = [
        100,
        98,
        96,
        94,
        92,
        90,
        88,
        86,
        84,
        82,
        80,
        80,
        80,
        80,
        80,
        80,
        80,
        80,
        79,
        79,
    ]
    result = build_low_with_rising_signal(
        target_date=TARGET_DATE, today_close=-10.0, history=_make_history(closes)
    )
    assert result is None


# --- duplicate trading dates (CodeRabbit review on PR #31) ----------------------


def test_none_when_row_count_reaches_range_window_only_via_duplicate_dates():
    """RANGE_WINDOW 檢查的必須是「不重複的交易日數」，不是單純的
    row 數量。20 筆歷史資料裡如果其中一天被重複列了兩次，代表背後
    只有 19 個真正不同的交易日，不能靠重複那一天湊滿 20 筆就當作
    「資料齊全」去硬算區間與均線——必須回傳 None，理由跟
    app.domain.institutional_flow_builder 對重複日期的防禦邏輯一致。"""
    history = _make_history(
        [100, 98, 96, 94, 92, 90, 88, 86, 84, 82, 80, 80, 80, 80, 80, 80, 80, 80, 79]
    )
    # Duplicate the last date instead of adding a genuinely new
    # trading day — 20 rows total, but only 19 distinct dates.
    duplicate_last_date = HistoricalPricePoint(
        trading_date=history[-1].trading_date,
        close=79.0,
        volume=1000.0,
        turnover=1000.0,
    )
    history_with_duplicate = history + [duplicate_last_date]

    result = build_low_with_rising_signal(
        target_date=TARGET_DATE, today_close=83.0, history=history_with_duplicate
    )
    assert result is None


def test_true_when_the_same_20_distinct_dates_have_no_duplicates():
    """對照組：確保上面的修正沒有誤傷正常情況——20 筆資料、20 個真正
    不同的交易日，且無重複，仍應正常算出結果（沿用既有的低檔+翻多
    交叉 happy path 案例）。"""
    closes = [
        100,
        98,
        96,
        94,
        92,
        90,
        88,
        86,
        84,
        82,
        80,
        80,
        80,
        80,
        80,
        80,
        80,
        80,
        79,
        79,
    ]
    result = build_low_with_rising_signal(
        target_date=TARGET_DATE, today_close=83, history=_make_history(closes)
    )
    assert result is True


# --- constants sanity (guards against accidental drift) -------------------------


def test_range_window_is_at_least_ma5_window_plus_one():
    """RANGE_WINDOW 必須 >= MOVING_AVERAGE_WINDOW + 1，這樣「資料是否
    足夠」只需要看一個門檻（RANGE_WINDOW），一旦低檔判斷的資料夠了，
    均線交叉判斷的資料也一定夠——不需要兩套獨立的「資料不足」邏輯。"""
    assert RANGE_WINDOW >= MOVING_AVERAGE_WINDOW + 1


def test_threshold_is_the_documented_30_percent():
    assert RANGE_POSITION_LOW_THRESHOLD == 0.30
