import datetime as dt

import pytest

from app.news.classify import EventType
from app.news.models import TAIPEI_TZ, ContentLevel, RawArticle
from app.news.preprocess import RepeatedNotice, collapse_repeats
from app.news.select import Tier, assess, pick_representatives


def mk(title, *, stock="2330", day=10, hour=8, official=True, market="TWSE", desc="d"):
    when = dt.datetime(2026, 11, day, hour, 0, 0, tzinfo=TAIPEI_TZ)
    return RawArticle(
        article_id=f"{stock}-{title}-{day}-{hour}",
        market=market,
        stock_id=stock,
        title=title,
        description=desc,
        content=None,
        source="MOPS",
        published_at=when,
        fetched_at=when,
        source_url="https://example.test",
        original_url=None,
        is_official=official,
        content_level=ContentLevel.TITLE_DESCRIPTION,
    )


def notice(title, **kw):
    return RepeatedNotice(
        article=(a := mk(title, **kw)),
        first_published_at=a.published_at,
        last_published_at=a.published_at,
        occurrences=1,
        article_ids=(a.article_id,),
    )


REPORT = "公告本公司115年第三季財務報告"
CONTRACT = "公告本公司簽訂重大契約"
PERSONNEL = "公告本公司研發主管異動"
ATTENTION = "公告本公司股票列為注意交易資訊"


def test_financial_report_is_medium():
    c = assess(notice(REPORT))
    assert c.event_type is EventType.FINANCIAL_REPORT and c.tier is Tier.MEDIUM


def test_attention_notice_is_never_a_candidate():
    assert assess(notice(ATTENTION)) is None


def test_media_and_unsupported_market_are_not_candidates():
    assert assess(notice(REPORT, official=False, market="MEDIA")) is None
    assert assess(notice(REPORT, market="MEDIA")) is None


def test_routine_subsidiary_notice_drops_one_tier():
    own = assess(notice(REPORT))
    sub = assess(notice("公告本公司代子公司公告115年第三季財務報告"))
    assert sub.is_subsidiary and sub.tier == own.tier - 1


def test_subsidiary_tier_never_goes_below_low():
    c = assess(notice("公告本公司代子公司公告研發主管異動"))
    assert c.is_subsidiary and c.tier is Tier.LOW


def test_material_subsidiary_event_is_not_downgraded():
    c = assess(notice("公告本公司代子公司簽訂重大契約"))
    assert c.is_subsidiary and c.event_type is EventType.CONTRACT
    assert c.tier is Tier.HIGH


def test_higher_tier_beats_a_newer_lower_tier():
    old_contract = notice(CONTRACT, hour=9)
    new_personnel = notice(PERSONNEL, hour=15)
    best = pick_representatives([new_personnel, old_contract])
    assert best["2330"].event_type is EventType.CONTRACT


def test_same_tier_uses_type_priority_before_time():
    report_early = notice(REPORT, hour=8)
    briefing_late = notice("公告本公司受邀參加法人說明會", hour=16)
    best = pick_representatives([briefing_late, report_early])
    assert best["2330"].event_type is EventType.FINANCIAL_REPORT


def test_equal_score_picks_newer_first_publication():
    early = notice("公告本公司董事會決議配發股利", hour=8)
    late = notice("公告本公司董事會決議配發現金股利", hour=14)
    best = pick_representatives([early, late])
    assert best["2330"].notice is late


def test_final_tie_is_stable_by_article_id_regardless_of_input_order():
    a = notice(REPORT, hour=8)
    b = notice("公告本公司115年度第三季財務報告", hour=8)
    one = pick_representatives([a, b])["2330"].notice.article.article_id
    two = pick_representatives([b, a])["2330"].notice.article.article_id
    assert one == two


def test_republished_old_notice_does_not_win_by_its_last_date():
    rename = [mk("公告本公司更名", day=d, desc="same") for d in (3, 4, 5, 10)]
    fresh = notice("公告本公司董事會決議配發股利", day=10)
    cands = [*collapse_repeats(rename), fresh]
    assert cands[0].last_published_at.day == 10
    # the fresh MEDIUM notice beats the old LOW rename, whatever the last date
    assert pick_representatives(cands)["2330"].notice is fresh


