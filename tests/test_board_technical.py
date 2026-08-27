from insightboard.contracts import DailyBar, QuoteInput
from insightboard.research import BoardTechnicalAdapter
from insightboard.store import BoardStore


async def test_board_technical_adapter_uses_local_bars(tmp_path):
    store = BoardStore(str(tmp_path / "board.db")); store.initialize()
    store.replace_quotes([QuoteInput(stock_code="000858", name="测试", price=100)], source="fixture")
    bars = [DailyBar("000858", f"2026-01-{index+1:02d}", 100+index, 102+index, 99+index, 101+index, 1000+index, 0) for index in range(20)]
    store.save_deep(bars, [], source="fixture")
    payload = await BoardTechnicalAdapter(store).fetch_technical("000858")
    assert payload["indicator"]["source"] == "computed"
    assert payload["indicator"]["bars_used"] == 20
    assert payload["kline"]["source"] == "insightboard"
    assert payload["price"]["source"] == "insightboard"


async def test_board_technical_adapter_fails_for_fallback(tmp_path):
    store = BoardStore(str(tmp_path / "board.db")); store.initialize()
    try:
        await BoardTechnicalAdapter(store).fetch_technical("000858")
    except RuntimeError as error:
        assert "unavailable" in str(error)
    else:
        raise AssertionError("missing board bars must trigger fallback")
