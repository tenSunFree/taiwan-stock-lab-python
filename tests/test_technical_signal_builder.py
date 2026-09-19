import datetime as dt
from decimal import Decimal

import pytest

from app.domain.feature_builder import HistoricalPricePoint
from app.domain.price_ticks import calculate_limit_up_price
from app.domain.technical_signal_builder import (
    MOVING_AVERAGE_WINDOW,
    RANGE_POSITION_LOW_THRESHOLD,
    RANGE_WINDOW,
    LowFirstLimitUpSignal,
    build_low_first_limit_up_signal,
    build_low_with_rising_signal,
    estimate_previous_session_limit_up,
)

TARGET_DATE = dt.date(2026, 8, 27)


def _approx_limit_up(reference_close: float) -> float:
    return float(calculate_limit_up_price(Decimal(str(reference_close))))


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


# =================================================================================
# build_low_first_limit_up_signal() / estimate_previous_session_limit_up()
# 「低檔首板」— now returns a structured LowFirstLimitUpSignal, not a bare
# bool | None; `.matched` is the old tri-state result.
# =================================================================================

# --- short-circuit on is_close_limit_up -----------------------------------------


def test_first_board_false_when_not_limit_up_today_even_with_insufficient_history():
    """今天根本沒漲停時，整體結果就是 False，即使歷史資料不足以算低檔
    區間——不需要為了一個已知為 False 的結論去湊資料。is_low／
    previous_session_limit_up_estimated 這兩個子欄位在這個短路情境下
    根本不會被嘗試計算，必須維持 None，不能被誤植成 False。"""
    result = build_low_first_limit_up_signal(
        target_date=TARGET_DATE,
        today_close=100.0,
        is_close_limit_up=False,
        history=_make_history([100.0] * (RANGE_WINDOW - 1)),
    )
    assert result == LowFirstLimitUpSignal(
        matched=False,
        is_low=None,
        range_position=None,
        is_close_limit_up=False,
        previous_session_limit_up_estimated=None,
    )


def test_first_board_false_when_not_limit_up_today_even_in_low_range():
    closes = [100 - i for i in range(RANGE_WINDOW)]
    result = build_low_first_limit_up_signal(
        target_date=TARGET_DATE,
        today_close=closes[-1] + 0.5,
        is_close_limit_up=False,
        history=_make_history(closes),
    )
    assert result.matched is False
    assert result.is_low is None
    assert result.previous_session_limit_up_estimated is None


# --- happy path: limit-up + low + genuinely first board -------------------------


def test_first_board_true_when_limit_up_low_and_previous_session_was_not_limit_up():
    # 20 天從 100 一路跌到 50 後打平在低檔盤整，前一交易日收盤 50，跟
    # 再前一日（同樣是 50）比較並不是漲停——今天從 50 漲停到約 55，
    # 相對 20 日區間 [50, 100] 仍落在最低 30% 以內 -> 低檔 + 首板同時
    # 成立，必須是 True，且結構化欄位要能各自對得上。
    closes = [100, 95, 90, 85, 80, 75, 70, 65, 60, 55, 50] + [50.0] * 9
    assert len(closes) == RANGE_WINDOW
    history = _make_history(closes)
    today_close = _approx_limit_up(closes[-1])  # today's close IS limit-up

    result = build_low_first_limit_up_signal(
        target_date=TARGET_DATE,
        today_close=today_close,
        is_close_limit_up=True,
        history=history,
    )
    assert result.matched is True
    assert result.is_low is True
    assert result.range_position == pytest.approx(0.1)
    assert result.is_close_limit_up is True
    assert result.previous_session_limit_up_estimated is False
    assert result.previous_session_check_provisional is True


# --- limit-up + low, but NOT the first board (previous session already up) ------


def test_first_board_false_when_previous_session_was_already_a_limit_up():
    """前一天自己就已經是（近似判定的）漲停——代表今天是連板，不是首
    板。previous_session_limit_up_estimated 要明確顯示 True（是造成
    結果為 False 的一個原因），而不是含糊地整包變 None。"""
    day_before_previous_close = 90.0
    previous_close = _approx_limit_up(day_before_previous_close)  # 前一天就漲停
    closes = [100 - i for i in range(10)] + [90.0] * 9 + [previous_close]
    history = _make_history(closes)
    today_close = _approx_limit_up(float(previous_close))

    result = build_low_first_limit_up_signal(
        target_date=TARGET_DATE,
        today_close=today_close,
        is_close_limit_up=True,
        history=history,
    )
    assert result.matched is False
    assert result.previous_session_limit_up_estimated is True


