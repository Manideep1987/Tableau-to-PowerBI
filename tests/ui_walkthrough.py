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
    step("UI-01 Home shows upload zone and 4 samples (finance showcase first)",
         page.locator("[data-sample]").count() == 4 and
         page.locator("[data-sample]").first.get_attribute("data-sample") == "Invoice_Health.twb")

    page.click("[data-sample='Invoice_Health.twb']")
    page.wait_for_selector("#convert-result:not([hidden]) #stats .stat")
    page.wait_for_timeout(200)
    page.screenshot(path=f"{OUT}/02_inventory.png", full_page=False)
    step("UI-02 Conversion opens on the inventory (parse completeness + feature support)",
         page.locator("#stats .stat").count() == 6 and "Parse completeness" in page.inner_text("#tab-body")
         and page.locator("#steps li.active").inner_text().endswith("Inventory"))
    page.click("#result-tabs button[data-tab='calcs']")
    page.wait_for_timeout(200)
    rows = page.locator("#calc-body tr").count()
    page.screenshot(path=f"{OUT}/03_mapping.png", full_page=False)
    step("UI-03 Mapping tab lists Tableau->DAX with PASS/REVIEW/UNSUPPORTED", rows >= 15 and
         "REVIEW" in page.inner_text("#calc-body"), f"{rows} rows")
    page.click(".chip[data-st='REVIEW']")
    page.wait_for_timeout(150)
    step("UI-04 Classification filter", 0 < page.locator("#calc-body tr").count() < rows)
    page.click(".chip[data-st='All']")
    page.click("[data-edit='On-time %']")
    page.fill("[data-editor='On-time %'] textarea", "DIVIDE([On Time Paid Amount], [Nope])")
    page.click("[data-save='On-time %']")
    page.wait_for_timeout(400)
    rejected = "unknown measure" in page.inner_text("[data-editor='On-time %'] [data-msg]")
    page.fill("[data-editor='On-time %'] textarea", "DIVIDE([On Time Paid Amount], [Paid Amount], 0)")
    page.fill("[data-editor='On-time %'] [data-note]", "Return 0 instead of blank when nothing is paid")
    page.click("[data-save='On-time %']")
    page.wait_for_selector("#calc-body tr[data-obj='On-time %'] .pill.warn")
    page.screenshot(path=f"{OUT}/04_override.png", full_page=False)
    step("UI-05 DAX override: invalid reference rejected, valid override applied with lineage", rejected and
         "Original:" in page.inner_text("#calc-body tr[data-obj='On-time %']"))

    for tab, shot in (("data", "05_data_sql"), ("model", "06_model"), ("layout", "07_layout"),
                      ("filters", "08_filters_params"), ("security", "09_security"), ("issues", "10_issues")):
        page.click(f"#result-tabs button[data-tab='{tab}']")
        page.wait_for_timeout(250)
        page.screenshot(path=f"{OUT}/{shot}.png", full_page=False)
        step(f"UI-06 Tab '{tab}' renders", len(page.inner_text("#tab-body")) > 50)
    page.click("#result-tabs button[data-tab='security']")
    page.wait_for_timeout(150)
    step("UI-07 Security tab shows the RLS role converted from USERNAME()",
         "USERPRINCIPALNAME()" in page.inner_text("#tab-body") and "Manager Security" in page.inner_text("#tab-body"))
    page.click("#result-tabs button[data-tab='layout']")
    page.wait_for_timeout(200)
    step("UI-08 Report page wireframe draws visuals", page.locator(".canvas .vis").count() >= 6,
         f"{page.locator('.canvas .vis').count()} visuals")

    page.click("#btn-to-validate")
    page.wait_for_selector("#validate-form:not([hidden])")
    page.click("#btn-validate")
    page.wait_for_selector(".gates .gate", timeout=120000)
    page.screenshot(path=f"{OUT}/11_validation_gates.png", full_page=False)
    gates = page.locator(".gates .gate").count()
    step("UI-09 Validation shows 5 gates, readiness and verdict", gates == 5 and
         "Readiness score" in page.inner_text("#validate-result"), page.inner_text(".banner b")[:80])
    step("UI-10 KPI register and identity matrix rendered",
         "KPI regression register" in page.inner_text("#validate-result") and
         "identity matrix" in page.inner_text("#validate-result").lower())
    page.locator("h2:has-text('KPI regression register')").scroll_into_view_if_needed()
    page.screenshot(path=f"{OUT}/12_kpi_register.png", full_page=False)
    page.locator("h2:has-text('identity matrix')").scroll_into_view_if_needed()
    page.screenshot(path=f"{OUT}/13_identity_matrix.png", full_page=False)
    page.click("#validate-result details[data-status='PASS'] summary >> nth=12")
    page.wait_for_timeout(200)
    page.screenshot(path=f"{OUT}/14_validation_detail.png", full_page=False)
    step("UI-11 Check detail shows comparison table", page.locator("#validate-result details[open] table").count() >= 1)
    page.click(".chip[data-vf='PENDING']")
    page.wait_for_timeout(150)
    vis = page.locator("#validate-result details[data-status]:not([hidden])").count()
    step("UI-12 Human checks are PENDING, never simulated as passed", vis >= 3, f"{vis} PENDING checks")
    page.click(".chip[data-vf='All']")
    page.fill("#so-who", "Priya Shah, AP Finance Director")
    page.select_option("#so-dec", "approved-with-redesign")
    page.fill("#so-notes", "Reviewed KPI register and aging matrix; On-time % returns 0 instead of blank (override)")
    page.click("#btn-signoff")
    page.wait_for_timeout(400)
    page.locator("h2:has-text('Gate 5')").scroll_into_view_if_needed()
    page.screenshot(path=f"{OUT}/15_signoff.png", full_page=False)
    step("UI-13 Business sign-off recorded for gate 5", "Priya Shah" in page.inner_text("#validate-result"))

    page.click("nav button[data-view='export']")
    page.wait_for_selector("#export-body table")
    page.screenshot(path=f"{OUT}/16_export.png", full_page=False)
    step("UI-14 Export lists all artifacts", all(x in page.inner_text("#export-body") for x in
                                                ("migration_report.json", "lineage.csv", "validation_results.csv",
                                                 "ir.json", "uat_signoff.json")))
    with page.expect_download() as dl:
        page.click("#export-body a[href$='/pbip']")
    d = dl.value
    import zipfile as _zf
    names = _zf.ZipFile(d.path()).namelist()
    step("UI-15 PBIP zip contains project + manifest + lineage + validation CSV + sign-off",
         all(any(n.endswith(x) for n in names) for x in (".pbip", "migration_report.json", "lineage.csv",
                                                          "validation_results.csv", "uat_signoff.json", "Manager Security.tmdl")),
         d.suggested_filename)

    page.click("nav button[data-view='translate']")
    page.uncheck("#use-job")
    page.fill("#formula", "{FIXED [Customer Name] : MIN([Order Date])}")
    page.click("#btn-translate")
    page.wait_for_selector("#translate-out pre.code")
    out = page.inner_text("#translate-out pre.code")
    page.screenshot(path=f"{OUT}/18_translator.png", full_page=False)
    step("UI-16 Formula translator returns DAX", "CALCULATE(MIN('Orders'[Order Date])" in out, out[:80])
    page.fill("#formula", "SUM([Sales]")
    page.click("#btn-translate")
    page.wait_for_timeout(400)
    step("UI-17 Translator reports syntax errors", "Syntax error" in page.inner_text("#translate-out"))

    page.click("nav button[data-view='reference']")
    page.wait_for_selector("#ref-body tr")
    n = page.locator("#ref-body tr").count()
    page.fill("#ref-search", "RANK")
    page.wait_for_timeout(200)
    page.screenshot(path=f"{OUT}/19_reference.png", full_page=False)
    step("UI-18 DAX reference lists and searches functions", n >= 70 and page.locator("#ref-body tr").count() < n, f"{n} entries")

    page.click("nav button[data-view='compare']")
    here = os.path.dirname(os.path.abspath(__file__))
    page.set_input_files("#cmp-t", os.path.join(here, "fixtures", "tableau_export.csv"))
    page.set_input_files("#cmp-p", os.path.join(here, "fixtures", "powerbi_export.csv"))
    page.click("#btn-compare")
    page.wait_for_selector("#compare-out .banner")
    page.screenshot(path=f"{OUT}/20_compare_exports.png", full_page=False)
    step("UI-19 Export comparison detects the seeded mismatch", page.inner_text("#compare-out .banner .big") == "FAIL")

    mob = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=SCALE)
    mob.goto(BASE + "/#convert")
    mob.wait_for_selector("[data-sample]")
    sw = mob.evaluate("document.documentElement.scrollWidth")
    mob.screenshot(path=f"{OUT}/21_mobile.png", full_page=False)
    step("UI-20 Responsive at phone width (no horizontal scroll)", sw <= 390, f"scrollWidth={sw}")

    dark = b.new_page(viewport={"width": 1366, "height": 900}, color_scheme="dark", device_scale_factor=SCALE)
    dark.goto(BASE + "/#reference")
    dark.wait_for_selector("#ref-body tr")
    bg = dark.evaluate("getComputedStyle(document.body).backgroundColor")
    dark.screenshot(path=f"{OUT}/22_dark_mode.png", full_page=False)
    step("UI-21 Dark mode theme applies", bg != "rgb(246, 247, 249)", bg)
    step("UI-22 No JavaScript errors", not errors, "; ".join(errors))
    b.close()

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} UI checks passed")
sys.exit(1 if failed else 0)
