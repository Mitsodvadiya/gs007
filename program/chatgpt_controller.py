"""
ChatGPT Controller - drive chatgpt.com through your real, signed-in Chrome
with Python + Playwright.

It attaches to the Chrome you already have running, opens ChatGPT in the
chosen profile (default: the "Default" folder), and lets you:
  - send text prompts and read the reply (text + any images)
  - upload files / images with a prompt
  - start a new chat, list recent chats, switch between chats
  - save chats under a name and come back to them later

Allow once: a small background server keeps one Playwright connection to
Chrome open, so Chrome's "Allow remote debugging?" prompt appears only when
the server starts - not on every prompt. Code edits are picked up by the
running server without reconnecting.

One-time setup: open chrome://inspect/#remote-debugging in Chrome and turn on
remote debugging.

Usage:
    python chatgpt_controller.py                       # interactive console
    python chatgpt_controller.py "Explain black holes"  # one prompt in a new chat
    python chatgpt_controller.py -f report.pdf "Summarize this"
    python chatgpt_controller.py --chat current "And in short?"   # same chat
    python chatgpt_controller.py --chat work "Continue"            # saved name
    python chatgpt_controller.py --list-chats
    python chatgpt_controller.py --status | --stop
    python chatgpt_controller.py --list-profiles

Output goes to ./responses/<timestamp>_<prompt-slug>/
    response.md   - prompt, chat URL and ChatGPT's text reply
    image_N.png   - any images in the reply
"""

import argparse
import base64
import http.client
import importlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

BASE_DIR = Path(__file__).resolve().parent
CHROME_USER_DATA = Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data"
CHROME_EXE_CANDIDATES = [
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ["LOCALAPPDATA"]) / "Google/Chrome/Application/chrome.exe",
]
DEFAULT_PROFILE = "Default"  # name, email or folder of a Chrome profile
RESPONSES_DIR = BASE_DIR / "responses"
SESSIONS_FILE = BASE_DIR / "sessions.json"  # named chats: {"name": "https://chatgpt.com/c/..."}
SERVER_STATE = BASE_DIR / ".server.json"  # port + auth token of the background server
SERVER_LOG = BASE_DIR / "server.log"
SERVER_START_TIMEOUT_S = 240  # time the user has to click Allow
RESULT_MARKER = "__CHATGPT_CONTROLLER_RESULT__ "
CHATGPT_URL = "https://chatgpt.com/"
REMOTE_DEBUGGING_PAGE = "chrome://inspect/#remote-debugging"

# ChatGPT's DOM changes from time to time - if something breaks, update these.
# Each selector lists the current markup first, then older variants.
PROMPT_SELECTOR = ('form[data-chatgpt-composer] [contenteditable="true"], '
                   '[contenteditable="true"][aria-label="Ask ChatGPT"], #prompt-textarea')
SEND_BUTTON_SELECTOR = ('form button[aria-label^="Send"], button[data-testid="send-button"], '
                        'button#composer-submit-button')
STOP_BUTTON_SELECTOR = 'form button[aria-label^="Stop"], button[data-testid="stop-button"]'
FILE_INPUT_SELECTOR = 'input[type="file"]'
TURN_SELECTOR = "[data-content-search-turn-key], article"  # one user message + its reply
USER_MSG_SELECTOR = '[data-chatgpt-search-unit-key$=":user"], [data-message-author-role="user"]'
ASSISTANT_MSG_SELECTOR = ('[data-chatgpt-search-unit-key$=":assistant"], '
                          '[data-message-author-role="assistant"]')
REPLY_TEXT_SELECTOR = '[class*="MarkdownRoot"], .markdown'
CHAT_LINK_SELECTOR = 'nav a[href*="/c/"]'
POPUP_DISMISS_TEXT = r"^\s*(Not now|No thanks|Dismiss|Got it|Close|Maybe later|Stay logged out)\s*$"

RESPONSE_TIMEOUT_S = 600  # image generation / long thinking can be slow
UPLOAD_TIMEOUT_S = 180
STABLE_FOR_S = 4  # reply is "done" once it stops changing for this long
MIN_IMAGE_SIZE = 128  # ignore icons/avatars smaller than this (px)

# Signed out = a visible "Log in" button.
SIGNED_OUT_JS = r"""() => [...document.querySelectorAll('button, a')]
    .some(e => e.offsetParent && /^\s*Log in\s*$/i.test(e.innerText))"""

