"""Conservative, deterministic categorisation of TWSE/TPEx announcement titles.

This module is **title-only** and has no I/O or ranking side effects.
Rules are tried in order; the first match wins.  Prefer returning OTHER
rather than inferring an event from generic words such as 合併, 設立,
孫公司, 委任 or 櫃檯買賣 without sufficient context.

The result is a *coarse event type*, not an event identity, materiality
score, or assertion that two announcements describe the same event.
The announcement's 說明 and official 符合條款 (if stored later) may be
needed to disambiguate a title; nothing here silently invents them.
"""

from __future__ import annotations

import re
import unicodedata
from enum import Enum


class EventType(str, Enum):
    FINANCIAL_REPORT = "財報"
    MARKET_ATTENTION = "股價異常公告"
    INCIDENT_OR_CLARIFICATION = "重大事件與澄清"
    LOAN_GUARANTEE = "資金貸與背書保證"
    EMPLOYEE_EQUITY = "員工股權"
    CAPITAL_AND_DEBT = "增資減資發債"
    DIVIDEND = "股利盈餘"
    TREASURY_STOCK = "庫藏股"
    COMMITTEE = "設置委員會"
    EXECUTIVE_CHANGE = "人事異動_高層"
    PERSONNEL_OTHER = "人事異動_其他"
    NON_COMPETE = "經理人競業"
    INVITED_INVESTOR_EVENT = "受邀參加投資人活動"
    BRIEFING = "法說記者會"
    SHAREHOLDER_MEETING = "股東會"
    MONTHLY_FIGURES = "月份數據"
    ORGANISATION_AND_INVESTMENT = "組織與投資"
    CONTRACT = "重大契約"
    ASSET_TRANSACTION = "資產交易"
    SUPPLEMENT = "補充公告"
    BOARD_RESOLUTION_SUMMARY = "董事會重要決議彙總"
    NAME_OR_PAR_VALUE = "公司名稱或面額"
    FINANCIAL_WARNING = "財務警示"
    OTHER = "其他"


def _p(value: str) -> re.Pattern[str]:
    return re.compile(value, re.IGNORECASE)


