"""
Drives admin.gastroagi.com/upload-documents in the real, logged-in Chrome.

The panel's fields have no ids or names. Each chunk repeats the same five fields in page order, so chunk i is the
i-th match of each placeholder. "Add More Chunks" stays disabled until the last chunk's Original Text is filled.
If the panel changes, update the constants below.
"""

import time
from pathlib import Path

from playwright.sync_api import Page

FIELD_SELECTORS = {
    "topic": 'input[placeholder="Medicine"]',
    "subtopic": 'input[placeholder="Gastroenterology"]',
    "section": 'input[placeholder="Guidance"]',
    "reference": 'input[placeholder="Book xyz"]',
    "text": 'textarea[placeholder="Paste your text here..."]',
}
LOAD_TIMEOUT_MS = 30_000
UPLOAD_TIMEOUT_S = 180     # the server may embed every chunk before it answers
NO_REQUEST_TIMEOUT_S = 20  # Upload was clicked but nothing went out
QUIET_S = 4                # all requests answered for this long = finished
ERROR_TEXT = ("failed", "error", "invalid", "required", "unauthorized")


class PanelError(RuntimeError):
    pass


def open_tab(link, open_in_chrome, profile_dir: str, url: str, host: str, timeout: float = 30) -> Page:
    """A new tab of this Chrome profile. Tabs already open are left alone."""
    before = {id(p) for p in link.all_pages()}
    open_in_chrome(profile_dir, url)
    deadline = time.time() + timeout
    while time.time() < deadline:
        for page in link.all_pages():
            if id(page) not in before and host in (page.url or ""):
                return page
        link.pump(0.3)
    raise PanelError(f"The {host} tab did not open in Chrome.")


def wait_ready(page: Page) -> None:
    page.wait_for_load_state("domcontentloaded", timeout=LOAD_TIMEOUT_MS)
    try:
        page.wait_for_selector(f'{FIELD_SELECTORS["text"]}, input[type="password"]', timeout=LOAD_TIMEOUT_MS)
    except Exception:
        raise PanelError(f"The upload form did not appear at {page.url}. The panel may have changed.")
    if "/login" in page.url or page.locator('input[type="password"]').count():
        raise PanelError("The panel is signed out in this Chrome profile. Sign in there, then run the PDF again.")


def fill(page: Page, chunks: list[dict], log=print) -> None:
    """Fill every chunk, then read the page back. Raises if what is on the page differs from the chunks."""
    add_more = page.get_by_role("button", name="Add More Chunks")
    for i, chunk in enumerate(chunks):
        if i:
            add_more.click(timeout=10_000)
            page.locator(FIELD_SELECTORS["text"]).nth(i).wait_for(timeout=10_000)
        for key, selector in FIELD_SELECTORS.items():
            page.locator(selector).nth(i).fill(chunk[key])
        log(f"Filled chunk {i + 1} of {len(chunks)}: {chunk['section'] or '(no section)'}")

    on_page = page.locator(FIELD_SELECTORS["text"]).count()
    if on_page != len(chunks):
        raise PanelError(f"The panel shows {on_page} chunks, expected {len(chunks)}.")
    for i, chunk in enumerate(chunks):
        for key, selector in FIELD_SELECTORS.items():
            if page.locator(selector).nth(i).input_value().strip() != chunk[key].strip():
                raise PanelError(f"Chunk {i + 1}: the {key} on the page does not match.")


def upload(page: Page, screenshot: Path | None = None, log=print) -> list[dict]:
    """Click Upload and wait until the panel's requests finish. Returns them; raises if any failed."""
    sent, answered, opened = [], [], [0]

    def on_request(req):
        if req.method in ("POST", "PUT", "PATCH"):
            opened[0] += 1
            sent.append(req)

    def on_response(res):
        if res.request in sent:
            answered.append({"status": res.status, "url": res.url.split("?")[0]})

    def on_failed(req):
        if req in sent:
            answered.append({"status": 0, "url": req.url.split("?")[0]})

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfailed", on_failed)
    page.get_by_role("button", name="Upload", exact=True).click(timeout=10_000)
    log("Clicked Upload, waiting for the panel...")

    start = last_change = time.time()
    seen = 0
    while True:
        now = time.time()
        if len(answered) != seen or opened[0] != len(answered):
            seen, last_change = len(answered), now
        if not sent and now - start > NO_REQUEST_TIMEOUT_S:
            _shot(page, screenshot)
            raise PanelError("Upload was clicked but the panel sent nothing. Check the tab for a message.")
        if sent and now - last_change >= QUIET_S:
            break
        if now - start > UPLOAD_TIMEOUT_S:
            _shot(page, screenshot)
            raise PanelError(f"No answer from the panel after {UPLOAD_TIMEOUT_S}s. Check the tab before retrying.")
        page.wait_for_timeout(300)

    _shot(page, screenshot)
    bad = [a for a in answered if not 200 <= a["status"] < 300]
    if bad:
        raise PanelError(f"The panel refused the upload: HTTP {bad[0]['status']} from {bad[0]['url']}")
    message = _toast_text(page)
    if any(word in message.lower() for word in ERROR_TEXT):
        raise PanelError(f"The panel reported: {message[:200]}")
    return answered


def _toast_text(page: Page) -> str:
    return page.evaluate("""() => [...document.querySelectorAll(
        '[role="alert"], [role="status"], [class*="toast" i], [data-sonner-toast]')]
        .map(e => e.innerText.trim()).filter(Boolean).join(' | ')""")


def _shot(page: Page, path: Path | None) -> None:
    if path:
        try:
            page.screenshot(path=str(path), full_page=True)
        except Exception:
            pass
