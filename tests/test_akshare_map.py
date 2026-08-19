from insightagent.akshare_map import (
    CANONICAL_UNITS,
    convert_unit,
    map_akshare_row,
    parse_quantity,
)


def test_canonical_units_for_agent_fields():
    assert CANONICAL_UNITS["operating_cf"] == "yi_yuan"
    assert CANONICAL_UNITS["net_profit"] == "yi_yuan"
    assert CANONICAL_UNITS["volume"] == "lot"
    assert CANONICAL_UNITS["roe"] == "percent"
    assert CANONICAL_UNITS["close"] == "yuan"


def test_parse_quantity_keeps_suffix_unit():
    assert parse_quantity("80.63亿") == (80.63, "yi_yuan")
    assert parse_quantity("6.50%") == (6.5, "percent")
    assert parse_quantity("增持129.53万") == (129.53, "wan")
    assert parse_quantity(-2535371232.04) == (-2535371232.04, None)


def test_eastmoney_and_sina_cashflow_align_to_yi_yuan():
    em = map_akshare_row(
        "stock_cash_flow_sheet_by_report_em",
        {
            "REPORT_DATE": "2026-03-31 00:00:00",
            "NETCASH_OPERATE": -2535371232.04,
            "NETPROFIT": 8062765000.0,
        },
    )
    sina = map_akshare_row(
        "stock_financial_report_sina",
        {
            "报告日": "20260331",
            "经营活动产生的现金流量净额": -2535371232.04,
            "净利润": 8062765000.0,
        },
    )
    assert em["date"] == sina["date"] == "2026-03-31"
    assert em["operating_cf"] == sina["operating_cf"] == -25.3537
    assert em["net_profit"] == sina["net_profit"] == 80.6277


def test_ths_and_em_financials_align_percent_and_yi_yuan():
    ths = map_akshare_row(
        "stock_financial_abstract_ths",
        {
            "报告期": "2026-03-31",
            "净利润": "80.63亿",
            "净利润同比增长率": "82.57%",
            "营业总收入同比增长率": "33.67%",
            "销售毛利率": "81.43%",
            "净资产收益率": "6.50%",
            "资产负债率": "34.31%",
            "流动比率": 2.56,
        },
    )
    em = map_akshare_row(
        "stock_financial_analysis_indicator_em",
        {
            "REPORT_DATE": "2026-03-31 00:00:00",
            "ROEJQ": 6.50,
            "TOTALOPERATEREVETZ": 33.666964,
            "PARENTNETPROFITTZ": 82.567786,
            "XSMLL": 81.434385,
            "ZCFZL": 34.313309,
            "LD": 2.559561,
            "PARENTNETPROFIT": 8.062765e9,
        },
    )
    assert ths["roe"] == em["roe"] == 6.5
    assert abs(ths["net_profit"] - em["net_profit"]) < 0.01
    assert abs(ths["profit_yoy"] - em["profit_yoy"]) < 0.1
    assert abs(ths["debt_ratio"] - em["debt_ratio"]) < 0.02


def test_tencent_hist_does_not_treat_turnover_as_amount():
    mapped = map_akshare_row(
        "stock_zh_a_hist_tx",
        {
            "date": "2026-08-18",
            "open": 72.41,
            "close": 72.56,
            "high": 72.78,
            "low": 72.14,
            "volume": 192681.0,
            "turnover": 0.0050,
            "amount": 1.395188e9,
        },
    )
    em = map_akshare_row(
        "stock_zh_a_hist",
        {
            "日期": "2026-08-18",
            "开盘": 72.41,
            "收盘": 72.56,
            "最高": 72.78,
            "最低": 72.14,
            "成交量": 192681,
            "成交额": 1.395188e9,
        },
    )
    assert mapped["turnover"] == em["turnover"] == 1.395188e9
    assert mapped["volume"] == em["volume"] == 192681.0
    assert mapped["close"] == em["close"] == 72.56


def test_holder_change_wan_aligns_to_shares():
    mapped = map_akshare_row(
        "stock_shareholder_change_ths",
        {
            "公告日期": "2026-08-07",
            "变动股东": "四川省宜宾五粮液集团有限公司",
            "变动数量": "增持129.53万",
            "变动途径": "二级市场",
        },
    )
    assert mapped["change_shares"] == 1_295_300.0
    assert mapped["raw_change"] == "增持129.53万"
    assert mapped["published_at"] == "2026-08-07"


def test_convert_unit_yuan_to_yi_yuan():
    assert convert_unit(8_062_765_000.0, declared="yuan", canonical="yi_yuan") == 80.6277
    assert convert_unit("80.63亿", declared="yuan", canonical="yi_yuan") == 80.63