# Text + images of the reply to the most recent user message.
LAST_REPLY_JS = f"""() => {{
    const FOLLOWING = Node.DOCUMENT_POSITION_FOLLOWING;
    const users = [...document.querySelectorAll('{USER_MSG_SELECTOR}')];
    const lastUser = users[users.length - 1];
    const after = el => !lastUser || (lastUser.compareDocumentPosition(el) & FOLLOWING);
    const assistant = [...document.querySelectorAll('{ASSISTANT_MSG_SELECTOR}')].filter(after);
    const textOf = el => {{
        const parts = [...el.querySelectorAll('{REPLY_TEXT_SELECTOR}')];
        if (parts.length) return parts.map(p => p.innerText.trim()).join('\\n\\n');
        return el.innerText.replace(/^\\s*ChatGPT said:\\s*/, '');
    }};
    const text = assistant.map(e => textOf(e).trim()).filter(Boolean).join('\\n\\n');
    // Generated images can sit outside the message element, so look at the whole
    // turn (minus the user's own message, which may hold uploaded images).
    const firstTurn = lastUser ? lastUser.closest('{TURN_SELECTOR}') : null;
    let scope = [...document.querySelectorAll('{TURN_SELECTOR}')]
        .filter(t => !firstTurn || t === firstTurn || (firstTurn.compareDocumentPosition(t) & FOLLOWING));
    if (!scope.length) scope = assistant;
    const seen = new Set();
    const images = [];
    const pending = [];  // not loaded yet (a hidden tab doesn't load images)
    let loading = false;
    for (const root of scope) {{
        for (const img of root.querySelectorAll('img')) {{
            if (img.closest('{USER_MSG_SELECTOR}')) continue;
            const src = img.currentSrc || img.src;
            if (!src) continue;
            if (!img.complete) {{ loading = true; if (/estuary|oaiusercontent|files|blob:/.test(src)) pending.push(src); continue; }}
            if (img.naturalWidth < {MIN_IMAGE_SIZE} || img.naturalHeight < {MIN_IMAGE_SIZE}) continue;
            if (seen.has(src)) continue;
            seen.add(src);
            images.push(src);
        }}
    }}
    return {{count: assistant.length, text, images, loading, pending}};
}}"""

MARK_OLD_JS = f"""() => {{
    const users = document.querySelectorAll('{USER_MSG_SELECTOR}');
    users.forEach(u => u.setAttribute('data-cc-old', '1'));
    return users.length;
}}"""

# The last user message is new (not tagged before sending) and starts with our prompt.
NEW_USER_MSG_JS = f"""(start) => {{
    const users = [...document.querySelectorAll('{USER_MSG_SELECTOR}')];
    const last = users[users.length - 1];
    if (!last || last.hasAttribute('data-cc-old')) return false;
    return last.innerText.replace(/\\s+/g, ' ').includes(start.slice(0, 40));
}}"""

FETCH_IMAGE_JS = """async (src) => {
    const res = await fetch(src, {credentials: 'include'});
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const buf = new Uint8Array(await res.arrayBuffer());
    let bin = '';
    for (let i = 0; i < buf.length; i += 0x8000)
        bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
    return {data: btoa(bin), type: res.headers.get('content-type') || ''};
}"""

RECENT_CHATS_JS = f"""() => {{
    const seen = new Set();
    return [...document.querySelectorAll('{CHAT_LINK_SELECTOR}')].map(a => {{
        const m = a.getAttribute('href').match(/\\/c\\/([\\w-]+)/);
        return m && {{id: m[1], title: a.innerText.trim().split('\\n')[0], url: new URL(a.href).href}};
    }}).filter(c => c && !seen.has(c.id) && seen.add(c.id));
}}"""


class ChatGPTError(RuntimeError):
    pass


# ---------------------------------------------------------------- Chrome ----

def list_chrome_profiles() -> list[dict]:
    local_state = json.loads((CHROME_USER_DATA / "Local State").read_text(encoding="utf-8"))
    cache = local_state.get("profile", {}).get("info_cache", {})
    return [
        {"dir": folder, "name": info.get("name", ""), "email": info.get("user_name", "")}
        for folder, info in sorted(cache.items())
    ]


def find_profile(query: str) -> dict:
    q = query.strip().lower()
    for prof in list_chrome_profiles():
        if q in (prof["dir"].lower(), prof["name"].lower(), prof["email"].lower()):
            return prof
    raise ChatGPTError(f"No Chrome profile matches '{query}'. Run with --list-profiles.")


def chrome_exe() -> Path:
    for path in CHROME_EXE_CANDIDATES:
        if path.exists():
            return path
    raise SystemExit("Could not find chrome.exe - is Google Chrome installed?")


def open_in_chrome(profile_dir: str, url: str) -> None:
    """Open a URL in the running Chrome, in the given profile's window."""
    subprocess.Popen([str(chrome_exe()), f"--profile-directory={profile_dir}", url])


def chrome_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
                         capture_output=True, text=True).stdout
    return "chrome.exe" in out


def devtools_endpoint(profile_dir: str, log=print) -> str:
    """Return the ws:// endpoint Chrome publishes once remote debugging is enabled."""
    port_file = CHROME_USER_DATA / "DevToolsActivePort"
    if not chrome_running():
        log("Chrome is not running - starting it with the chosen profile...")
        port_file.unlink(missing_ok=True)  # stale file from the last session
        open_in_chrome(profile_dir, CHATGPT_URL)
        deadline = time.time() + 20
        while time.time() < deadline and not port_file.exists():
            time.sleep(0.5)
    if not port_file.exists():
        log(f"[!] Remote debugging is not enabled in Chrome (one-time setup). Opening "
            f"{REMOTE_DEBUGGING_PAGE} - turn remote debugging ON there.")
        open_in_chrome(profile_dir, REMOTE_DEBUGGING_PAGE)
        while not port_file.exists():
            time.sleep(1)
    port, path = port_file.read_text().split()[:2]
    return f"ws://127.0.0.1:{port}{path}"


