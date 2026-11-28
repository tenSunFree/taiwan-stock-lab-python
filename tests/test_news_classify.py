import pytest

from app.news.classify import EventType as T
from app.news.classify import classify_title, is_subsidiary_notice

# Titles are real TWSE/TPEx 主旨 text seen in production data.
CASES = [
    ("公告本公司董事會通過115年第3季合併財務報告", T.FINANCIAL_REPORT),
    ("公告本公司董事會通過115年第三季財務報表案", T.FINANCIAL_REPORT),
    ("公告本公司董事會通過115年度第三季合併財報", T.FINANCIAL_REPORT),
    ("公告本公司115年第三季合併財務報告業經董事會決議通過", T.FINANCIAL_REPORT),
    (
        "係因本公司有價證券於集中交易市場達公布注意交易資訊標準，故公布相關",
        T.MARKET_ATTENTION,
    ),
    (
        "公告本公司線上商店委外服務廠商發生資訊安全事件之處理情形",
        T.INCIDENT_OR_CLARIFICATION,
    ),
    ("澄清115年11月03日工商時報A13之新聞內容(補充說明)", T.INCIDENT_OR_CLARIFICATION),
    ("公告本公司鑄造廠(觀音廠)火災事件說明", T.INCIDENT_OR_CLARIFICATION),
    ("公告本公司新增資金貸與金額達新臺幣一千萬元以上", T.LOAN_GUARANTEE),
    (
        "為子公司背書保證達公開發行公司資金貸與及背書保證處理準則第25條第一項第四款公告",
        T.LOAN_GUARANTEE,
    ),
    ("公告本公司董事會決議辦理限制員工權利新股收回註銷減資事宜", T.EMPLOYEE_EQUITY),
    ("公告本公司董事會決議通過員工認股權憑證轉換普通股之增資基準日", T.EMPLOYEE_EQUITY),
    ("董事會決議擬發行中華民國境內第四次有擔保轉換公司債", T.CAPITAL_AND_DEBT),
    ("公告本公司減資換股作業計畫", T.CAPITAL_AND_DEBT),
    ("公告本公司董事會決議辦理現金增資發行新股", T.CAPITAL_AND_DEBT),
    ("公告本公司董事會決議通過私募普通股補辦公開發行暨申請上櫃案", T.CAPITAL_AND_DEBT),
    ("董事會通過分派115年第3季盈餘", T.DIVIDEND),
    ("公告本公司董事會決議除息基準日", T.DIVIDEND),
    ("公告本公司董事會決議115年上半年度盈餘不分派.", T.DIVIDEND),
    ("公告本公司董事會決議買回庫藏股", T.TREASURY_STOCK),
    ("公告本公司董事會決議設置永續發展委員會", T.COMMITTEE),
    ("公告董事會決議設置提名委員會及委任成員", T.COMMITTEE),
    ("公告本公司總經理異動", T.EXECUTIVE_CHANGE),
    ("公告本公司董事會通過總經理任命案", T.EXECUTIVE_CHANGE),
    ("代子公司星德電力公告董事長異動", T.EXECUTIVE_CHANGE),
    ("公告本公司內部稽核主管異動", T.PERSONNEL_OTHER),
    ("公告本公司董事會通過內部稽核主管追認案", T.PERSONNEL_OTHER),
    ("公告本公司公司治理主管異動", T.PERSONNEL_OTHER),
    ("公告本公司董事會通過解除經理人競業禁止之限制", T.NON_COMPETE),
    ("本公司受邀參加群益金鼎證券舉辦之線上法人說明會", T.INVITED_INVESTOR_EVENT),
    ("本公司將於115年11月19日參加永豐金證券舉辦之法人說明會", T.INVITED_INVESTOR_EVENT),
    ("公告本公司將於115年11月12日召開2026年第三季法人說明會", T.BRIEFING),
    ("公告本公司召開重大訊息記者會新聞稿內容", T.BRIEFING),
    ("公告本公司董事會決議召開民國115年第一次股東臨時會", T.SHAREHOLDER_MEETING),
    ("公告本公司115年10月份自結合併營收", T.MONTHLY_FIGURES),  # not a merger
    ("公告本公司115年10月份合併自結損益", T.MONTHLY_FIGURES),
    (
        "公告本公司與大陽科技股份有限公司股份轉換案之股份轉換基準日",
        T.ORGANISATION_AND_INVESTMENT,
    ),
    ("公告本公司董事會決議於馬來西亞投資設立子公司", T.ORGANISATION_AND_INVESTMENT),
    (
        "公告本公司董事會通過解散清算Hannspree China Holdings L",
        T.ORGANISATION_AND_INVESTMENT,
    ),
    ("修正本公司台中市北屯區美和段569地號等1筆新建工程承攬契約總金額", T.CONTRACT),
    ("公告本公司向關係人勤眾興業股份有限公司取得不動產使用權資產", T.ASSET_TRANSACTION),
    ("公告本公司處分有價證券", T.ASSET_TRANSACTION),
    ("公告本公司取得營業用設備達新台幣5億元以上", T.ASSET_TRANSACTION),
    ("(補充公告)公告本公司董事會決議興建觀音廠相關事宜公告", T.SUPPLEMENT),
    ("公告本公司董事會重要決議事項", T.BOARD_RESOLUTION_SUMMARY),
    ("本公司於115年11月5日召開董事會通過之主要議案", T.BOARD_RESOLUTION_SUMMARY),
    (
        "公告本公司名稱由「森寶開發股份有限公司」更名為「長耀地產投資股份有限公司」",
        T.NAME_OR_PAR_VALUE,
    ),
    (
        "公告本公司股票面額由「新台幣10元」變更為「新台幣0.5元」，公告期間：115年",
        T.NAME_OR_PAR_VALUE,
    ),
    ("公告本公司董事會通過變更營業地址", T.OTHER),
    ("", T.OTHER),
]


