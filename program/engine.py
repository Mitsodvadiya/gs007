"""
The queue: many ChatGPT accounts turn PDFs into chunks at the same time, and one panel tab uploads them one PDF at a time.

A single thread owns the Playwright connection, so Chrome asks "Allow remote debugging?" only once. It never waits on
one account: each pass sends work to idle accounts, then looks at busy ones. An account's reply is read from ChatGPT's
stored copy, which works in a background tab and keeps text the page drops after "<".
"""

import json
import re
import time
import traceback
from collections import deque
from datetime import datetime

from chatgpt_controller import (CHATGPT_URL, CHROME_USER_DATA, STOP_BUTTON_SELECTOR, ChatGPTController, ChatGPTError,
                                list_chrome_profiles, open_in_chrome)

import chunks as chunk_parser
import panel

MAX_ATTEMPTS = 3            # replies asked for, per batch, in one chat
MAX_TRIES = 3               # ChatGPT failures a PDF can take before it is given up on
REPLY_TIMEOUT_S = 900       # three long PDFs in one message can be slow
START_TIMEOUT_S = 60        # the chat must exist this soon after sending
POLL_EVERY_S = 10           # per account
LIMIT_REST_S = 60 * 60      # how long an account rests after a usage limit
LOGIN_COOKIE = b"__Secure-next-auth.session-token"
RETRY_PROMPT = ("Your reply could not be used:\n{problems}\n\nSend the JSON again from the start for these PDFs only: "
                "{names}. Write every chunk in full, inside one ```json code block, with \"complete\": true at the end "
                "of each document and at the end of the JSON.")
PAGE_ERROR = re.compile(r"This response couldn.t load|Something went wrong|error in message stream|"
                        r"There was an error generating a response|Network error", re.I)
# The token stays inside the page.
STATE_JS = """async ([id, stopSelector]) => {
    const state = {tail: document.body.innerText.slice(-1500), generating: !!document.querySelector(stopSelector), reply: null};
    if (!id) return state;
    if (!window.__chunkToken) window.__chunkToken = (await (await fetch('/api/auth/session')).json()).accessToken;
    const res = await fetch('/backend-api/conversation/' + id, {headers: {Authorization: 'Bearer ' + window.__chunkToken}});
    if (res.status === 401) window.__chunkToken = null;
    if (!res.ok) return state;
    const chat = await res.json();
    for (let node = chat.mapping[chat.current_node]; node; node = chat.mapping[node.parent]) {
        const msg = node.message;
        if (msg?.author?.role === 'user') break;
        if (msg?.author?.role === 'assistant' && msg.content?.content_type === 'text') {
            state.reply = {id: msg.id, text: msg.content.parts.join(''), status: msg.status, endTurn: msg.end_turn};
            break;
        }
    }
    return state;
}"""


def chatgpt_login_state(profile_dir: str) -> str:
    """'yes', 'no', or 'open'. Only the cookie's name is looked for, never its value. Chrome locks this file while a
    profile is open, so an open profile can't be judged here; its ChatGPT tab shows the truth when it is first used."""
    try:
        cookies = (CHROME_USER_DATA / profile_dir / "Network" / "Cookies").read_bytes()
    except PermissionError:
        return "open"
    except OSError:
        return "no"
    return "yes" if LOGIN_COOKIE in cookies and b"chatgpt.com" in cookies else "no"


def find_chatgpt_profiles(skip: list[str]) -> list[dict]:
    """Chrome profiles that may have ChatGPT signed in, open ones first."""
    found = {"open": [], "yes": []}
    for profile in list_chrome_profiles():
        state = chatgpt_login_state(profile["dir"])
        if state in found and not {profile["dir"], profile["name"], profile["email"]} & set(skip):
            found[state].append(profile)
    return found["open"] + found["yes"]


class ChatTab(ChatGPTController):
    """The controller in a tab of its own. Its normal _tab() reuses and closes ChatGPT tabs that are already open,
    which would take over a chat someone is reading."""

    def _tab(self):
        self.page = self.link.pages.get(self.profile["dir"])
        if not self.page or self.page.is_closed():
            self.page = panel.open_tab(self.link, open_in_chrome, self.profile["dir"], CHATGPT_URL, "chatgpt.com")
            self.link.pages[self.profile["dir"]] = self.page
        self.ensure_logged_in()
        return self.page

    def read_state(self) -> dict:
        return self.page.evaluate(STATE_JS, [self.chat_id, STOP_BUTTON_SELECTOR])


