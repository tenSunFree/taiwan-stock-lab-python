import csv
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.news_models import ensure_news_tables
from app.db.news_repository import NewsRepository
from app.jobs import news_validate
from app.news.models import TAIPEI_TZ, ContentLevel, RawArticle


def art(
    title,
    *,
    stock="2330",
    day=10,
    hour=8,
    minute=0,
    desc="d",
    market="TWSE",
    official=True,
):
    when = dt.datetime(2026, 11, day, hour, minute, tzinfo=TAIPEI_TZ)
    return RawArticle(
        article_id=f"{market}-{stock}-{title}-{day}-{hour}-{minute}",
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


DATA = [
    art("公告本公司115年第三季財務報告"),
    art("公告本公司處分有價證券", stock="2317"),
    art("公告本公司某某事項", stock="2454"),
    art("公告本公司股票列為注意交易資訊", stock="1101"),
    art("公告本公司更名", stock="3008", day=9, desc="same"),
    art("公告本公司更名", stock="3008", day=10, desc="same"),
    art("媒體報導", stock="2330", official=False, market="MEDIA"),
]


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    ensure_news_tables(engine)
    with Session(engine) as s:
        NewsRepository(s).save_new(DATA)
        yield s


def test_analyze_counts_collapse_and_excludes_media():
    r = news_validate.analyze(DATA)
    assert len(r.notices_rows) == 5  # 6 official rows, 2 renames merged
    assert not any(x["title"] == "媒體報導" for x in r.notices_rows)
    assert "1 repeats merged" in r.report


def test_attention_notice_is_listed_but_excluded():
    r = news_validate.analyze(DATA)
    row = next(x for x in r.notices_rows if x["stock_id"] == "1101")
    assert row["tier"] == "EXCLUDED" and row["selected"] is False


def test_selected_flag_matches_pick_one_per_stock():
    r = news_validate.analyze(DATA)
    assert sum(x["selected"] for x in r.notices_rows) == 4


def test_other_share_excludes_attention_notices():
    r = news_validate.analyze(DATA)
    assert "(excluding 股價異常公告): 1/4 = 25.0%" in r.report


def test_samples_have_blank_review_columns_and_are_reproducible():
    one = news_validate.analyze(DATA, sample=1).sample_rows
    two = news_validate.analyze(DATA, sample=1).sample_rows
    assert one == two
    groups = {g for x in one for g in x["sample_groups"].split("|")}
    assert groups >= {"資產交易", "其他"}
    assert all(x["expected_type"] == "" and x["review_note"] == "" for x in one)


def test_picked_stocks_group_only_contains_selected_notices():
    r = news_validate.analyze(DATA, stocks=frozenset({"2330"}))
    group = [
        x for x in r.sample_rows if "picked_stocks" in x["sample_groups"].split("|")
    ]
    assert [x["stock_id"] for x in group] == ["2330"]
    assert all(x["selected"] for x in group)


def test_sample_cap_applies_per_group():
    many = [art(f"公告本公司處分有價證券{i}", stock=f"{2000 + i}") for i in range(10)]
    r = news_validate.analyze(many, sample=3)
    assert sum("資產交易" in x["sample_groups"].split("|") for x in r.sample_rows) == 3


def test_run_writes_files_from_database_using_taipei_day_bounds(session, tmp_path):
    early = art("公告本公司115年第三季財務報告", stock="9999", day=14, hour=0, minute=5)
    NewsRepository(session).save_new([early])
    news_validate.run(
        session,
        start=dt.date(2026, 11, 14),
        end=dt.date(2026, 11, 14),
        out_dir=tmp_path,
        sample=5,
        stocks=frozenset(),
    )
    with (tmp_path / "notices.csv").open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["stock_id"] for r in rows] == ["9999"]  # 00:05 Taipei is kept
    assert (tmp_path / "report.md").read_text(encoding="utf-8").startswith("# News")
    assert (tmp_path / "samples.csv").exists()


def test_run_empty_range_still_writes_report(session, tmp_path):
    news_validate.run(
        session,
        start=dt.date(2020, 1, 1),
        end=dt.date(2020, 1, 2),
        out_dir=tmp_path,
        sample=5,
        stocks=frozenset(),
    )
    assert "official rows: 0" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_run_does_not_modify_the_table(session, tmp_path):
    from sqlalchemy import func, select

    from app.db.news_models import RawArticleRow

    before = session.scalar(select(func.count()).select_from(RawArticleRow))
    news_validate.run(
        session,
        start=dt.date(2026, 11, 1),
        end=dt.date(2026, 11, 30),
        out_dir=tmp_path,
        sample=5,
        stocks=frozenset(),
    )
    assert session.scalar(select(func.count()).select_from(RawArticleRow)) == before


def test_end_before_start_is_rejected(session, tmp_path):
    with pytest.raises(ValueError):
        news_validate.run(
            session,
            start=dt.date(2026, 11, 2),
            end=dt.date(2026, 11, 1),
            out_dir=tmp_path,
            sample=5,
            stocks=frozenset(),
        )