@pytest.mark.parametrize("title,expected", CASES)
def test_classify_real_titles(title, expected):
    assert classify_title(title) is expected


def test_enum_values_are_the_labels_used_in_reports():
    assert T.FINANCIAL_REPORT.value == "財報"
    assert T.OTHER.value == "其他"


@pytest.mark.parametrize(
    "title",
    [
        "代子公司星德電力公告董事長異動",
        "代重要子公司大倉實業股份有限公司公告董事會決議115年第3季現金股利分派事宜",
        "玉山金控代子公司三商美邦保險代理人股份有限公司公告董事長異動",
        "代本公司之子公司Dynamic Skyline Ltd.公告，新增資金貸與金額達",
        "代孫公司昆山元崧電子科技有限公司公告，新增資金貸與金額達",
    ],
)
def test_subsidiary_notice_detected_anywhere_in_title(title):
    assert is_subsidiary_notice(title)


@pytest.mark.parametrize(
    "title",
    [
        "為子公司背書保證達公開發行公司資金貸與及背書保證處理準則第25條第一項第四款公告",
        "本公司董事會決議增資子公司Sunon Electronics Philippines",
        "公告本公司總經理異動",
    ],
)
def test_own_filings_about_a_subsidiary_are_not_subsidiary_notices(title):
    assert not is_subsidiary_notice(title)


