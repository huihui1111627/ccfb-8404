"""Browser smoke test: drive the real console through every page.

Run with:  .venv/bin/python tests/browser_smoke.py [base_url]

Checks that each page loads its data, the map draws OD lines, and no
JavaScript errors reach the console.
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8404"
OUT = "/tmp/odp_shots"


def main() -> int:
    import os
    os.makedirs(OUT, exist_ok=True)
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1560, "height": 980})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(800)
        assert page.locator("#run-select option").count() >= 1
        page.screenshot(path=f"{OUT}/00_boot.png")

        # if there is no finished run yet, trigger the demo run and wait
        if page.locator("#run-select").input_value() == "":
            page.click('#nav button[data-page="import"]')
            page.wait_for_timeout(300)
            page.click("#btn-demo-load")
            page.wait_for_function(
                "() => document.querySelector('#run-select').value !== ''", timeout=60000)
            page.wait_for_timeout(5000)
        else:
            page.click('#nav button[data-page="import"]')
            page.wait_for_timeout(300)
            page.click("#btn-demo-load")
            page.wait_for_timeout(6000)
        page.click('#nav button[data-page="dashboard"]')
        page.wait_for_timeout(1000)
        page.wait_for_timeout(1200)
        page.screenshot(path=f"{OUT}/01_dashboard.png")

        for slug, label in [("import", "数据导入"), ("matching", "站点匹配"),
                            ("chains", "出行链"), ("od", "OD 矩阵"),
                            ("metrics", "指标看板"), ("compare", "方案对比"),
                            ("params", "参数设置")]:
            page.click(f'#nav button[data-page="{slug}"]')
            page.wait_for_timeout(1200 if slug in ("od", "metrics") else 600)
            title = page.locator("#page-title").inner_text()
            assert title == label, f"{slug}: title {title!r} != {label!r}"
            page.screenshot(path=f"{OUT}/02_{slug}.png", full_page=(slug != "od"))

        # map drew polylines?
        page.click('#nav button[data-page="od"]')
        page.wait_for_timeout(1600)
        paths = page.locator("#map path.leaflet-interactive").count()
        print("map polylines:", paths)
        assert paths > 0, "no OD lines drawn on map"
        page.screenshot(path=f"{OUT}/03_map.png")

        # click a row in OD list, then a chain row to open card detail
        page.click('#nav button[data-page="chains"]')
        page.wait_for_timeout(800)
        rows = page.locator("#ch-table tbody tr").count()
        print("chain rows:", rows)
        assert rows > 0
        page.locator("#ch-table tbody tr").first.click()
        page.wait_for_timeout(800)
        assert page.locator("#ch-detail-panel").is_visible()
        page.screenshot(path=f"{OUT}/04_chain_detail.png")

        # dataset preview modal: clean datasets show a read-only mapping board
        page.click('#nav button[data-page="import"]')
        page.wait_for_timeout(500)
        page.locator('#dataset-list .run-item', has_text="演示数据").first \
            .locator('button[data-act="preview"]').click()
        page.wait_for_timeout(500)
        assert page.locator("#modal-back").is_visible()
        assert page.locator("#modal-body .mapping-row").count() >= 4
        page.click("#modal-close")
        page.wait_for_timeout(300)

        # run a second inference with a changed parameter to exercise compare
        page.click('#nav button[data-page="params"]')
        page.wait_for_timeout(500)
        name = page.locator("#pm-run-name")
        name.fill("对比运行 · 换乘阈值 40 分钟")
        win = page.locator('#pm-grid input[data-key="transfer_window_min"]')
        win.fill("40")
        win.dispatch_event("change")
        page.click("#pm-submit")
        page.wait_for_timeout(1000)
        page.wait_for_function(
            """() => {
                const opts = [...document.querySelectorAll('#cmp-select input')];
                return opts.length >= 2 && !document.querySelector('#pm-submit').disabled;
            }""", timeout=60000)
        page.wait_for_timeout(6000)
        page.screenshot(path=f"{OUT}/05_params_run.png")

        page.click('#nav button[data-page="compare"]')
        page.wait_for_timeout(500)
        boxes = page.locator("#cmp-select input")
        boxes.nth(0).check()
        boxes.nth(1).check()
        page.click("#cmp-run")
        page.wait_for_timeout(1200)
        page.screenshot(path=f"{OUT}/06_compare.png", full_page=True)
        assert page.locator("#cmp-result table").count() >= 2

        browser.close()

    if errors:
        print("CONSOLE ERRORS:")
        for e in errors:
            print("  -", e)
        return 1
    print("browser smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