# Each rule has one or more requirements; ALL patterns must match.
# Deliberately keep the first-match order visible and reviewable.
_RULES: tuple[tuple[EventType, tuple[re.Pattern[str], ...]], ...] = (
    # Avoid classifying a security-incident disclosure as generic finance.
    (
        EventType.INCIDENT_OR_CLARIFICATION,
        (_p(r"澄清|資安事件|資訊安全事件|資料外洩|個資外洩|火災|爆炸|重大事故"),),
    ),
    # '集中交易' and '櫃檯買賣' alone are NOT evidence of abnormal trading.
    (
        EventType.MARKET_ATTENTION,
        (_p(r"注意交易資訊|注意股票|異常交易|交易異常|股價異常|成交量異常"),),
    ),
    (EventType.FINANCIAL_REPORT, (_p(r"財務報告|財務報表|財報"),)),
    (EventType.FINANCIAL_WARNING, (_p(r"累積虧損"),)),
    # Monthly results may include '合併'; that does not mean M&A.
    (
        EventType.MONTHLY_FIGURES,
        (
            _p(
                r"(?:[0-9一二三四五六七八九十]{1,2}月(?:份)?).{0,40}"
                r"(?:營收|營業收入|營業額|自結損益|營運情形|損益)"
                r"|月(?:份)?(?:營收|損益)"
                r"|自結(?:合併)?(?:營收|損益|盈餘)"
                r"|合併營收概算"
            ),
        ),
    ),
    (
        EventType.NAME_OR_PAR_VALUE,
        (
            _p(
                r"更名|公司名稱.{0,12}(?:變更|由)|變更(?:公司)?名稱|股票面額|每股面額|面額由"
            ),
            # "薪資報酬委員會更名為…" renames a committee, not the company.
            _p(r"^(?!.*委員會)"),
        ),
    ),
    (EventType.LOAN_GUARANTEE, (_p(r"資金貸與|背書保證"),)),
    (EventType.EMPLOYEE_EQUITY, (_p(r"限制員工權利|員工認股權|員工認購權"),)),
    (EventType.TREASURY_STOCK, (_p(r"庫藏股"),)),
    (
        EventType.DIVIDEND,
        (
            _p(
                r"股利|盈餘(?:分派|分配|不分派|不分配)|(?:分派|分配).{0,40}盈餘|除息|除權息|現金股息"
            ),
        ),
    ),
    # Participation in another entity's capital raise != issuer raising capital.
    # Likewise investing in / raising capital for one's subsidiary.
    (
        EventType.ORGANISATION_AND_INVESTMENT,
        (
            _p(
                r"參與.{0,80}(?:現金增資|私募)"
                r"|(?:增資|投資|注資).{0,32}(?:子公司|孫公司)"
                r"|(?:對|向).{0,24}(?:子公司|孫公司).{0,32}(?:增資|注資)"
                r"|投資設立|參與設立|合資公司|(?:設立|新設|成立).{0,20}(?:子公司|孫公司|分公司)"
                r"|合併(?:案|基準日|契約|完成)|簡易合併|吸收合併|併購"
                r"|股份轉換|股權投資|股權收購|轉投資|解散|清算|公開收購"
                r"|(?:增加|追加)投資"
            ),
        ),
    ),
    (
        EventType.CAPITAL_AND_DEBT,
        (_p(r"增資|減資|公司債|可轉債|轉換公司債|無擔保轉|有擔保轉|私募"),),
    ),
    (EventType.NON_COMPETE, (_p(r"競業禁止|解除經理人競業|競業限制"),)),
    # '委員會委員異動' should be PERSONNEL_OTHER, not setting up a committee.
    (EventType.COMMITTEE, (_p(r"委員會"), _p(r"設置|設立|成立|更名|改名|委任|選任"))),
    (
        EventType.EXECUTIVE_CHANGE,
        (
            _p(
                r"董事長|總經理|執行長|財務長|營運長|技術長|研發長|資訊長|永續長|資訊安全長"
            ),
            _p(r"異動|委任|任命|解任|辭任|新任|選任|卸任|退休|追認|改派|設置"),
        ),
    ),
    # Avoid '委任' alone: contracts and service providers may be appointed.
    (
        EventType.PERSONNEL_OTHER,
        (
            _p(
                r"(?:董事(?!會)|監察人|獨立董事|委員(?!會)|副總經理|"
                r"財務主管|會計主管|稽核主管|內部稽核主管|公司治理主管|"
                r"發言人|經理人|總經理|董事長|執行長|"
                r"[^，。]{0,6}主管|協理|副理|總監|處長|代表人|成員)"
            ),
            _p(r"異動|委任|任命|解任|辭任|新任|選任|卸任|退休|追認|改派"),
        ),
    ),
    (
        EventType.INVITED_INVESTOR_EVENT,
        (_p(r"受邀參加|(?:參加|參與).{0,100}(?:法人說明會|法說會|投資人說明會)"),),
    ),
    (EventType.BRIEFING, (_p(r"法人說明會|法說會|記者會"),)),
    (EventType.SHAREHOLDER_MEETING, (_p(r"股東常會|股東臨時會|股東會"),)),
    # Assets before generic contracts, e.g. acquiring property via contract.
    (
        EventType.ASSET_TRANSACTION,
        (
            _p(
                r"關係人.{0,50}(?:取得|處分|交易)|"
                r"(?:取得|處分).{0,80}(?:資產|不動產|有價證券|設備|股權|土地|工程|廠房|房屋)|"
                r"投資.{0,30}有價證券|"
                r"(?:不動產|使用權資產|有價證券|機器設備).{0,40}(?:取得|處分|交易)|"
                r"(?:租賃|租約).{0,30}(?:資產|不動產|處分|終止)"
            ),
        ),
    ),
    (EventType.CONTRACT, (_p(r"契約|承攬|合建案|重大合約|供貨協議"),)),
    (
        EventType.BOARD_RESOLUTION_SUMMARY,
        (_p(r"重要決議|主要議案|董事會決議事項|重大決議事項|各項議案"),),
    ),
    # Supplementary status is a modifier: only label it here when the
    # underlying event was not determined from the title.
    (
        EventType.SUPPLEMENT,
        (_p(r"補充公告|補充說明|補充資訊|更正公告|更正資訊|^\W*補充"),),
    ),
)

# May be preceded by the parent's name (玉山金控代子公司…) and may include
# the optional '本公司之' or '重要'.  This flags who published it; it is not
# a universal materiality penalty, because subsidiary events can be major.
_SUBSIDIARY_NOTICE = _p(r"代(?:本公司(?:之)?)?(?:重要)?(?:子|孫)公司")


def classify_title(title: str) -> EventType:
    """Return a coarse event type, or OTHER if the title is ambiguous.

    Both content and official categorisation codes are deliberately out of
    scope.  Results must NOT be used as a deduplication key on their own.
    """
    if not isinstance(title, str):
        raise TypeError("title must be str")
    normalized = unicodedata.normalize("NFKC", title).strip()
    if not normalized:
        return EventType.OTHER
    for event_type, patterns in _RULES:
        if all(pattern.search(normalized) for pattern in patterns):
            return event_type
    return EventType.OTHER


def is_subsidiary_notice(title: str) -> bool:
    """Whether this announcement is *issued on behalf of* a subsidiary.

    Merely mentioning, investing in, or guaranteeing a subsidiary does not
    mean the parent is publishing an announcement on its behalf.
    """
    if not isinstance(title, str):
        raise TypeError("title must be str")
    return _SUBSIDIARY_NOTICE.search(unicodedata.normalize("NFKC", title)) is not None