# Additional regression tests: superficially similar wording must not be
# interpreted as the same kind of corporate event.
@pytest.mark.parametrize(
    "title,expected",
    [
        ("公告本公司董事會通過115年度第三季合併財報", T.FINANCIAL_REPORT),
        ("公告本公司115年10月營業收入", T.MONTHLY_FIGURES),
        ("公告本公司115年10月份合併營運情形", T.MONTHLY_FIGURES),
        ("本公司115年10月份自結合併損益", T.MONTHLY_FIGURES),
        ("本公司有價證券於集中交易市場達公布注意交易資訊標準", T.MARKET_ATTENTION),
        ("公司於櫃檯買賣中心掛牌交易", T.OTHER),
        ("本公司於集中交易市場處分有價證券", T.ASSET_TRANSACTION),
        ("公告本公司審計委員會委員異動", T.PERSONNEL_OTHER),
        ("公告本公司獨立董事辭任功能性委員會之委員", T.PERSONNEL_OTHER),
        ("公告本公司董事會決議設置提名委員會及委任成員", T.COMMITTEE),
        ("公告本公司委任第三方協助處理契約", T.CONTRACT),
        ("公告本公司董事會決議除息基準日", T.DIVIDEND),
        (
            "公告本公司董事會通過擬參與永豐金融控股股份有限公司現金增資案",
            T.ORGANISATION_AND_INVESTMENT,
        ),
        (
            "本公司董事會決議增資子公司Sunon Electronics Philippines",
            T.ORGANISATION_AND_INVESTMENT,
        ),
        ("本公司擬對子公司辦理現金增資", T.ORGANISATION_AND_INVESTMENT),
        ("本公司董事會通過第三季合併營運報告事項", T.FINANCIAL_REPORT),
        ("代子公司X公告董事會決議辦理現金增資發行新股", T.CAPITAL_AND_DEBT),
        ("本公司115年10月份合併營收", T.MONTHLY_FIGURES),
        ("公告本公司股份合併案之合併基準日", T.ORGANISATION_AND_INVESTMENT),
        ("公告代孫公司X董事異動", T.PERSONNEL_OTHER),
        ("本公司與合作夥伴簽訂工程承攬契約", T.CONTRACT),
        ("公告本公司董事會通過變更營業地址", T.OTHER),
        ("代孫公司X公告員工認股權憑證轉換普通股之增資基準日", T.EMPLOYEE_EQUITY),
        ("公告本公司補充說明資金貸與資訊", T.SUPPLEMENT),
        ("(補充公告)公告本公司董事會決議興建觀音廠相關事宜公告", T.SUPPLEMENT),
        (
            "公告本公司股票面額由「新台幣10元」變更為「新台幣0.5元」",
            T.NAME_OR_PAR_VALUE,
        ),
        ("本公司召開重大訊息記者會新聞稿內容", T.BRIEFING),
        ("本公司將於11月19日參加永豐金證券舉辦之法人說明會", T.INVITED_INVESTOR_EVENT),
        ("本公司將於11月19日自行召開法人說明會", T.BRIEFING),
        ("公告本公司解散清算海外子公司", T.ORGANISATION_AND_INVESTMENT),
        ("公告本公司董事會主要議案", T.BOARD_RESOLUTION_SUMMARY),
        ("公告本公司重大資安事件", T.INCIDENT_OR_CLARIFICATION),
    ],
)
def test_classification_regressions(title, expected):
    assert classify_title(title) is expected


@pytest.mark.parametrize(
    "title",
    [
        "代本公司之重要子公司X公告董事長異動",
        "玉山金控代重要孫公司X公告總經理異動",
        "代本公司子公司X公告現金增資",
    ],
)
def test_subsidiary_notice_additional_forms(title):
    assert is_subsidiary_notice(title)


@pytest.mark.parametrize(
    "title",
    [
        "本公司對子公司提供背書保證",
        "本公司董事會決議增資子公司",
        "本公司取得孫公司股權",
    ],
)
def test_mentions_of_subsidiary_are_not_on_behalf(title):
    assert not is_subsidiary_notice(title)


def test_unicode_normalization_and_blank_title():
    assert classify_title("  本公司１１５年１０月份自結合併營收  ") is T.MONTHLY_FIGURES
    assert classify_title("   ") is T.OTHER


