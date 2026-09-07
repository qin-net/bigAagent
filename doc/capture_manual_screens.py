"""Capture live bigAagent screenshots for the user manual. Not a product dependency."""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "doc" / "manual-screenshots"
BASE = "http://127.0.0.1:8000/"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=2,
        )
        page.goto(BASE, wait_until="networkidle", timeout=60_000)
        page.wait_for_selector("#cover-enter")
        page.screenshot(path=str(OUT / "fig-cover.png"))

        page.click("#cover-enter")
        page.wait_for_selector("#quotes tr", timeout=30_000)
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT / "fig-market.png"))

        page.fill("#search", "000858")
        page.click("#refresh")
        page.wait_for_timeout(900)
        page.screenshot(path=str(OUT / "fig-market-search.png"))

        page.evaluate("() => detail('000858')")
        page.wait_for_function(
            "() => document.querySelector('#detail h2') && document.querySelector('#detail h2').textContent.includes('000858')",
            timeout=30_000,
        )
        page.wait_for_timeout(1200)
        page.screenshot(path=str(OUT / "fig-detail-hero.png"))
        page.locator("#stage-research").screenshot(path=str(OUT / "fig-analyze.png"))
        page.locator("#stage-invest").screenshot(path=str(OUT / "fig-invest.png"))
        page.locator("#stage-track").screenshot(path=str(OUT / "fig-track.png"))
        page.locator("#stage-learn").screenshot(path=str(OUT / "fig-learn.png"))
        page.locator("#chart-panel").screenshot(path=str(OUT / "fig-chart.png"))
        notices = page.locator("#detail-view .content-card").last
        if notices.count():
            notices.screenshot(path=str(OUT / "fig-notices.png"))
        page.screenshot(path=str(OUT / "fig-detail-full.png"), full_page=True)

        page.evaluate("() => loadWatchlist()")
        page.wait_for_timeout(900)
        page.screenshot(path=str(OUT / "fig-watchlist.png"))

        page.evaluate("() => loadPaper()")
        page.wait_for_function(
            "() => document.querySelector('#paper .portfolio-summary, #paper .notice')",
            timeout=20_000,
        )
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT / "fig-paper.png"))

        page.evaluate("() => loadProfile()")
        page.wait_for_function(
            "() => document.querySelector('#profile .persona-hero, #profile .persona-empty, #profile .notice')",
            timeout=20_000,
        )
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT / "fig-profile.png"))

        page.evaluate("() => loadExperts()")
        page.wait_for_selector(".expert-card", timeout=20_000)
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT / "fig-experts.png"))
        page.screenshot(path=str(OUT / "fig-experts-full.png"), full_page=True)

        browser.close()
    print("saved", len(list(OUT.glob("fig-*.png"))), "figures to", OUT)


if __name__ == "__main__":
    main()