def test_since_drops_notices_first_announced_before_the_window():
    rename = collapse_repeats(
        [mk("公告本公司更名", day=d, desc="same") for d in (3, 10)]
    )
    since = dt.datetime(2026, 11, 9, tzinfo=TAIPEI_TZ)
    assert pick_representatives(rename, since=since) == {}
    assert pick_representatives(rename) != {}


def test_since_must_be_timezone_aware():
    with pytest.raises(ValueError):
        pick_representatives([], since=dt.datetime(2026, 11, 9))


def test_one_per_stock_and_stocks_are_independent():
    best = pick_representatives(
        [notice(REPORT, stock="2330"), notice(CONTRACT, stock="2317")]
    )
    assert set(best) == {"2330", "2317"}


def test_no_candidate_returns_empty():
    assert pick_representatives([notice(ATTENTION)]) == {}
    assert pick_representatives([]) == {}


def test_early_taipei_morning_notice_is_kept_by_taipei_since():
    early = notice(REPORT, day=9, hour=0)
    since = dt.datetime(2026, 11, 9, 0, 0, tzinfo=TAIPEI_TZ)
    assert "2330" in pick_representatives([early], since=since)


def test_asset_transaction_is_medium_not_high():
    c = assess(notice("公告本公司處分有價證券"))
    assert c.event_type is EventType.ASSET_TRANSACTION and c.tier is Tier.MEDIUM


def test_report_and_executive_change_same_stock_prefers_report_even_if_older():
    report = notice(REPORT, hour=8)
    change = notice("公告本公司總經理異動", hour=16)
    best = pick_representatives([change, report])["2330"]
    assert best.event_type is EventType.FINANCIAL_REPORT


def test_high_tier_asset_wording_still_wins_if_contract_present():
    best = pick_representatives(
        [notice("公告本公司處分有價證券", hour=16), notice(CONTRACT, hour=8)]
    )["2330"]
    assert best.event_type is EventType.CONTRACT


def test_trading_status_is_visible_but_attention_is_still_excluded():
    c = assess(notice("公告本公司上櫃有價證券繼續變更交易方法"))
    assert c is not None
    assert c.event_type is EventType.TRADING_RESTRICTION
    assert c.tier is Tier.MEDIUM
    assert assess(notice(ATTENTION)) is None


def test_bank_credit_is_not_automatically_high():
    c = assess(notice("本公司董事會決議通過新臺幣78億元銀行聯合授信案"))
    assert c is not None
    assert c.event_type is EventType.CREDIT_FACILITY
    assert c.tier is Tier.MEDIUM


def test_subsidiary_clinical_hold_is_not_downgraded_to_low():
    c = assess(notice("代子公司FBD Biologics公告FDA通知新藥臨床試驗實施臨床暫停"))
    assert c is not None
    assert c.is_subsidiary
    assert c.event_type is EventType.DRUG_DEVELOPMENT
    assert c.tier is Tier.MEDIUM


def test_all_event_types_have_a_base_tier():
    from app.news.select import BASE_TIER, EXCLUDED

    assert set(BASE_TIER) | set(EXCLUDED) == set(EventType)


def test_same_medium_tier_uses_priority_before_time():
    report = notice(REPORT, hour=8)
    briefing = notice("公告本公司自行召開法人說明會", hour=16)
    assert assess(report).tier is assess(briefing).tier is Tier.MEDIUM
    assert pick_representatives([briefing, report])["2330"].notice is report


def test_trading_restriction_has_priority_over_financial_report():
    restriction = notice("公告本公司股票繼續變更交易方法", hour=8)
    report = notice(REPORT, hour=16)
    assert pick_representatives([report, restriction])["2330"].notice is restriction


def test_policy_mappings_are_disjoint_and_have_no_excluded_priorities():
    from app.news.select import (
        BASE_TIER,
        EXCLUDED,
        SUBSIDIARY_NOT_DOWNGRADED,
        TYPE_PRIORITY,
    )

    assert not (set(BASE_TIER) & EXCLUDED)
    assert set(TYPE_PRIORITY) <= set(BASE_TIER)
    assert SUBSIDIARY_NOT_DOWNGRADED <= set(BASE_TIER)


def test_since_rejects_tzinfo_without_utc_offset():
    class NoOffset(dt.tzinfo):
        def utcoffset(self, value):
            return None

    with pytest.raises(ValueError, match="timezone-aware"):
        pick_representatives([], since=dt.datetime(2026, 11, 9, tzinfo=NoOffset()))