def test_non_string_is_rejected_explicitly():
    with pytest.raises(TypeError):
        classify_title(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        is_subsidiary_notice(None)  # type: ignore[arg-type]


# Real titles that the first reviewed rule set got wrong on production data
# (committee renames, bonus personnel nouns, assets, dividends, supplements).
@pytest.mark.parametrize(
    "title,expected",
    [
        (
            "公告本公司董事會通過「薪資報酬委員會」更名為「薪資報酬暨提名委員會」",
            T.COMMITTEE,
        ),
        ("公告本公司提名委員會成員異動", T.PERSONNEL_OTHER),
        ("公告本公司研發主管異動", T.PERSONNEL_OTHER),
        ("公告本公司生活科技部協理新任事宜", T.PERSONNEL_OTHER),
        ("本公司法人董事改派代表人", T.PERSONNEL_OTHER),
        ("公告本公司董事會決議以自有資金投資國外有價證券案", T.ASSET_TRANSACTION),
        (
            "代子公司日月光半導體製造(股)公司公告向森川營造工程有限公司取得供營業用之廠務工程",
            T.ASSET_TRANSACTION,
        ),
        ("本公司董事會決議115年上半年度不分配盈餘", T.DIVIDEND),
        (
            "代重要子公司Yonggu Materials Co., Ltd.公告董事會決議分配盈餘匯回",
            T.DIVIDEND,
        ),
        ("公布本行115年10月自結盈餘相關資訊。", T.MONTHLY_FIGURES),
        ("公告本公司2026年10月合併營業額", T.MONTHLY_FIGURES),
        (
            "(補充115/8/5公告)本公司以租地委建方式於台北港南碼頭新建五期廠房",
            T.SUPPLEMENT,
        ),
        ("公告本公司董事會通過參與設立合資公司", T.ORGANISATION_AND_INVESTMENT),
        (
            "代重要子公司台信電訊(股)公司公告公開收購精誠資訊(股)公司普通股股份",
            T.ORGANISATION_AND_INVESTMENT,
        ),
        ("公告本公司增加投資台塑新智能科技股份有限公司", T.ORGANISATION_AND_INVESTMENT),
        ("公告本公司董事會通過設置永續長", T.EXECUTIVE_CHANGE),
        ("公告本公司資訊安全長異動", T.EXECUTIVE_CHANGE),
        ("公告本公司累積虧損達實收資本額二分之一", T.FINANCIAL_WARNING),
        ("公告本公司董事會決議事項", T.BOARD_RESOLUTION_SUMMARY),
        ("公告本公司參與線上投資人說明會", T.INVITED_INVESTOR_EVENT),
        (
            "係因本公司有價證券於櫃買市場達公布注意交易資訊標準，故公告相關訊息",
            T.MARKET_ATTENTION,
        ),
        (
            "本公司有價證券近期股價異常，故公告相關訊息以利投資人區別瞭解。",
            T.MARKET_ATTENTION,
        ),
        (
            "說明本公司線上平台委外服務廠商91APP發生網路資安事件",
            T.INCIDENT_OR_CLARIFICATION,
        ),
    ],
)
def test_production_regressions(title, expected):
    assert classify_title(title) is expected


# Real titles from the 2026-11-01..13 validation run (samples.csv / notices.csv).
@pytest.mark.parametrize(
    "title,expected",
    [
        # financial reports: 營運報告 and a title truncated by the source
        ("發佈本公司115年第三季合併營運報告之相關內容", T.FINANCIAL_REPORT),
        ("公告本公司董事會通過115年第三季合併財務報", T.FINANCIAL_REPORT),
        # 國際財務報導準則 is not a financial report notice
        ("公告本公司採用國際財務報導準則之會計政策變動", T.OTHER),
        # supplements and corrections refer to an earlier announcement
        ("補充113年12月20日代子公司凱基證券公告取得不動產使用權資產", T.SUPPLEMENT),
        ("更正本公司115年9月關係人交易申報資訊", T.SUPPLEMENT),
        ("更正115年第3季XBRL合併財務報告公告資訊", T.SUPPLEMENT),
        ("公告更正115年8月至9月背書保證申報資訊", T.SUPPLEMENT),
        (
            "(補充115/8/5公告)本公司以租地委建方式於台北港南碼頭新建五期廠房",
            T.SUPPLEMENT,
        ),
        (
            "(更正主旨)本公司董事會通過擬與美國UPower Energy LLC簽署合作備忘錄",
            T.SUPPLEMENT,
        ),
        # ...but a clarification stays a clarification
        (
            "澄清115年11月03日工商時報A13之新聞內容(補充說明)",
            T.INCIDENT_OR_CLARIFICATION,
        ),
        # a word that merely contains 補充 is not a supplement
        ("公告本公司董事會通過子公司上市所出具補充承諾案", T.OTHER),
        # treasury stock
        ("公告董事會決議買回本公司股份", T.TREASURY_STOCK),
        ("本公司董事會通過自集中交易市場買回公司股份", T.TREASURY_STOCK),
        ("本公司董事會決議註銷買回股份辦理減資事宜", T.TREASURY_STOCK),
        # committees: restructuring vs membership
        (
            "公告本公司董事會通過「薪資報酬委員會」改制為「薪酬暨提名委員會」",
            T.COMMITTEE,
        ),
        ("公告本公司董事會決議審計委員會調整為審計暨風險管理委員會", T.COMMITTEE),
        ("公告本公司第二屆提名委員會委員", T.PERSONNEL_OTHER),
        ("公告本公司董事會補選薪資報酬委員會委員", T.PERSONNEL_OTHER),
        ("公告本公司薪資報酬委員會異動", T.PERSONNEL_OTHER),
        ("公告本公司第三屆企業永續發展委員會人員異動案", T.PERSONNEL_OTHER),
        (
            "本公司新藥獲墨西哥衛生風險保護聯邦委員會核准使用於成人真性紅血球增多症",
            T.DRUG_DEVELOPMENT,
        ),
        ("公告本公司獨立董事對審計委員會及董事會議決事項表示反對意見", T.OTHER),
        # investor forums
        (
            "公告本公司參加J.P. Morgan 和Morgan Stanley和BofA舉辦之投資論壇",
            T.INVITED_INVESTOR_EVENT,
        ),
        ("本公司參加康和證券舉辦之投資論壇", T.INVITED_INVESTOR_EVENT),
        # asset transactions
        ("公告本公司購買新造貨櫃", T.ASSET_TRANSACTION),
        ("本公司訂購機器設備公告", T.ASSET_TRANSACTION),
        ("本公司董事會決議購入台中市營建用地", T.ASSET_TRANSACTION),
        ("公告本公司董事會決議購置彰濱東三路12號廠房案", T.ASSET_TRANSACTION),
        ("代孫公司濟南聯合製罐有限公司公告取得理財商品", T.ASSET_TRANSACTION),
        ("代子公司台玻成都玻璃有限公司公告處分理財商品", T.ASSET_TRANSACTION),
        ("公告授信資產之轉讓", T.ASSET_TRANSACTION),
        # security incident wording
        ("公告本公司電商網站資通安全事件及因應措施", T.INCIDENT_OR_CLARIFICATION),
    ],
)
def test_validation_run_regressions(title, expected):
    assert classify_title(title) is expected


# Reproduced from a second review of the validation-run fixes.
@pytest.mark.parametrize(
    "title,expected",
    [
        ("公告本公司選任審計委員會委員", T.PERSONNEL_OTHER),
        ("公告本公司董事會委任第二屆提名委員會委員", T.PERSONNEL_OTHER),
        ("公告本公司董事會補行委任薪資報酬委員會成員", T.PERSONNEL_OTHER),
        ("公告本公司董事會決議設置提名委員會暨委任第一屆提名委員會委員", T.COMMITTEE),
        ("公告本公司審計委員會委任會計師", T.OTHER),
        ("公告本公司參加AI產業論壇", T.OTHER),
        ("公告本公司授信資產管理政策調整", T.OTHER),
        ("公告授信資產之債權處分", T.ASSET_TRANSACTION),
        ("公告本公司取得工程承攬契約", T.CONTRACT),
        (
            "本公司代子公司國泰人壽公告以自地委建方式與關係人交易簽訂工程承攬契約書",
            T.CONTRACT,
        ),
        ("公告本公司取得廠務工程", T.ASSET_TRANSACTION),
        # a correction of an attention notice stays excluded, it is not a candidate
        ("更正公告本公司股票列為注意交易資訊", T.MARKET_ATTENTION),
    ],
)
def test_second_review_regressions(title, expected):
    assert classify_title(title) is expected


# From the CodeRabbit review of the validation-run fixes.
@pytest.mark.parametrize(
    "title,expected",
    [
        ("公告本公司總經理暨永續發展委員會委員異動", T.EXECUTIVE_CHANGE),
        ("公告本公司第二屆提名委員會委員", T.PERSONNEL_OTHER),
        # '股份有限公司' in a counterparty name is not purchased shares
        ("公告本公司簽訂購買甲股份有限公司雲端服務之重大合約", T.CONTRACT),
        ("公告本公司購買乙公司股份", T.ASSET_TRANSACTION),
    ],
)
def test_coderabbit_review_regressions(title, expected):
    assert classify_title(title) is expected


# From the out-of-sample run on 2026-11-16..11-20 (real titles).
@pytest.mark.parametrize(
    "title,expected",
    [
        # exchange-imposed trading status is a market notice, not an asset deal
        ("公告本公司上櫃有價證券經櫃買中心通知列為變更交易方法", T.TRADING_RESTRICTION),
        ("公告本公司股票繼續變更交易方法", T.TRADING_RESTRICTION),
        ("公告本公司接獲櫃買中心通知股票停止買賣日期", T.TRADING_RESTRICTION),
        ("公告本公司處分有價證券", T.ASSET_TRANSACTION),
        # executive resignation / election
        ("公告本公司總經理辭職", T.EXECUTIVE_CHANGE),
        ("公告本公司董事長辭職", T.EXECUTIVE_CHANGE),
        ("代子公司萬洲化學股份有限公司公告推選董事長", T.EXECUTIVE_CHANGE),
        ("公告本公司發言人辭職案", T.PERSONNEL_OTHER),
        ("公告本公司法人董事代表人辭職", T.PERSONNEL_OTHER),
        ("代子公司維克樂科技股份有限公司公告法派董監事異動", T.PERSONNEL_OTHER),
        ("永豐金控公告董事會決議通過子公司京城銀行總稽核異動", T.PERSONNEL_OTHER),
        # investor events
        (
            "公告本公司受花旗證券邀請參加「Citi Nov Taiwan Corporate Day」",
            T.INVITED_INVESTOR_EVENT,
        ),
        ("本公司將參加UBS舉辦之投資人會議", T.INVITED_INVESTOR_EVENT),
        # incidents, contracts, capital
        ("本公司部分資訊系統遭受駭客攻擊之說明", T.INCIDENT_OR_CLARIFICATION),
        (
            "說明本公司接獲疑似冒用名義寄發虛假訂單電子郵件之事件",
            T.INCIDENT_OR_CLARIFICATION,
        ),
        ("本公司與台灣某科技股份有限公司竹科分公司簽署工程合約", T.CONTRACT),
        ("公告本公司「虎尾IDC機房建築工程案」之得標廠商", T.CONTRACT),
        ("代子公司上鋌營造股份有限公司公告董事會決議辦理發行新股", T.CAPITAL_AND_DEBT),
        ("本公司董事會決議通過新臺幣78億元銀行聯合授信案", T.CREDIT_FACILITY),
        ("代子公司國泰世華商業銀行公告聯合授信資產之轉讓", T.ASSET_TRANSACTION),
        # corrections and renames
        ("代子公司更正115年8月~10月份衍生性商品交易資訊", T.SUPPLEMENT),
        ("公告本公司證券簡稱由「沛爾生醫*-創」變更為「沛爾*-創」", T.NAME_OR_PAR_VALUE),
        ("代子公司隆揚電子公告取得理財產品", T.ASSET_TRANSACTION),
        # must not over-reach
        ("公告本公司與銀行簽訂聯合承銷合約", T.OTHER),
        ("公告本公司董事會決議通過資本支出預算案", T.OTHER),
    ],
)
def test_out_of_sample_round_regressions(title, expected):
    assert classify_title(title) is expected


# New labels are intentional product decisions: unlike MARKET_ATTENTION,
# exchange trading restrictions are visible as a recent-focus candidate.
@pytest.mark.parametrize(
    "title,expected",
    [
        ("公告本公司股票繼續維持全額交割", T.TRADING_RESTRICTION),
        ("更正公告本公司股票繼續變更交易方法", T.SUPPLEMENT),
        ("公告本公司上櫃有價證券繼續維持變更交易方法", T.TRADING_RESTRICTION),
        ("公告本公司因股價異常列為注意交易資訊", T.MARKET_ATTENTION),
        ("公告本公司獲銀行團聯合授信案", T.CREDIT_FACILITY),
        ("公告本公司授信資產之轉讓", T.ASSET_TRANSACTION),
        ("公告本公司增資發行新股", T.CAPITAL_AND_DEBT),
        ("公告本公司與AOS等三家公司就美國專利訴訟案簽訂和解協議", T.LEGAL_DISPUTE),
        ("本公司及子公司收到返還貨款、賠償倉儲費之民事起訴狀", T.LEGAL_DISPUTE),
        ("公告本公司最高法院判決結果", T.LEGAL_DISPUTE),
        ("本公司接獲法院公告", T.OTHER),
        (
            "代子公司FBD Biologics Limited公告，美國FDA通知新藥臨床試驗申請實施臨床暫停",
            T.DRUG_DEVELOPMENT,
        ),
        ("本公司開發中新藥於美國啟動臨床二期試驗", T.DRUG_DEVELOPMENT),
        ("本公司參加AI產業論壇", T.OTHER),
        ("本公司受邀參加開源技術大會", T.OTHER),
        ("代&#12070;公司隆揚電&#12070;公告取得理財產品", T.ASSET_TRANSACTION),
    ],
)
def test_round4_policy_and_false_positive_regressions(title, expected):
    assert classify_title(title) is expected


def test_subsidiary_entity_decoding_and_announcing_typo():
    assert is_subsidiary_notice("代&#12070;公司隆揚電&#12070;公告取得理財產品")
    assert is_subsidiary_notice("代公告子公司共信寵物生醫股份有限公司申請藥證")


# Synthetic boundary cases: not additional out-of-sample production evidence.
@pytest.mark.parametrize(
    "title,expected",
    [
        ("公告本公司副總經理異動", T.PERSONNEL_OTHER),
        ("公告本公司副董事長辭任", T.PERSONNEL_OTHER),
        ("公告本公司推選副董事長", T.PERSONNEL_OTHER),
        ("公告本公司副執行長異動", T.PERSONNEL_OTHER),
        ("公告本公司總經理暨副總經理異動", T.EXECUTIVE_CHANGE),
        ("公告本公司銀行聯合授信 資產之轉讓", T.ASSET_TRANSACTION),
        ("公告本公司銀行授信案之授信資產轉讓", T.ASSET_TRANSACTION),
        ("公告本公司處分聯合授信資產", T.ASSET_TRANSACTION),
        ("公告本公司董事會通過銀行授信額度", T.CREDIT_FACILITY),
        ("公告本公司理財產品管理政策調整", T.OTHER),
        ("公告本公司贖回理財產品", T.ASSET_TRANSACTION),
        ("公告本公司關係人交易管理政策", T.OTHER),
        ("公告本公司取得乙公司股份", T.ASSET_TRANSACTION),
        ("公告本公司代子公司甲公告更正115年9月營收", T.SUPPLEMENT),
        ("公告本公司補充承諾案", T.OTHER),
        ("代子公司甲公告董事會決議補充營運資金辦理現金增資", T.CAPITAL_AND_DEBT),
        ("公告本公司受邀參加投資人關係管理課程", T.OTHER),
        ("公告本公司受邀參加投資人會議", T.INVITED_INVESTOR_EVENT),
    ],
)
def test_additional_boundary_regressions(title, expected):
    assert classify_title(title) is expected