class ChromeLink:
    """The one Playwright connection to Chrome, plus a ChatGPT tab per profile.

    Lives for the whole life of the background server, so Chrome's Allow
    prompt only shows when it connects.
    """

    def __init__(self, log=print):
        self.log = log
        self.pw = None
        self.browser = None
        self.pages: dict[str, Page] = {}  # profile folder -> its ChatGPT tab

    def connect(self, profile_dir: str) -> None:
        self.close()
        endpoint = devtools_endpoint(profile_dir, self.log)
        self.log('Connecting to Chrome - click "Allow" in the Chrome window that asks'
                 " (it may be a different profile's window; that's fine)...")
        self.pw = sync_playwright().start()
        try:
            self.browser = self.pw.chromium.connect_over_cdp(endpoint, timeout=SERVER_START_TIMEOUT_S * 1000)
        except Exception as e:
            self.pw.stop()
            self.pw = None
            raise ChatGPTError(f"Could not connect to Chrome ({e}). Check remote debugging is "
                               f"still ON at {REMOTE_DEBUGGING_PAGE} and that you clicked Allow.")
        self.pages.clear()
        self.reset_downloads()
        self._guard_dialogs()
        self.log("Connected to Chrome.")

    def _guard_dialogs(self) -> None:
        """Every tool connects to the same Chrome. If they all auto-close a pop-up dialog in some tab at once, they
        race ("No dialog is showing") and their browser drivers crash. So: a dialog in one of THIS tool's own tabs
        is closed here (safely); a dialog in any other tab is left to its owner / the user."""
        def handler(page):
            def on_dialog(dialog):
                if page in self.pages.values():
                    try:
                        if getattr(page, "_leave_ok", False) and dialog.type == "beforeunload":
                            dialog.accept()
                        else:
                            dialog.dismiss()
                    except Exception:
                        pass
            return on_dialog

        def attach(page):
            try:
                page.on("dialog", handler(page))
            except Exception:
                pass

        for ctx in self.browser.contexts:
            for pg in ctx.pages:
                attach(pg)
            ctx.on("page", attach)

    @property
    def connected(self) -> bool:
        return self.browser is not None and self.browser.is_connected()

    def all_pages(self) -> list[Page]:
        return [p for ctx in self.browser.contexts for p in ctx.pages]

    def pump(self, seconds: float = 0.01) -> None:
        """Let Playwright process pending events (new tabs, closed tabs...).

        The sync API only handles incoming events during a Playwright call,
        so plain time.sleep() would never notice a newly opened tab.
        """
        pages = [p for p in self.all_pages() if not p.is_closed()]
        if pages:
            pages[0].wait_for_timeout(seconds * 1000)
        else:
            time.sleep(seconds)
            self.browser.new_browser_cdp_session().detach()

    def reset_downloads(self) -> None:
        """Put Chrome's download handling back to normal.

        Playwright switches Chrome to saving downloads into a temp folder under
        random names when it connects, and that sticks until Chrome restarts.
        """
        try:
            cdp = self.browser.new_browser_cdp_session()
            contexts = [None]
            try:
                contexts += cdp.send("Target.getBrowserContexts")["browserContextIds"]
            except Exception:
                pass
            for ctx in contexts:
                params = {"behavior": "default"}
                if ctx:
                    params["browserContextId"] = ctx
                try:
                    cdp.send("Browser.setDownloadBehavior", params)
                except Exception:
                    pass
            cdp.detach()
        except Exception:
            pass

    def close(self) -> None:
        """Disconnect only - Chrome and its tabs stay open."""
        if self.pw:
            try:
                if self.connected:
                    self.reset_downloads()
            finally:
                self.pw.stop()
                self.pw = None
                self.browser = None


# ---------------------------------------------------------- Named chats ----

def load_sessions() -> dict:
    try:
        return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_session(name: str, url: str) -> None:
    sessions = load_sessions()
    sessions[name] = url
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2), encoding="utf-8")


def delete_session(name: str) -> bool:
    sessions = load_sessions()
    if sessions.pop(name, None) is None:
        return False
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2), encoding="utf-8")
    return True


# ----------------------------------------------------------- Controller ----

