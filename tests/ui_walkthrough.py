"""End-to-end UI walkthrough with Playwright (used for UAT screenshots).

    python tests/ui_walkthrough.py http://127.0.0.1:8765 docs/screenshots
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
OUT = sys.argv[2] if len(sys.argv) > 2 else "docs/screenshots"
SCALE = float(os.environ.get("SHOT_SCALE", "1"))
os.makedirs(OUT, exist_ok=True)
results = []


def step(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail else ""))


with sync_playwright() as p:
    exe = os.environ.get("PW_CHROMIUM", "/opt/pw-browsers/chromium")
    b = p.chromium.launch(executable_path=exe if os.path.exists(exe) else None)
    page = b.new_page(viewport={"width": 1366, "height": 900}, device_scale_factor=SCALE)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE + "/#convert")
    page.wait_for_selector("[data-sample]")
    page.screenshot(path=f"{OUT}/01_convert_start.png", full_page=True)
    step("UI-01 Home shows upload zone and 3 samples", page.locator("[data-sample]").count() == 3)

    page.click("[data-sample='Superstore_Sales.twb']")
    page.wait_for_selector("#convert-result:not([hidden]) #stats .stat")
    page.screenshot(path=f"{OUT}/02_conversion_summary.png", full_page=False)
    step("UI-02 Sample conversion renders summary cards", page.locator("#stats .stat").count() == 6)
    rows = page.locator("#calc-body tr").count()
    step("UI-03 Calculations tab lists Tableau->DAX mappings", rows >= 30, f"{rows} rows")
    page.fill("#calc-q", "FIXED")
    page.wait_for_timeout(200)
    step("UI-04 Calculation search filters rows", 0 < page.locator("#calc-body tr").count() < rows)
    page.screenshot(path=f"{OUT}/03_calculations_search.png", full_page=False)
    page.fill("#calc-q", "")

    for tab, shot in (("data", "04_data_sql"), ("model", "05_model"), ("layout", "06_layout"),
                      ("filters", "07_filters_params"), ("issues", "08_issues")):
        page.click(f"#result-tabs button[data-tab='{tab}']")
        page.wait_for_timeout(250)
        page.screenshot(path=f"{OUT}/{shot}.png", full_page=False)
        step(f"UI-05 Tab '{tab}' renders", len(page.inner_text("#tab-body")) > 50)
    page.click("#result-tabs button[data-tab='layout']")
    page.wait_for_timeout(200)
    step("UI-06 Layout wireframe draws visuals", page.locator(".canvas .vis").count() >= 6,
         f"{page.locator('.canvas .vis').count()} visuals")

    with page.expect_download() as dl:
        page.click("#dl-pbip")
    d = dl.value
    step("UI-07 PBIP zip download", d.suggested_filename.endswith("_PowerBI.zip"), d.suggested_filename)

    page.click("#btn-to-validate")
    page.wait_for_selector("#validate-form:not([hidden])")
    page.screenshot(path=f"{OUT}/09_validate_form.png", full_page=False)
    page.click("#btn-validate")
    page.wait_for_selector(".banner", timeout=120000)
    banner = page.inner_text(".banner .big")
    page.screenshot(path=f"{OUT}/10_validation_results.png", full_page=False)
    step("UI-08 Validation runs and shows overall status", banner in ("PASS", "WARN"), banner)
    page.click("#validate-result details[data-status='PASS'] summary >> nth=10")
    page.wait_for_timeout(200)
    page.screenshot(path=f"{OUT}/11_validation_detail.png", full_page=False)
    step("UI-09 Check detail shows comparison table", page.locator("#validate-result details[open] table").count() >= 1)
    page.click(".chip[data-vf='WARN']")
    page.wait_for_timeout(150)
    vis = page.locator("#validate-result details[data-status]:not([hidden])").count()
    step("UI-10 Status filter chips filter checks", vis >= 1, f"{vis} WARN checks visible")

    page.click("nav button[data-view='translate']")
    page.fill("#formula", "{FIXED [Customer Name] : MIN([Order Date])}")
    page.click("#btn-translate")
    page.wait_for_selector("#translate-out pre.code")
    out = page.inner_text("#translate-out pre.code")
    page.screenshot(path=f"{OUT}/12_translator.png", full_page=False)
    step("UI-11 Formula translator returns DAX", "CALCULATE(MIN('Orders'[Order Date])" in out, out[:80])
    page.fill("#formula", "SUM([Sales]")
    page.click("#btn-translate")
    page.wait_for_timeout(400)
    step("UI-12 Translator reports syntax errors", "Syntax error" in page.inner_text("#translate-out"))

    page.click("nav button[data-view='reference']")
    page.wait_for_selector("#ref-body tr")
    n = page.locator("#ref-body tr").count()
    page.fill("#ref-search", "RANK")
    page.wait_for_timeout(200)
    page.screenshot(path=f"{OUT}/13_reference.png", full_page=False)
    step("UI-13 DAX reference lists and searches functions", n >= 70 and page.locator("#ref-body tr").count() < n, f"{n} entries")

    page.click("nav button[data-view='compare']")
    here = os.path.dirname(os.path.abspath(__file__))
    page.set_input_files("#cmp-t", os.path.join(here, "fixtures", "tableau_export.csv"))
    page.set_input_files("#cmp-p", os.path.join(here, "fixtures", "powerbi_export.csv"))
    page.click("#btn-compare")
    page.wait_for_selector("#compare-out .banner")
    page.screenshot(path=f"{OUT}/14_compare_exports.png", full_page=False)
    step("UI-14 Export comparison detects the seeded mismatch", page.inner_text("#compare-out .banner .big") == "FAIL")

    mob = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=SCALE)
    mob.goto(BASE + "/#convert")
    mob.wait_for_selector("[data-sample]")
    sw = mob.evaluate("document.documentElement.scrollWidth")
    mob.screenshot(path=f"{OUT}/15_mobile.png", full_page=False)
    step("UI-15 Responsive at phone width (no horizontal scroll)", sw <= 390, f"scrollWidth={sw}")

    dark = b.new_page(viewport={"width": 1366, "height": 900}, color_scheme="dark", device_scale_factor=SCALE)
    dark.goto(BASE + "/#reference")
    dark.wait_for_selector("#ref-body tr")
    bg = dark.evaluate("getComputedStyle(document.body).backgroundColor")
    dark.screenshot(path=f"{OUT}/16_dark_mode.png", full_page=False)
    step("UI-16 Dark mode theme applies", bg != "rgb(246, 247, 249)", bg)
    step("UI-17 No JavaScript errors", not errors, "; ".join(errors))
    b.close()

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} UI checks passed")
sys.exit(1 if failed else 0)
