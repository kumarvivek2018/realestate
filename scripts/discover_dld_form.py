"""Inspect the DLD open-data form to discover required fields.

We open the page, click the Transactions tab, and dump every visible input
field (id, name, type, required, value, placeholder), plus the actual
network requests fired when 'Download as CSV' is clicked.
"""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "data" / "raw"
PAGE = "https://dubailand.gov.ae/en/open-data/real-estate-data/"


def main():
    captured: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        def on_request(req):
            url = req.url
            if any(k in url.lower() for k in ("opendata", "transaction", "rent", "export", ".csv", ".xlsx")):
                captured.append({
                    "method": req.method,
                    "url": url,
                    "post_data": req.post_data,
                })

        def on_response(resp):
            url = resp.url
            if any(k in url.lower() for k in ("opendata", "transaction", "rent", "export", ".csv", ".xlsx")):
                ct = resp.headers.get("content-type", "")
                cl = resp.headers.get("content-length", "?")
                print(f"  RESP {resp.status} ct={ct[:40]} len={cl}  {url[:100]}")

        page.on("request", on_request)
        page.on("response", on_response)

        page.goto(PAGE, wait_until="networkidle", timeout=60_000)

        # Click Transactions tab
        print("clicking Transactions tab...")
        page.locator("a:has-text('Transactions')").first.click()
        page.wait_for_timeout(1500)

        # Find the active tab pane
        # Dump every input/select inside visible tab content
        print("\n=== form fields in active tab pane ===")
        fields = page.evaluate("""() => {
            const visible = (e) => e.offsetParent !== null;
            const result = [];
            document.querySelectorAll('input,select,textarea').forEach(el => {
                if (!visible(el)) return;
                result.push({
                    tag: el.tagName,
                    type: el.type || null,
                    id: el.id || null,
                    name: el.name || null,
                    placeholder: el.placeholder || null,
                    required: el.required,
                    value: el.value || null,
                    label: (el.labels && el.labels[0] && el.labels[0].textContent.trim()) || null,
                });
            });
            return result;
        }""")
        for f in fields:
            print(f"  {f['tag']:8} type={f['type']!s:8} req={f['required']!s:5}  id={f['id']!s:30}  name={f['name']!s:30}  label={f['label']!r}")

        # Try clicking Download as CSV without filling — capture what happens
        print("\n=== clicking Download as CSV (no fields filled) ===")
        btn = page.locator("button:has-text('Download as CSV'):visible").first
        if btn.is_visible():
            try:
                with page.expect_download(timeout=15_000) as dl_info:
                    btn.click()
                dl = dl_info.value
                print(f"  download triggered: {dl.suggested_filename}")
            except Exception as e:
                print(f"  no download: {type(e).__name__}: {e}")
                # Look for visible error/validation messages
                page.wait_for_timeout(1500)
                msgs = page.evaluate("""() => Array.from(document.querySelectorAll('.error, .validation-summary-errors, .field-validation-error, [class*=\"error\"]:not(:empty)'))
                    .filter(e => e.offsetParent !== null)
                    .map(e => e.textContent.trim())
                    .filter(t => t.length > 0 && t.length < 300)""")
                for m in set(msgs[:10]):
                    print(f"  ERR: {m!r}")

        # Inspect tab pane HTML
        pane = page.locator("[role='tabpanel']:visible").first
        if pane.count() > 0:
            html = pane.inner_html()[:3000]
            (OUT / "_dld_tab_pane_transactions.html").write_text(html)

        browser.close()

    print(f"\ncaptured {len(captured)} relevant requests")
    for r in captured[:5]:
        print(f"  {r['method']} {r['url'][:120]}")
        if r['post_data']:
            print(f"    body: {r['post_data'][:200]}")


if __name__ == "__main__":
    main()
