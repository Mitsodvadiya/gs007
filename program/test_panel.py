"""
panel.py against a local copy of the form, in a throwaway browser. Never touches the real panel.
python test_panel.py
"""

import json

from playwright.sync_api import sync_playwright

import panel

FORM = """<!doctype html><title>t</title><form id=f onsubmit="return false"><div id=list></div>
<button type=button id=add disabled>Add More Chunks</button><button type=button>Cancel</button>
<button type=button id=up>Upload</button></form><script>
const row = () => { const d = document.createElement('div');
  d.innerHTML = `<input placeholder="Medicine"><input placeholder="Gastroenterology"><input placeholder="Guidance">
    <input placeholder="Book xyz"><textarea placeholder="Paste your text here..."></textarea>`;
  list.append(d); check(); };
const check = () => { const t = [...document.querySelectorAll('textarea')]; add.disabled = !t.at(-1).value; };
document.addEventListener('input', check);
add.onclick = row; row();
up.onclick = async () => {
  const items = [...document.querySelectorAll('textarea')].map(t => t.value);
  const r = await fetch('/api/chunks', {method:'POST', body: JSON.stringify(items)});
  if (window.TOAST) { const n = document.createElement('div'); n.setAttribute('role','alert'); n.textContent = TOAST; document.body.append(n); }
};
</script>"""

CHUNKS = [{"topic": f"T{i}", "subtopic": "S", "section": f"Sec {i}", "reference": "R",
           "text": f"Body {i}\n\nSecond paragraph – “quoted”."} for i in range(3)]


def scenario(browser, status=200, toast=None, respond=True):
    page = browser.new_page()
    received = []

    def handle(route):
        if route.request.url.endswith("/form"):
            return route.fulfill(content_type="text/html", body=FORM)
        received.append(json.loads(route.request.post_data))
        if respond:
            route.fulfill(status=status, body="{}")

    page.route("http://panel.test/**", handle)
    page.goto("http://panel.test/form")
    if toast:
        page.evaluate("t => { window.TOAST = t }", toast)
    panel.wait_ready(page)
    panel.fill(page, CHUNKS, lambda s: None)
    return page, received


def expect_error(fn, text):
    try:
        fn()
    except panel.PanelError as e:
        assert text in str(e), str(e)
        return
    raise AssertionError(f"expected an error with {text!r}")


def main():
    panel.NO_REQUEST_TIMEOUT_S, panel.QUIET_S = 2, 1
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)

        page, received = scenario(browser)
        assert page.locator("textarea").count() == 3
        assert panel.upload(page, None, lambda s: None)[0]["status"] == 200
        assert received == [[c["text"] for c in CHUNKS]]
        print("ok fill + upload")

        page, _ = scenario(browser, status=500)
        expect_error(lambda: panel.upload(page, None, lambda s: None), "HTTP 500")
        print("ok server error is reported")

        page, _ = scenario(browser, toast="Upload failed: bad token")
        expect_error(lambda: panel.upload(page, None, lambda s: None), "bad token")
        print("ok error toast is reported")

        page, _ = scenario(browser)
        page.evaluate("document.getElementById('up').onclick = null")
        expect_error(lambda: panel.upload(page, None, lambda s: None), "sent nothing")
        print("ok no request is reported")

        browser.close()


if __name__ == "__main__":
    main()