class ChatGPTController:
    """Controls the ChatGPT tab of one Chrome profile over a ChromeLink.

    Usage from other Python code (one Allow per run; the CLI uses the
    background server instead):
        link = ChromeLink()
        link.connect("Default")
        gpt = ChatGPTController(link)
        print(gpt.ask("Hello", files=["notes.pdf"])["text"])
        link.close()
    """

    def __init__(self, link: ChromeLink, profile: str = DEFAULT_PROFILE, log=print):
        self.link = link
        self.log = log
        self.profile = find_profile(profile)
        self.page = self._tab()

    # -- tab -----------------------------------------------------------------

    def _tab(self) -> Page:
        """This profile's ChatGPT tab; opens one if there is none yet."""
        page = self.link.pages.get(self.profile["dir"])
        if page and not page.is_closed():
            for extra in self.link.all_pages():  # one tab per site: close leftover duplicates
                if extra is not page and "chatgpt.com" in (extra.url or "") and not extra.is_closed():
                    try:
                        extra.close()
                    except Exception:
                        pass
            return page
        self.log(f"Opening ChatGPT in profile '{self.profile['name']}' ({self.profile['email']})...")
        # reuse a tab of this site that is already open (after a restart) and close duplicates
        same = [p for p in self.link.all_pages() if "chatgpt.com" in (p.url or "") and not p.is_closed()]
        if same:
            keep = same[0]
            for extra in same[1:]:
                try:
                    extra.close()
                except Exception:
                    pass
            self.link.pages[self.profile["dir"]] = keep
            self.page = keep
            self.log(f"Reusing the open chatgpt.com tab" + (f" (closed {len(same) - 1} extra)" if len(same) > 1 else ""))
            try:
                self.ensure_logged_in()
            except Exception:
                pass
            return keep
        before = {id(p) for p in self.link.all_pages()}
        open_in_chrome(self.profile["dir"], CHATGPT_URL)
        deadline = time.time() + 30
        while time.time() < deadline:
            for page in self.link.all_pages():
                if id(page) not in before and "chatgpt.com" in page.url:
                    self.link.pages[self.profile["dir"]] = page
                    self.page = page
                    self.ensure_logged_in()
                    return page
            self.link.pump(0.5)
        raise ChatGPTError("The ChatGPT tab did not open in Chrome.")

    def _wait_ready(self, timeout: float = 60) -> None:
        self.page.wait_for_selector(PROMPT_SELECTOR, timeout=timeout * 1000)
        self.page.wait_for_timeout(1000)  # let the sidebar render

    def ensure_logged_in(self) -> None:
        try:
            self._wait_ready()
        except Exception:
            pass
        if self.page.locator(PROMPT_SELECTOR).count() == 0 or self.page.evaluate(SIGNED_OUT_JS):
            raise ChatGPTError(
                f"Profile '{self.profile['name']}' is not signed in to ChatGPT (or a Cloudflare "
                "check is showing). Finish it in the ChatGPT tab, then run again.")

    def dismiss_popups(self) -> None:
        """Close promo / tooltip dialogs that ChatGPT sometimes shows."""
        for _ in range(3):
            buttons = self.page.get_by_role("button", name=re.compile(POPUP_DISMISS_TEXT, re.I))
            if buttons.count() == 0:
                return
            try:
                buttons.first.click(timeout=2000)
                self.page.wait_for_timeout(500)
            except Exception:
                return

    # -- chats (sessions) ----------------------------------------------------

    @property
    def chat_id(self) -> str | None:
        m = re.search(r"/c/([\w-]+)", self.page.url)
        return m.group(1) if m else None

    def new_chat(self) -> None:
        self.page.goto(CHATGPT_URL)
        self._wait_ready()

    def recent_chats(self) -> list[dict]:
        """Chats from the sidebar: [{id, title, url}], pinned first, then newest."""
        try:
            self.page.wait_for_selector(CHAT_LINK_SELECTOR, timeout=8000)
        except Exception:
            pass  # sidebar closed or no chats yet
        return self.page.evaluate(RECENT_CHATS_JS)

    def open_chat(self, target: str) -> None:
        """Switch chat: 'new', 'current', saved name, number from recent_chats(), id or URL."""
        target = target.strip()
        sessions = load_sessions()
        if target in ("", "current"):
            return
        if target == "new":
            return self.new_chat()
        if target in sessions:
            url = sessions[target]
        elif target.isdigit():
            chats = self.recent_chats()
            index = int(target) - 1
            if not 0 <= index < len(chats):
                raise ChatGPTError(f"No chat #{target} in the list (have {len(chats)}).")
            url = chats[index]["url"]
        elif target.startswith("http"):
            url = target
        elif re.fullmatch(r"[\w-]{8,}", target):
            url = f"{CHATGPT_URL}c/{target}"
        else:
            raise ChatGPTError(f"Unknown chat '{target}' - use new, current, a saved name, a "
                               "number from the chat list, a chat id or a URL.")
        if self.page.url.split("?")[0].rstrip("/") == url.split("?")[0].rstrip("/"):
            return            # already open - reloading a long chat full of images is slow
        self.page.bring_to_front()   # a background tab loads very slowly (Chrome throttles it)
        self.page.goto(url)
        self._wait_ready(120)

    def current(self) -> dict:
        title = self.page.title()
        if self.chat_id:
            for chat in self.recent_chats():
                if chat["id"] == self.chat_id:
                    title = chat["title"]
        return {"url": self.page.url, "id": self.chat_id, "title": title}

    # -- asking --------------------------------------------------------------

    def _file_input(self):
        """The composer's file input; prefer one that accepts any file type."""
        inputs = self.page.locator(FILE_INPUT_SELECTOR)
        if inputs.count() == 0:
            raise ChatGPTError("Could not find ChatGPT's file upload input.")
        for i in range(inputs.count()):
            accept = (inputs.nth(i).get_attribute("accept") or "").strip()
            if not accept or accept == "*":
                return inputs.nth(i)
        return inputs.first

    def _wait_send_enabled(self, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            send = self.page.locator(SEND_BUTTON_SELECTOR)
            if send.count() and send.first.is_enabled():
                return
            self.page.wait_for_timeout(500)
        raise ChatGPTError("Timed out waiting for the Send button (file upload stuck?).")

    def send(self, prompt: str, files: list[Path] = ()) -> None:
        self.page.bring_to_front()
        self.dismiss_popups()
        for _ in range(3):   # an open image viewer / dialog covers the composer - close it first
            if not self.page.locator('[role="dialog"]').filter(visible=True).count():
                break
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(600)
        if files:
            self.log(f"... uploading {len(files)} file(s)")
            self._file_input().set_input_files([str(f) for f in files])
        box = self.page.locator(PROMPT_SELECTOR).first
        box.click()
        box.fill(prompt)
        self._wait_send_enabled(UPLOAD_TIMEOUT_S if files else 30)
        self.page.locator(SEND_BUTTON_SELECTOR).first.click()

    def _mark_old_messages(self) -> int:
        """Tag the user messages already on the page. Long chats only render their last few turns, so a count
        can stay the same after sending - a new, untagged message is what shows ours arrived."""
        return self.page.evaluate(MARK_OLD_JS)

    def _our_message_shown(self, previous_users: int, prompt: str) -> bool:
        if self.page.locator(USER_MSG_SELECTOR).count() > previous_users:
            return True
        return self.page.evaluate(NEW_USER_MSG_JS, re.sub(r"\s+", " ", prompt).strip()[:60])

    def wait_for_reply(self, previous_users: int, prompt: str = "") -> dict:
        """Wait until ChatGPT has finished answering the message just sent."""
        self.log("... waiting for ChatGPT")
        start = time.time()
        # 1. our message shows up in the chat
        while not self._our_message_shown(previous_users, prompt):
            if time.time() - start > 60:
                raise ChatGPTError("The message did not appear in the chat.")
            self.page.wait_for_timeout(500)
        # 2. the reply stops changing and ChatGPT is no longer generating
        last_key, stable_since, next_note, waiting_img = None, time.time(), start + 30, None
        while True:
            now = time.time()
            if now - start > RESPONSE_TIMEOUT_S:
                raise ChatGPTError(f"No complete reply after {RESPONSE_TIMEOUT_S}s.")
            if now >= next_note:
                self.log(f"... still generating ({int(now - start)}s)")
                next_note += 30
            reply = self.page.evaluate(LAST_REPLY_JS)
            generating = self.page.locator(STOP_BUTTON_SELECTOR).count() > 0
            if not generating and reply["loading"]:
                # finished, but its image hasn't loaded - Chrome doesn't load images in a background tab
                waiting_img = waiting_img or now
                if now - waiting_img > 10:
                    self.page.bring_to_front()
                    self.page.evaluate("() => { const i = [...document.querySelectorAll('img')].pop(); "
                                       "if (i) i.scrollIntoView({block: 'center'}); }")
                if now - waiting_img > 40 and reply["pending"]:   # take it straight from its link
                    reply["images"] = list(dict.fromkeys(reply["images"] + reply["pending"]))
                    reply["loading"] = False
            else:
                waiting_img = None
            busy = generating or reply["loading"]
            if not busy and not reply["text"] and now - start > 8:
                # ChatGPT's own failure notices: no answer will ever come - fail now so another AI takes over
                tail = self.page.evaluate("() => document.body.innerText.slice(-1500)")
                m = re.search(r"This response couldn.t load|Something went wrong|error in message stream|"
                              r"There was an error generating a response|Network error", tail, re.I)
                if m:
                    raise ChatGPTError(f"ChatGPT failed: {m.group(0)}")
            key = (reply["text"], tuple(reply["images"]))
            if key != last_key or busy or not (reply["text"] or reply["images"]):
                last_key, stable_since = key, now
            elif now - stable_since >= STABLE_FOR_S:
                return reply
            self.page.wait_for_timeout(1000)

    def ask(self, prompt: str, files: list[str | Path] = (), chat: str = "current") -> dict:
        """Send a prompt (+ files) and save the reply.

        chat: 'current' (default), 'new', a saved name, chat number, id or URL.
        Returns {"text", "images": [paths], "chat_url", "folder"}.
        """
        files = [check_file(f) for f in files]
        self.open_chat(chat)
        previous_users = self._mark_old_messages()
        self.send(prompt, files)
        reply = self.wait_for_reply(previous_users, prompt)
        folder = make_output_dir(prompt)
        images = self.save_images(reply["images"], folder)
        (folder / "response.md").write_text(
            f"# Prompt\n\n{prompt}\n\n"
            + (f"Files: {', '.join(str(f) for f in files)}\n\n" if files else "")
            + f"Chat: {self.page.url}\n\n# ChatGPT reply\n\n{reply['text']}\n",
            encoding="utf-8")
        return {"text": reply["text"], "images": [str(p) for p in images],
                "chat_url": self.page.url, "folder": str(folder)}

    def save_images(self, sources: list[str], folder: Path) -> list[Path]:
        saved = []
        for i, src in enumerate(sources, 1):
            try:
                if src.startswith("data:"):
                    header, data = src.split(",", 1)
                    blob, mime = base64.b64decode(data), header[5:].split(";")[0]
                else:
                    res = self.page.evaluate(FETCH_IMAGE_JS, src)
                    blob, mime = base64.b64decode(res["data"]), res["type"]
            except Exception as e:
                self.log(f"    could not download image {i}: {e}")
                continue
            path = folder / f"image_{i}{_ext(mime, blob)}"
            path.write_bytes(blob)
            saved.append(path)
        return saved


# ------------------------------------------------------------- Helpers ----

def check_file(path: str | Path) -> Path:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise ChatGPTError(f"File not found: {path}")
    return p


def make_output_dir(prompt: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:40] or "prompt"
    folder = RESPONSES_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{slug}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _ext(mime: str, blob: bytes) -> str:
    mime = mime.lower()
    for key, ext in (("png", ".png"), ("jpeg", ".jpg"), ("jpg", ".jpg"), ("webp", ".webp"),
                     ("gif", ".gif")):
        if key in mime:
            return ext
    if blob.startswith(b"\x89PNG"):
        return ".png"
    if blob.startswith(b"\xff\xd8"):
        return ".jpg"
    if blob[8:12] == b"WEBP":
        return ".webp"
    return ".png"


# ---------------------------------------------------------------- Server ----

def run_server(profile: str) -> None:
    """Hold one Chrome connection and serve commands from the CLI over HTTP."""
    stream = None  # the open client response, while a request runs

    def log(text: str, to_client: bool = True) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}"
        with open(SERVER_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if stream and to_client:
            try:
                stream.write((text + "\n").encode("utf-8"))
                stream.flush()
            except OSError:
                pass

    link = ChromeLink(log)
    link.connect(find_profile(profile)["dir"])
    try:  # open ChatGPT right away in the default profile
        ChatGPTController(link, profile, log)
    except ChatGPTError as e:
        log(f"[!] {e}")

    token = secrets.token_hex(16)
    controller_mtime = [Path(__file__).stat().st_mtime]
    state = {"stop": False}

    def controller_module():
        """The controller code, reloaded when the .py file changed (no new Allow)."""
        mod = sys.modules.get("chatgpt_controller") or importlib.import_module("chatgpt_controller")
        mtime = Path(__file__).stat().st_mtime
        if mtime != controller_mtime[0]:
            controller_mtime[0] = mtime
            mod = importlib.reload(mod)
            log("Reloaded chatgpt_controller.py")
        return mod

    def dispatch(cmd: str, args: dict):
        if cmd == "ping":
            return {"ok": True, "pid": os.getpid()}
        if cmd == "stop":
            state["stop"] = True
            return {"ok": True}
        if not link.connected:
            log("Lost the connection to Chrome - reconnecting...")
            link.connect(find_profile(profile)["dir"])
        mod = controller_module()
        gpt = mod.ChatGPTController(link, args.get("profile") or profile, log)
        if cmd == "ask":
            return gpt.ask(args["prompt"], args.get("files", []), args.get("chat", "current"))
        if cmd == "open":
            gpt.open_chat(args.get("chat", "current"))
            return gpt.current()
        if cmd == "current":
            return gpt.current()
        if cmd == "chats":
            return gpt.recent_chats()
        if cmd == "eval":  # debugging: run JavaScript in the ChatGPT tab
            return gpt.page.evaluate(args["js"])
        raise ChatGPTError(f"Unknown command '{cmd}'")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def do_POST(self):
            nonlocal stream
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
            if body.get("token") != token:
                self.send_response(403)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            stream = self.wfile
            cmd, args = body.get("cmd", ""), body.get("args", {})
            if cmd not in ("ping",):
                log(f"request: {cmd} {json.dumps(args, ensure_ascii=False)[:300]}", to_client=False)
            try:
                result = {"ok": True, "result": dispatch(cmd, args)}
                if cmd not in ("ping",):
                    log(f"done: {cmd} ok", to_client=False)
            except Exception as e:
                msg = str(e) if isinstance(e, ChatGPTError) or type(e).__name__ == "ChatGPTError" \
                    else f"{type(e).__name__}: {e}"
                log(f"done: {cmd} error: {msg}", to_client=False)
                with open(SERVER_LOG, "a", encoding="utf-8") as f:
                    traceback.print_exc(file=f)
                result = {"ok": False, "error": msg}
            stream = None
            try:
                self.wfile.write((RESULT_MARKER + json.dumps(result, ensure_ascii=False) + "\n")
                                 .encode("utf-8"))
            except OSError:
                pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    httpd.timeout = 2
    SERVER_STATE.write_text(json.dumps({"port": httpd.server_address[1], "token": token,
                                        "pid": os.getpid(), "profile": profile}))
    log(f"Server ready on port {httpd.server_address[1]} (pid {os.getpid()})")
    try:
        while not state["stop"]:
            httpd.handle_request()
            if link.connected:
                try:
                    link.pump()
                except Exception:
                    pass
    finally:
        SERVER_STATE.unlink(missing_ok=True)
        link.close()
        log("Server stopped.")


# ---------------------------------------------------------------- Client ----

def _server_state() -> dict | None:
    try:
        return json.loads(SERVER_STATE.read_text())
    except Exception:
        return None


def call(cmd: str, timeout: float = RESPONSE_TIMEOUT_S + 300, quiet: bool = False, **args):
    """Send a command to the background server; prints its progress lines."""
    state = _server_state()
    if not state:
        raise ChatGPTError("The background server is not running.")
    conn = http.client.HTTPConnection("127.0.0.1", state["port"], timeout=timeout)
    try:
        conn.request("POST", "/", json.dumps({"token": state["token"], "cmd": cmd, "args": args}),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise ChatGPTError(f"Server refused the request (HTTP {resp.status}).")
        while line := resp.readline().decode("utf-8"):
            if line.startswith(RESULT_MARKER):
                result = json.loads(line[len(RESULT_MARKER):])
                if not result["ok"]:
                    raise ChatGPTError(result["error"])
                return result["result"]
            if not quiet:
                print(line.rstrip("\n"))
        raise ChatGPTError("The server closed the connection without a result (see server.log).")
    finally:
        conn.close()


def _pid_alive(pid) -> bool:
    """Alive AND running this controller's server. After a restart Windows reuses process ids, so a stale
    .server.json pid can belong to some other program - that must not count as 'our server is running'."""
    try:
        out = subprocess.run(["wmic", "process", "where", f"ProcessId={int(pid)}", "get", "CommandLine", "/value"],
                             capture_output=True, text=True).stdout
    except Exception:
        return False
    return Path(__file__).name in out and "--serve" in out


def server_running() -> bool:
    """Running = its process is alive. A server that is busy with a long task doesn't answer a ping in 5 s,
    and treating that as 'dead' used to start a second server (and a second Chrome tab)."""
    state = _server_state()
    if state and state.get("pid") and _pid_alive(state["pid"]):
        return True
    try:
        call("ping", timeout=5, quiet=True)
        return True
    except (ChatGPTError, OSError):
        return False


def ensure_server(profile: str) -> None:
    if server_running():
        return
    SERVER_STATE.unlink(missing_ok=True)
    print("Starting the ChatGPT Controller background server.")
    print('Click "Allow" in Chrome - needed only once while the server keeps running'
          " (the prompt may appear in a different profile's window; that's fine)...")
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    log = open(SERVER_LOG, "a", encoding="utf-8")
    cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "--serve", "--profile", profile]
    try:
        # Break away from the caller's job object so the server outlives it.
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                cwd=BASE_DIR, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    except OSError:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                cwd=BASE_DIR, creationflags=flags)
    deadline = time.time() + SERVER_START_TIMEOUT_S + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise ChatGPTError("The background server failed to start - see server.log.")
        if _server_state() and server_running():
            print("Connected - ChatGPT is open in Chrome.")
            return
        time.sleep(1)
    raise ChatGPTError("Timed out waiting for Chrome's Allow click (see server.log).")


