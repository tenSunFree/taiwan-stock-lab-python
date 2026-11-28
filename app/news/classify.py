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

import html
import re
import unicodedata
from enum import Enum


class EventType(str, Enum):
    FINANCIAL_REPORT = "財報"
    MARKET_ATTENTION = "股價異常公告"
    TRADING_RESTRICTION = "交易方式或停止買賣"
    INCIDENT_OR_CLARIFICATION = "重大事件與澄清"
    LOAN_GUARANTEE = "資金貸與背書保證"
    EMPLOYEE_EQUITY = "員工股權"
    CAPITAL_AND_DEBT = "增資減資發債"
    CREDIT_FACILITY = "銀行授信"
    LEGAL_DISPUTE = "訴訟與法律"
    DRUG_DEVELOPMENT = "新藥研發進展"
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


def _normalize_title(title: str) -> str:
    """NFKC after decoding HTML entities seen in source announcements.

    Example: &#12070; is the CJK radical ⼦ and normalizes to 子.
    Avoid using this title normalization as an event identity / dedup key.
    """
    if not isinstance(title, str):
        raise TypeError("title must be str")
    return unicodedata.normalize("NFKC", html.unescape(title)).strip()


# Each rule has one or more requirements; ALL patterns must match.
# Deliberately keep the first-match order visible and reviewable.
_RULES: tuple[tuple[EventType, tuple[re.Pattern[str], ...]], ...] = (
    # Avoid classifying a security-incident disclosure as generic finance.
    (
        EventType.INCIDENT_OR_CLARIFICATION,
        (
            _p(
                r"澄清|資安事件|資通安全事件|資訊安全事件|資料外洩|個資外洩|火災|爆炸|重大事故"
                r"|駭客|網路攻擊|勒索軟體|冒用名義"
            ),
        ),
    ),
    # '集中交易' and '櫃檯買賣' alone are NOT evidence of abnormal trading.
    # Abnormal price/volume announcements remain excluded from recent focus.
    (
        EventType.MARKET_ATTENTION,
        (_p(r"注意交易資訊|注意股票|異常交易|交易異常|股價異常|成交量異常"),),
    ),
    # A supplement or correction refers to an EARLIER announcement, so it is
    # not a new event of the underlying type (e.g. a correction of last
    # year's asset notice).  Checked before the type rules on purpose.
    (
        EventType.SUPPLEMENT,
        (
            _p(
                r"(?:^|[(（])\s*(?:公告)?(?:本公司)?\s*(?:更正|補充(?!承諾|資本|營運資金))"
                r"|代(?:本公司(?:之)?)?(?:重要)?(?:公告)?(?:子|孫)公司"
                r"(?:\s*|[^，。]{0,60}?公告\s*)(?:更正|補充(?!承諾|資本|營運資金))"
                r"|補充公告|補充說明|補充資訊|更正公告|更正資訊"
            ),
        ),
    ),
    # A trading-method change or exchange suspension is a regulatory STATUS,
    # not abnormal price/volume movement.  Keep it a candidate until is_managed
    # and similar risk inputs have a reliable official data source.
    (
        EventType.TRADING_RESTRICTION,
        (
            _p(
                r"變更交易方法|分盤集合競價|預收款券|全額交割"
                r"|(?:證交所|櫃買中心).{0,20}(?:停止買賣|暫停買賣)"
                r"|(?:股票|有價證券).{0,12}(?:停止買賣|暫停買賣)"
            ),
        ),
    ),
    # '財務報$': the source sometimes truncates the title mid-word.
    # '財務報導' (e.g. 國際財務報導準則) is deliberately NOT matched.
    (
        EventType.FINANCIAL_REPORT,
        (_p(r"財務報告|財務報表|財報|財務報$|季.{0,10}營運報告"),),
    ),
    (EventType.FINANCIAL_WARNING, (_p(r"累積虧損"),)),
    # Specific title evidence, not free-form words such as 法院/臨床.
    # Default materiality is MEDIUM; amounts, outcome and issuer-level
    # relevance require content parsing and should not be inferred here.
    (
        EventType.LEGAL_DISPUTE,
        (
            _p(
                r"(?:專利|民事|刑事|商業|行政).{0,10}(?:訴訟|起訴|判決)"
                r"|(?:民事|刑事)起訴狀|(?:最高法院|地方法院|高等法院).{0,28}(?:判決|裁定|起訴狀)"
                r"|訴訟.{0,24}(?:和解|判決|起訴)|和解協議"
            ),
        ),
    ),
    (
        EventType.DRUG_DEVELOPMENT,
        (
            _p(
                r"新藥.{0,100}(?:臨床|試驗|核准|適應症|藥證|申請)"
                r"|(?:FDA|衛生部).{0,70}(?:臨床暫停|新藥(?:申請|核准))"
            ),
        ),
    ),
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
                r"|證券簡稱.{0,12}變更"
            ),
            # "薪資報酬委員會更名為…" renames a committee, not the company.
            _p(r"^(?!.*委員會)"),
        ),
    ),
    (EventType.LOAN_GUARANTEE, (_p(r"資金貸與|背書保證"),)),
    (EventType.EMPLOYEE_EQUITY, (_p(r"限制員工權利|員工認股權|員工認購權"),)),
    (EventType.TREASURY_STOCK, (_p(r"庫藏股|買回.{0,6}股份"),)),
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
        (
            _p(
                r"增資|減資|公司債|可轉債|轉換公司債|無擔保轉|有擔保轉|私募"
                r"|發行新股"
            ),
        ),
    ),
    # An asset disposal is distinct from obtaining a credit facility.
    (
        EventType.ASSET_TRANSACTION,
        (_p(r"授信\s*資產.{0,12}(?:轉讓|處分)|(?:轉讓|處分).{0,12}授信\s*資產"),),
    ),
    # Credit facilities are not the same as bond issuance / capital raises.
    # A credit line's amount alone is insufficient to label it HIGH.
    (
        EventType.CREDIT_FACILITY,
        (_p(r"聯合授信(?!\s*資產)|(?:銀行|金融機構).{0,12}(?:授信額度|授信案)"),),
    ),
    (EventType.NON_COMPETE, (_p(r"競業禁止|解除經理人競業|競業限制"),)),
    # '委員會委員異動' should be PERSONNEL_OTHER, not setting up a committee.
    (
        EventType.COMMITTEE,
        (_p(r"委員會"), _p(r"設置|設立|成立|更名|改名|改制|轉型|調整為")),
    ),
    (
        EventType.EXECUTIVE_CHANGE,
        (
            _p(
                r"(?<!副)董事長|(?<!副)總經理|(?<!副)執行長"
                r"|財務長|營運長|技術長|研發長|資訊長|永續長|資訊安全長"
            ),
            _p(
                r"異動|委任|任命|解任|辭任|辭職|辭世|新任|選任|推選|卸任|退休|追認|改派|設置"
            ),
        ),
    ),
    # After EXECUTIVE_CHANGE on purpose: '總經理暨…委員會委員異動' is an
    # executive change.  Members of a committee (lists, appointments, by-elections): a personnel
    # item, not a new committee.  '委員(?!會)' keeps 聯邦委員會 etc. out.
    # '審計委員會委任會計師' matches neither this nor COMMITTEE: it stays 其他.
    (
        EventType.PERSONNEL_OTHER,
        (_p(r"委員會.{0,10}(?:委員(?!會)|成員|人員異動|異動)" r"|(?:委員|成員)名單"),),
    ),
    # Avoid '委任' alone: contracts and service providers may be appointed.
    (
        EventType.PERSONNEL_OTHER,
        (
            _p(
                r"(?:董事(?!會)|監察人|獨立董事|委員(?!會)|副總經理|"
                r"財務主管|會計主管|稽核主管|內部稽核主管|公司治理主管|總稽核|"
                r"發言人|經理人|總經理|董事長|執行長|董監事|法人董事|"
                r"[^，。]{0,6}主管|協理|副理|總監|處長|代表人|成員)"
            ),
            _p(
                r"異動|委任|任命|解任|辭任|辭職|辭世|新任|選任|卸任|退休|追認|改派"
                r"|變動|續派|指派|名單|推選"
            ),
        ),
    ),
    (
        EventType.INVITED_INVESTOR_EVENT,
        (
            _p(
                r"受邀參加.{0,110}(?:投資人(?:說明會|會議|論壇|活動)"
                r"|投資.{0,6}論壇|法人說明會|法說會|corporate day)"
                r"|受.{1,20}邀請(?:參加|參與).{0,110}(?:投資人(?:說明會|會議|論壇|活動)"
                r"|法人說明會|法說會|corporate day)"
                r"|(?:參加|參與).{0,100}(?:法人說明會|法說會|投資人說明會|投資.{0,6}論壇)"
                r"|(?:參加|參與).{0,60}(?:corporate day|投資人會議)"
            ),
        ),
    ),
    (EventType.BRIEFING, (_p(r"法人說明會|法說會|記者會"),)),
    (EventType.SHAREHOLDER_MEETING, (_p(r"股東常會|股東臨時會|股東會"),)),
    # A construction contract (承攬契約) is a contract even when the title also
    # says 取得 or 工程; the 說明 of these notices carries 契約種類:工程承攬契約.
    (EventType.CONTRACT, (_p(r"承攬(?:契約|合約)"),)),
    # Assets before generic contracts, e.g. acquiring property via contract.
    (
        EventType.ASSET_TRANSACTION,
        (
            _p(
                r"(?:取得|處分).{0,80}(?:資產|不動產|有價證券|設備|股權"
                r"|土地|工程|廠房|房屋|股份(?!有限公司))|"
                r"投資.{0,30}有價證券|"
                r"(?:不動產|使用權資產|有價證券|機器設備).{0,40}(?:取得|處分|交易)|"
                r"(?:租賃|租約).{0,30}(?:資產|不動產|處分|終止)|"
                r"(?:取得|處分|購買|購入|申購|贖回).{0,40}(?:理財商品|理財產品)|"
                r"(?:理財商品|理財產品).{0,20}(?:取得|處分|贖回)|"
                r"(?:購買|購入|購置|訂購|承購).{0,40}"
                r"(?:貨櫃|設備|土地|用地|不動產|房屋|廠房|建物|貨輪|船|股份(?!有限公司)|股權|資產)"
            ),
        ),
    ),
    (EventType.CONTRACT, (_p(r"契約|承攬|合建案|重大合約|供貨協議|工程合約|得標"),)),
    (
        EventType.BOARD_RESOLUTION_SUMMARY,
        (_p(r"重要決議|主要議案|董事會決議事項|重大決議事項|各項議案"),),
    ),
)

# May be preceded by the parent's name (玉山金控代子公司…) and may include
# the optional '本公司之' or '重要'.  This flags who published it; it is not
# a universal materiality penalty, because subsidiary events can be major.
_SUBSIDIARY_NOTICE = _p(r"代(?:本公司(?:之)?)?(?:重要)?(?:公告)?(?:子|孫)公司")


def classify_title(title: str) -> EventType:
    """Return a coarse event type, or OTHER if the title is ambiguous.

    Both content and official categorisation codes are deliberately out of
    scope.  Results must NOT be used as a deduplication key on their own.
    """
    normalized = _normalize_title(title)
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
    return _SUBSIDIARY_NOTICE.search(_normalize_title(title)) is not None