# --- limit-up + first board, but NOT low in range --------------------------------


def test_first_board_false_when_limit_up_and_first_board_but_not_in_low_range():
    """今天漲停、也是首板，但目前股價落在近20日區間高檔——「低檔」條
    件不成立，整體仍必須是 False，且 is_low 要明確為 False（是造成
    結果為 False 的原因），previous_session_limit_up_estimated 則正常
    解出 False（首板本身是成立的）。"""
    closes = [80 + i for i in range(RANGE_WINDOW)]  # steadily rising, ends near high
    previous_close = closes[-1]
    today_close = _approx_limit_up(previous_close)

    result = build_low_first_limit_up_signal(
        target_date=TARGET_DATE,
        today_close=today_close,
        is_close_limit_up=True,
        history=_make_history(closes),
    )
    assert result.matched is False
    assert result.is_low is False
    assert result.previous_session_limit_up_estimated is False


# --- insufficient data (only checked once is_close_limit_up is True) ------------


def test_first_board_none_when_limit_up_today_but_not_enough_trailing_history():
    result = build_low_first_limit_up_signal(
        target_date=TARGET_DATE,
        today_close=110.0,
        is_close_limit_up=True,
        history=_make_history([100.0] * (RANGE_WINDOW - 1)),
    )
    assert result.matched is None
    assert result.is_low is None
    assert result.range_position is None


def test_first_board_matched_none_but_previous_session_check_still_resolves():
    """核心可解釋化行為：即使歷史資料不足以算 20 日區間（is_low 只能
    是 None），只要至少有 2 筆有效歷史資料，
    previous_session_limit_up_estimated 仍應獨立算出結果，不必被 is_low
    的資料不足拖著一起變 None——這樣即使整體 matched 是 None，讀者仍
    能看到「首板」這一半條件到底成不成立。"""
    closes = [50.0] * (RANGE_WINDOW - 5)  # fewer than RANGE_WINDOW, but >= 2
    result = build_low_first_limit_up_signal(
        target_date=TARGET_DATE,
        today_close=110.0,
        is_close_limit_up=True,
        history=_make_history(closes),
    )
    assert result.matched is None
    assert result.is_low is None
    assert result.range_position is None
    assert result.previous_session_limit_up_estimated is not None


def test_first_board_none_when_today_close_is_non_positive_even_if_marked_limit_up():
    """即使呼叫端誤傳 is_close_limit_up=True，today_close 本身不合法
    (<= 0) 時仍必須整包回傳 None（含 previous_session 子欄位），不能
    讓異常值進到低檔/首板計算。"""
    result = build_low_first_limit_up_signal(
        target_date=TARGET_DATE,
        today_close=0.0,
        is_close_limit_up=True,
        history=_make_history([100.0] * RANGE_WINDOW),
    )
    assert result == LowFirstLimitUpSignal(
        matched=None,
        is_low=None,
        range_position=None,
        is_close_limit_up=True,
        previous_session_limit_up_estimated=None,
    )


def test_first_board_none_when_duplicate_trading_date_in_history():
    """重複日期會讓整個歷史窗口不可信，因此 is_low 和
    previous_session_limit_up_estimated 都必須是 None，不能只擋掉其中
    一個。"""
    history = _make_history([100.0] * RANGE_WINDOW)
    history.append(
        HistoricalPricePoint(
            trading_date=history[-1].trading_date,
            close=100.0,
            volume=1000.0,
            turnover=1000.0,
        )
    )
    result = build_low_first_limit_up_signal(
        target_date=TARGET_DATE,
        today_close=110.0,
        is_close_limit_up=True,
        history=history,
    )
    assert result.matched is None
    assert result.is_low is None
    assert result.previous_session_limit_up_estimated is None


# --- estimate_previous_session_limit_up() unit tests -----------------------------


def test_estimate_previous_session_true_when_close_matches_approx_limit_up():
    reference_close = 50.0
    previous_close = _approx_limit_up(reference_close)
    history = _make_history([reference_close, previous_close])
    assert estimate_previous_session_limit_up(history) is True


def test_estimate_previous_session_false_for_an_ordinary_move():
    history = _make_history([50.0, 51.0])
    assert estimate_previous_session_limit_up(history) is False


def test_estimate_previous_session_none_with_fewer_than_two_points():
    history = _make_history([50.0])
    assert estimate_previous_session_limit_up(history) is None