def stop_server() -> None:
    if not server_running():
        print("Server is not running.")
        return
    call("stop", quiet=True)
    print("Server stopped (Chrome and the ChatGPT tab stay open).")


# ------------------------------------------------------------------- CLI ----

def print_reply(result: dict) -> None:
    print("\n" + "=" * 70)
    print(result["text"] or "(no text)")
    print("=" * 70)
    for img in result["images"]:
        print(f"saved image: {img}")
    print(f"Chat: {result['chat_url']}")
    print(f"Saved to: {result['folder']}")


def print_chats(chats: list[dict]) -> None:
    for i, chat in enumerate(chats, 1):
        print(f"  {i:>3}. {chat['title']}  ({chat['id']})")


HELP = """
Type a message and press Enter to send it to ChatGPT.

Commands:
  /new                  start a new chat
  /chats                list chats from the sidebar (numbered)
  /open <target>        switch chat: number from /chats, saved name, chat id or URL
  /save <name>          save the current chat under a name
  /sessions             list saved chat names     /forget <name>  delete one
  /current              show the current chat
  /file <path> [...]    attach file(s) to your next message (quote paths with spaces)
  /files                show attached files       /clear   drop attached files
  /multi                type a multi-line message, finish with a line: /end
  /help                 show this help            /quit    leave (server + Chrome stay open)
"""