class Account:
    """One Chrome profile's ChatGPT."""

    def __init__(self, profile_dir: str, name: str):
        self.dir, self.name = profile_dir, name
        self.tab = None
        self.jobs: list[dict] = []
        self.signed_out = False
        self.rest_until = 0.0
        self.attempt = self.batch_size = 0
        self.sent_at = self.next_poll = 0.0
        self.old_reply = self.seen_text = None

    @property
    def available(self) -> bool:
        return not self.jobs and not self.signed_out and time.time() >= self.rest_until

    def public(self) -> dict:
        if self.signed_out:
            state = "Signed out"
        elif time.time() < self.rest_until:
            state = f"Limit, back at {datetime.fromtimestamp(self.rest_until):%H:%M}"
        elif self.jobs:
            state = f"Working on {len(self.jobs)}"
        else:
            state = "Idle"
        return {"name": self.name, "state": state}


class Engine:
    def __init__(self, link, config: dict, prompt_file, save, make_tab=ChatTab):
        self.link, self.config, self.prompt_file, self.save, self.make_tab = link, config, prompt_file, save, make_tab
        self.pending: deque = deque()
        self.ready: deque = deque()
        self.current = None     # the job in the panel tab
        self.page = None
        self.notice = ""
        self.accounts: list[Account] = []
        self.working = False
        self.refresh_accounts()

    def refresh_accounts(self) -> None:
        """Find the signed-in profiles. Runs at start and before each new run of work, so a profile signed in while the
        program is open gets used without a restart. Signed-out accounts get checked again."""
        chosen = self.config["chatgpt_profiles"]
        if chosen == "auto":
            profiles = find_chatgpt_profiles(self.config.get("skip_profiles", []))
        else:
            profiles = [p for p in list_chrome_profiles() if p["dir"] in chosen]
        known = {account.dir: account for account in self.accounts}
        for account in self.accounts:
            account.signed_out = False
        for profile in profiles:
            if profile["dir"] not in known:
                self.accounts.append(Account(profile["dir"], profile["email"] or profile["name"]))
                self.log(f"Found ChatGPT profile: {self.accounts[-1].name}")

    def log(self, text: str) -> None:
        print(f"{datetime.now():%H:%M:%S} {text}", flush=True)

    def stamp(self, job: dict, text: str, status: str | None = None) -> None:
        if status:
            job["status"] = status
        job["step"] = text
        self.log(f"[{job['name']}] {text}")
        with open(job["dir"] / "job.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
        self.save(job)

    def fail(self, job: dict, error: str) -> None:
        job["error"] = error
        self.stamp(job, "Stopped.", "failed")

    # ------------------------------------------------------------ main loop ----

    def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                self.log(traceback.format_exc())
                time.sleep(1)

    def busy(self) -> bool:
        return bool(self.pending or self.ready or self.current or any(a.jobs for a in self.accounts))

    def tick(self) -> None:
        if not self.busy():
            self.working = False
            if self.link.connected:
                self.link.pump(0.5)
            else:
                time.sleep(0.5)
            return
        if not self.working:
            self.working = True
            self.refresh_accounts()
        if not self.link.connected and not self.connect():
            return
        for account in self.accounts:
            if self.pending and account.available:
                self.start(account)
        for account in self.accounts:
            if account.jobs and time.time() >= account.next_poll:
                self.poll(account)
        self.panel_step()
        if self.pending and all(a.signed_out for a in self.accounts):
            while self.pending:
                self.fail(self.pending.popleft(), "No ChatGPT account is signed in. Sign in, then restart the program.")
        self.link.pump(0.3)

    def connect(self) -> bool:
        self.notice = 'Click "Allow" in Chrome...'
        self.log(self.notice)
        try:
            self.link.connect(self.config["panel_profile"])
        except Exception as e:
            self.notice = ""
            for account in self.accounts:
                self.requeue(account)
            error = "Chrome refused the connection. Press Try again, then click Allow when Chrome asks." \
                if "403 Forbidden" in str(e) else str(e).split("Call log")[0].strip()
            while self.pending:
                self.fail(self.pending.popleft(), error)
            return False
        self.notice = ""
        for account in self.accounts:    # tabs from an old connection are gone
            account.tab = None
            self.requeue(account)
        return True

    def requeue(self, account: Account) -> None:
        for job in reversed(account.jobs):
            job["status"] = "queued"
            self.pending.appendleft(job)
        account.jobs = []

    # ------------------------------------------------------------- ChatGPT ----

    def next_batch(self) -> list[dict]:
        batch: list[dict] = []
        while self.pending and len(batch) < self.config["pdfs_per_message"]:
            if self.pending[0]["decision"] == "discard":
                self.pending.popleft()
            elif self.pending[0]["solo"] and batch:
                break
            else:
                batch.append(self.pending.popleft())
                if batch[0]["solo"]:
                    break
        return batch

    def start(self, account: Account) -> None:
        batch = self.next_batch()
        if not batch:
            return
        account.jobs, account.attempt, account.batch_size = batch, 0, len(batch)
        try:
            if not account.tab:
                account.tab = self.make_tab(self.link, account.dir, self.log)
            for job in batch:
                self.stamp(job, f"Sent to ChatGPT ({account.name}), {len(batch)} in this message", "chatgpt")
            names =[job["upload_name"] for job in batch]
            text = self.prompt_file.read_text(encoding="utf-8").strip() + "\n\nAttached PDFs:\n" + \
                "\n".join(f"- {name}" for name in names)
            account.tab.open_chat("new")
            account.tab.send(text, [job["dir"] / job["upload_name"] for job in batch])
            self.mark_sent(account, None)
        except ChatGPTError as e:
            if "not signed in" in str(e):
                self.log(f"{account.name} is signed out, skipping it: {e}")
                account.signed_out = True
                self.requeue(account)
                self.close_tab(account)
            else:
                self.account_failed(account, str(e))
        except Exception as e:
            self.account_failed(account, f"{type(e).__name__}: {e}")

    def close_tab(self, account: Account) -> None:
        page = self.link.pages.pop(account.dir, None)
        try:
            if page:
                page.close()
        except Exception:
            pass

    def mark_sent(self, account: Account, old_reply) -> None:
        account.sent_at, account.old_reply, account.seen_text = time.time(), old_reply, None
        account.next_poll = time.time() + POLL_EVERY_S

    def poll(self, account: Account) -> None:
        account.next_poll = time.time() + POLL_EVERY_S
        waited = time.time() - account.sent_at
        try:
            state = account.tab.read_state()
        except Exception as e:
            return self.account_failed(account, f"{type(e).__name__}: {e}")
        reply = state["reply"]
        fresh = reply and reply["id"] != account.old_reply and reply["status"] == "finished_successfully" \
            and reply["endTurn"] is not False
        if fresh and not state["generating"]:
            if reply["text"] == account.seen_text:     # the same on two looks = really finished
                return self.handle_reply(account, reply)
            account.seen_text = reply["text"]
            return
        if chunk_parser.USAGE_LIMIT.search(state["tail"]):
            account.rest_until = time.time() + LIMIT_REST_S
            self.log(f"{account.name} reached its usage limit, resting it for {LIMIT_REST_S // 60} minutes")
            for job in account.jobs:
                job["step"] = f"{account.name} reached its limit, waiting for another account"
            return self.requeue(account)
        if not state["generating"] and PAGE_ERROR.search(state["tail"]):
            return self.account_failed(account, f"ChatGPT failed: {PAGE_ERROR.search(state['tail']).group(0)}")
        if not account.tab.chat_id and waited > START_TIMEOUT_S:
            return self.account_failed(account, "The message did not reach ChatGPT.")
        if waited > REPLY_TIMEOUT_S:
            self.account_failed(account, f"No complete reply after {REPLY_TIMEOUT_S // 60} minutes.")

    def handle_reply(self, account: Account, reply: dict) -> None:
        names = [job["upload_name"] for job in account.jobs]
        results = chunk_parser.parse_batch(reply["text"], names)
        waiting = []
        for job in account.jobs:
            result = results[job["upload_name"]]
            (job["dir"] / f"reply_{account.attempt}.md").write_text(reply["text"] + "\n", encoding="utf-8")
            (job["dir"] / "chunks.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            job["warnings"], job["problems"] = result["warnings"], result["problems"]
            job["chunks"] = [{"section": c["section"], "words": c["words"]} for c in result["chunks"]]
            if result["problems"]:
                waiting.append(job)
            else:
                job["result"] = result
                self.stamp(job, f"{len(result['chunks'])} chunks ready, waiting for the panel", "ready")
                self.ready.append(job)
        account.jobs = waiting
        if not waiting:
            return
        account.attempt += 1
        if account.attempt < MAX_ATTEMPTS:
            return self.ask_again(account, reply)
        said = "" if '"chunks"' in reply["text"] else f" ChatGPT said: {reply['text'][:200]}"
        for job in reversed(waiting):
            if account.batch_size > 1 and not job["solo"]:     # smaller replies are less likely to be cut off
                job["solo"] = True
                self.stamp(job, "Reply incomplete, trying this PDF alone", "queued")
                self.pending.appendleft(job)
            else:
                self.fail(job, f"ChatGPT's reply stayed incomplete, so nothing was filled: "
                               f"{' '.join(job['problems'][:3])}{said}")
        account.jobs = []

    def ask_again(self, account: Account, reply: dict) -> None:
        problems = [f"{job['upload_name']}: {job['problems'][0]}" for job in account.jobs]
        for job in account.jobs:
            self.stamp(job, f"Reply incomplete, asking ChatGPT again ({account.attempt + 1} of {MAX_ATTEMPTS})")
        names = ", ".join(job["upload_name"] for job in account.jobs)
        try:
            account.tab.send(RETRY_PROMPT.format(problems="\n".join(problems), names=names))
            self.mark_sent(account, reply["id"])
        except Exception as e:
            self.account_failed(account, f"{type(e).__name__}: {e}")

    def account_failed(self, account: Account, error: str) -> None:
        """A failed chat says nothing about the PDFs, so they go back in the queue a limited number of times."""
        self.log(f"{account.name}: {error}")
        for job in reversed(account.jobs):
            job["tries"] += 1
            if job["tries"] >= MAX_TRIES:
                self.fail(job, error)
            else:
                self.stamp(job, f"ChatGPT failed, trying again ({job['tries'] + 1} of {MAX_TRIES}): {error}", "queued")
                self.pending.appendleft(job)
        account.jobs = []
        if account.tab and account.tab.page.is_closed():
            account.tab = None

    # --------------------------------------------------------------- panel ----

    def panel_step(self) -> None:
        job = self.current
        if job is None:
            while self.ready and self.current is None:
                job = self.ready.popleft()
                if job["decision"] != "discard":
                    self.current = job
            if self.current is None:
                return
            job = self.current
            try:
                self.fill(job)
            except Exception as e:
                return self.panel_failed(job, e)
        if job["status"] == "review" and not job["decision"]:
            if self.page.is_closed():
                self.panel_failed(job, panel.PanelError("The panel tab was closed before uploading."))
            return
        if job["decision"] == "discard":
            self.stamp(job, "Discarded. Nothing was uploaded.", "discarded")
            self.close_panel()
            return
        try:
            self.stamp(job, "Uploading...", "uploading")
            panel.upload(self.page, job["dir"] / "after_upload.png", lambda s: self.stamp(job, s))
            self.stamp(job, f"Uploaded {len(job['result']['chunks'])} chunks.", "done")
            self.close_panel()
        except Exception as e:
            self.panel_failed(job, e)

    def fill(self, job: dict) -> None:
        result = job["result"]
        self.stamp(job, f"Filling {len(result['chunks'])} chunks into the panel...", "filling")
        self.page = panel.open_tab(self.link, open_in_chrome, self.config["panel_profile"], self.config["panel_url"],
                                   "gastroagi.com")
        self.link.pages["panel"] = self.page
        panel.wait_ready(self.page)
        panel.fill(self.page, result["chunks"], lambda s: self.stamp(job, s))
        if job["auto"] and not result["warnings"]:
            return
        self.page.bring_to_front()
        self.stamp(job, "Filled. Check the panel tab, then choose Upload now or Discard here.", "review")

    def panel_failed(self, job: dict, error: Exception) -> None:
        self.fail(job, str(error))
        self.current = None
        self.link.pages.pop("panel", None)

    def close_panel(self) -> None:
        try:
            self.page.close()
        except Exception:
            pass
        self.current = None
        self.link.pages.pop("panel", None)

    def retry(self, job: dict) -> None:
        job.update(error="", decision=None, tries=0)
        if job.get("result"):
            job["status"] = "ready"
            self.ready.append(job)
        else:
            job["status"] = "queued"
            self.pending.append(job)
