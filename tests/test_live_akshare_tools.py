import pytest

from insightagent.market import AkshareMarketClient, MarketService
from insightagent.tools import build_market_tools


pytestmark = pytest.mark.live

TOOL_CASES = (
    ("fetch_stock_profile", {"stock_code": "000858"}),
    ("fetch_quote", {"stock_code": "000858"}),
    ("fetch_valuation", {"stock_code": "000858"}),
    ("fetch_financials", {"stock_code": "000858"}),
    ("fetch_cashflow", {"stock_code": "000858"}),
    ("get_price_snapshot", {"stock_code": "000858"}),
    ("get_indicator_snapshot", {"stock_code": "000858"}),
    ("get_kline_snapshot", {"stock_code": "000858", "limit": 10}),
    ("get_event_snapshot", {"stock_code": "000858"}),
    ("get_macro_snapshot", {}),
    ("search_announcements", {"stock_code": "000858", "query": "", "limit": 5}),
    ("search_news", {"stock_code": "000858", "query": "", "limit": 5}),
    ("get_holder_changes", {"stock_code": "000858"}),
    ("get_fundamental_snapshot", {"stock_code": "000858"}),
)


def _akshare_available() -> bool:
    try:
        import akshare  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.asyncio
@pytest.mark.skipif(not _akshare_available(), reason="akshare is not installed")
async def test_live_akshare_each_tool():
    tools = {
        tool.spec.name: tool
        for tool in build_market_tools(MarketService(AkshareMarketClient()))
    }
    results = {}
    for name, arguments in TOOL_CASES:
        try:
            payload = await tools[name].invoke(
                arguments, idempotency_key="live-{}".format(name)
            )
            results[name] = {"ok": True, "payload": payload}
            print("OK", name, _brief(payload))
        except Exception as error:
            results[name] = {"ok": False, "error": "{}: {}".format(type(error).__name__, error)}
            print("FAIL", name, results[name]["error"])

    succeeded = [name for name, item in results.items() if item["ok"]]
    failed = [name for name, item in results.items() if not item["ok"]]
    # Core price path must work; announcements/macro may be source-flaky.
    assert "fetch_quote" in succeeded or "get_kline_snapshot" in succeeded, failed
    assert "get_indicator_snapshot" in succeeded, failed
    indicator = results["get_indicator_snapshot"]["payload"]
    assert indicator.get("ma5") is not None or indicator.get("bars_used", 0) >= 20
    cashflow = results.get("fetch_cashflow", {}).get("payload") or {}
    if cashflow.get("operating_cf") is not None:
        assert abs(cashflow["operating_cf"]) < 10_000
    assert len(succeeded) >= 8, failed


def _brief(payload):
    if not isinstance(payload, dict):
        return payload
    keys = (
        "stock_code",
        "company_name",
        "price",
        "pe",
        "roe",
        "operating_cf",
        "lpr_1y",
        "bars_used",
        "ma5",
        "last_close",
    )
    return {key: payload.get(key) for key in keys if key in payload}