def test_main_requires_database_url(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert (
        news_validate.main(
            ["--start", "2026-11-01", "--end", "2026-11-02", "--out", str(tmp_path)]
        )
        == 1
    )


def test_notice_rows_carry_id_description_and_fetch_time():
    a = art("公告本公司處分有價證券", desc="1.交易標的:甲\n2.金額:100", stock="2317")
    r = news_validate.analyze([a])
    row = r.notices_rows[0]
    assert row["article_id"] == a.article_id
    assert row["description"] == "1.交易標的:甲\n2.金額:100"
    assert row["first_fetched_taipei"] == "2026-11-10 08:00"


def test_picked_other_is_sampled_without_stocks_and_ignores_the_cap():
    many = [art(f"公告本公司某某事項{i}", stock=f"{2000 + i}") for i in range(6)]
    r = news_validate.analyze(many, sample=1)
    picked = [
        x for x in r.sample_rows if "picked_other" in x["sample_groups"].split("|")
    ]
    assert len(picked) == 6


def test_a_notice_in_several_groups_is_listed_once():
    many = [art(f"公告本公司某某事項{i}", stock=f"{2000 + i}") for i in range(3)]
    r = news_validate.analyze(many, sample=3, stocks=frozenset({"2000"}))
    ids = [x["article_id"] for x in r.sample_rows]
    assert len(ids) == len(set(ids)) == 3
    both = next(x for x in r.sample_rows if x["stock_id"] == "2000")
    assert set(both["sample_groups"].split("|")) == {
        "其他",
        "picked_other",
        "picked_stocks",
    }


def test_same_title_different_description_stays_two_notices():
    a = art("公告本公司處分有價證券", desc="交易標的:甲", day=9)
    b = art("公告本公司處分有價證券", desc="交易標的:乙", day=10)
    assert len(news_validate.analyze([a, b]).notices_rows) == 2


def test_taipei_late_evening_is_inside_its_own_day(session, tmp_path):
    late = art(
        "公告本公司115年第三季財務報告", stock="8888", day=15, hour=23, minute=59
    )
    NewsRepository(session).save_new([late])
    news_validate.run(
        session,
        start=dt.date(2026, 11, 15),
        end=dt.date(2026, 11, 15),
        out_dir=tmp_path,
        sample=5,
        stocks=frozenset(),
    )
    with (tmp_path / "notices.csv").open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["stock_id"] for r in rows] == ["8888"]
    news_validate.run(
        session,
        start=dt.date(2026, 11, 16),
        end=dt.date(2026, 11, 16),
        out_dir=tmp_path,
        sample=5,
        stocks=frozenset(),
    )
    with (tmp_path / "notices.csv").open(encoding="utf-8-sig") as fh:
        assert list(csv.DictReader(fh)) == []


def test_unknown_stock_in_stocks_adds_nothing():
    r = news_validate.analyze(DATA, stocks=frozenset({"0000"}))
    assert not any("picked_stocks" in x["sample_groups"] for x in r.sample_rows)


def test_result_does_not_depend_on_input_order():
    one = news_validate.analyze(DATA, stocks=frozenset({"2330"}))
    two = news_validate.analyze(list(reversed(DATA)), stocks=frozenset({"2330"}))
    assert one.notices_rows == two.notices_rows
    assert one.sample_rows == two.sample_rows


def test_csv_cells_cannot_start_a_spreadsheet_formula(session, tmp_path):
    NewsRepository(session).save_new(
        [art('=HYPERLINK("http://x")', stock="7777", day=12, desc="+1+1")]
    )
    news_validate.run(
        session,
        start=dt.date(2026, 11, 12),
        end=dt.date(2026, 11, 12),
        out_dir=tmp_path,
        sample=5,
        stocks=frozenset(),
    )
    with (tmp_path / "notices.csv").open(encoding="utf-8-sig") as fh:
        row = next(csv.DictReader(fh))
    assert row["title"].startswith("'=") and row["description"] == "'+1+1"


def test_csv_round_trips_newlines_commas_and_quotes(session, tmp_path):
    desc = '1.甲, "乙"\n2.丙'
    NewsRepository(session).save_new(
        [art("公告本公司處分有價證券", stock="6666", day=12, desc=desc)]
    )
    news_validate.run(
        session,
        start=dt.date(2026, 11, 12),
        end=dt.date(2026, 11, 12),
        out_dir=tmp_path,
        sample=5,
        stocks=frozenset(),
    )
    with (tmp_path / "notices.csv").open(encoding="utf-8-sig", newline="") as fh:
        assert next(csv.DictReader(fh))["description"] == desc


def test_report_states_range_scope_and_denominators(session, tmp_path):
    r = news_validate.run(
        session,
        start=dt.date(2026, 11, 1),
        end=dt.date(2026, 11, 30),
        out_dir=tmp_path,
        sample=5,
        stocks=frozenset(),
    )
    assert "2026-11-01 to 2026-11-30 (Taipei, inclusive)" in r.report
    assert "NOT what the daily report would show" in r.report
    assert "eligible for selection" in r.report
    assert "NOT a count of missed events" in r.report
