"""Fetch daily bars for paper picks so the board line chart has data."""
from insightboard.collector import AkshareQuoteCollector
from insightboard.store import BoardStore
from insightagent.env import resolve_path

CODES = ("000858", "000333", "601318", "300308")


def main() -> None:
    store = BoardStore(resolve_path("", default_relative="data/board.db"))
    store.initialize()
    collector = AkshareQuoteCollector()
    for code in CODES:
        print("bars", code, flush=True)
        try:
            bars, notices = collector.collect_deep(code)
            store.save_deep(bars, notices, source=collector.source)
            print("saved", code, len(bars), "bars", len(notices), "notices", flush=True)
        except Exception as error:
            print("fail", code, type(error).__name__, error, flush=True)


if __name__ == "__main__":
    main()