def interactive(profile: str) -> None:
    pending: list[Path] = []
    current = call("current", profile=profile)
    print(f"Chat: {current['url']}")
    print(HELP)
    while True:
        try:
            label = (current.get("title") if current.get("id") else None) or "new chat"
            line = input(f"\n[{label[:40]}] you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        try:
            prompt = line
            if line.startswith("/"):
                cmd, _, arg = line.partition(" ")
                cmd, arg, prompt = cmd.lower(), arg.strip(), None
                if cmd in ("/quit", "/exit", "/q"):
                    break
                elif cmd == "/help":
                    print(HELP)
                elif cmd == "/new":
                    current = call("open", chat="new", profile=profile)
                    print("Started a new chat.")
                elif cmd == "/chats":
                    print_chats(call("chats", profile=profile))
                elif cmd == "/open":
                    if not arg:
                        print("Usage: /open <number | saved name | chat id | URL>")
                        continue
                    current = call("open", chat=arg, profile=profile)
                    print(f"Opened: {current['title']}  ({current['url']})")
                elif cmd == "/save":
                    current = call("current", profile=profile)
                    if not arg:
                        print("Usage: /save <name>")
                    elif not current["id"]:
                        print("Send a message first - a new chat has no URL yet.")
                    else:
                        save_session(arg, current["url"])
                        print(f"Saved '{arg}' -> {current['url']}")
                elif cmd == "/sessions":
                    sessions = load_sessions()
                    if not sessions:
                        print("No saved chats yet (use /save <name>).")
                    for name, url in sessions.items():
                        print(f"  {name}: {url}")
                elif cmd == "/forget":
                    print("Deleted." if delete_session(arg) else f"No saved chat '{arg}'.")
                elif cmd == "/current":
                    current = call("current", profile=profile)
                    print(f"{current['title']}  ({current['url']})")
                elif cmd == "/file":
                    for f in shlex.split(arg, posix=False):
                        pending.append(check_file(f.strip('"')))
                    print("Attached for next message: " + ", ".join(p.name for p in pending))
                elif cmd == "/files":
                    print(", ".join(str(p) for p in pending) or "No files attached.")
                elif cmd == "/clear":
                    pending.clear()
                    print("Attachments cleared.")
                elif cmd == "/multi":
                    print("Enter your message; finish with a line containing only /end")
                    lines = []
                    while (part := input()) != "/end":
                        lines.append(part)
                    prompt = "\n".join(lines).strip() or None
                else:
                    print(f"Unknown command {cmd}. Type /help.")
            if prompt:
                result = call("ask", prompt=prompt, files=[str(p) for p in pending],
                              chat="current", profile=profile)
                pending.clear()
                print_reply(result)
                current = call("current", quiet=True, profile=profile)
        except ChatGPTError as e:
            print(f"[!] {e}")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Control ChatGPT in your real Chrome (text, files, images, chats).")
    parser.add_argument("prompt", nargs="?", help="prompt to send; omit for interactive mode")
    parser.add_argument("-f", "--file", action="append", default=[],
                        help="file to upload with the prompt (repeatable)")
    parser.add_argument("-p", "--profile", default=DEFAULT_PROFILE,
                        help=f"Chrome profile name, email or folder (default: {DEFAULT_PROFILE})")
    parser.add_argument("-c", "--chat", default="new",
                        help="chat for a one-shot prompt: new (default), current, a saved name, "
                             "chat number, id or URL")
    parser.add_argument("--save-as", help="save the chat under this name after replying")
    parser.add_argument("--list-chats", action="store_true", help="list recent chats and exit")
    parser.add_argument("--list-profiles", action="store_true", help="list Chrome profiles")
    parser.add_argument("--sessions", action="store_true", help="list saved chat names")
    parser.add_argument("--status", action="store_true", help="is the background server running?")
    parser.add_argument("--stop", action="store_true", help="stop the background server")
    parser.add_argument("--eval", metavar="JS", help=argparse.SUPPRESS)  # debugging
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    try:
        if args.serve:
            return run_server(args.profile)
        if args.list_profiles:
            for prof in list_chrome_profiles():
                mark = "  <- default" if prof["email"] == DEFAULT_PROFILE else ""
                print(f"{prof['dir']:<12} {prof['name']:<12} {prof['email']}{mark}")
            return
        if args.sessions:
            for name, url in load_sessions().items():
                print(f"{name}: {url}")
            return
        if args.status:
            state = _server_state()
            print(f"Server running (pid {state['pid']})" if server_running() and state
                  else "Server is not running.")
            return
        if args.stop:
            return stop_server()

        find_profile(args.profile)  # fail fast on a typo
        ensure_server(args.profile)
        if args.eval:
            print(json.dumps(call("eval", js=args.eval, profile=args.profile),
                             indent=1, ensure_ascii=False))
        elif args.list_chats:
            print_chats(call("chats", profile=args.profile))
        elif not args.prompt:
            if args.chat != "new":
                call("open", chat=args.chat, profile=args.profile)
            interactive(args.profile)
        else:
            result = call("ask", prompt=args.prompt, files=[str(check_file(f)) for f in args.file],
                          chat=args.chat, profile=args.profile)
            print_reply(result)
            if args.save_as:
                save_session(args.save_as, result["chat_url"])
                print(f"Saved chat as '{args.save_as}'")
    except ChatGPTError as e:
        raise SystemExit(f"[!] {e}")


if __name__ == "__main__":
    main()
