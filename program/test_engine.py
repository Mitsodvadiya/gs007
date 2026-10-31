"""The queue against scripted ChatGPT accounts and a fake panel. Touches neither Chrome nor the real panel.
python test_engine.py"""

import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

CONFIG = json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))

import engine
import panel
from chatgpt_controller import ChatGPTError

WORDS = " ".join(["word"] * 120) + " (P<0.001)."
engine.POLL_EVERY_S = 0
ALL_PROFILES = list("ABCDEF")
engine.list_chrome_profiles = lambda: [{"dir": p, "email": p, "name": p} for p in ALL_PROFILES]
engine.open_in_chrome = lambda *a: None


def doc(name, done=True, text=WORDS):
    chunk = {"topic": "T", "subtopic": "S", "section": name, "reference": "R", "original_text": text}
    return {"file_name": name, "number_of_chunks": 1, "chunks": [chunk], "complete": done}


def reply(*docs):
    return "```json\n" + json.dumps({"documents": list(docs), "complete": True}) + "\n```"


def echo(names):
    return reply(*[doc(n) for n in names])


class FakeTab:
    """Answers each message with brain(names, tab). Replies appear after two looks, like a real chat finishing."""

    def __init__(self, world, profile):
        self.world, self.profile = world, profile
        self.page = SimpleNamespace(is_closed=lambda: False)
        self.chat_id, self.answer, self.count, self.names = None, None, 0, []

    def open_chat(self, chat):
        self.chat_id = None

    def send(self, prompt, files=()):
        if files:
            self.names = [Path(f).name for f in files]
            self.world.sent.append((self.profile, list(self.names)))
        else:
            self.world.retries.append(prompt)
            self.names = [n for n in self.names if n in prompt]
        self.chat_id = "chat"
        self.count += 1
        self.answer = self.world.brain(list(self.names), self)

    def read_state(self):
        if isinstance(self.answer, Exception):
            raise self.answer
        if self.answer == "LIMIT":
            return {"tail": "You've reached your message limit.", "generating": False, "reply": None}
        if self.answer == "ERROR":
            return {"tail": "Something went wrong", "generating": False, "reply": None}
        return {"tail": "", "generating": False, "reply": {"id": f"m{self.count}", "text": self.answer,
                                                           "status": "finished_successfully", "endTurn": True}}


class World:
    def __init__(self, brain, profiles=("A", "B"), per_message=3, auto=True, signed_out=()):
        self.brain, self.sent, self.retries, self.signed_out = brain, [], [], signed_out
        self.uploads, self.filled = [], []
        self.folder = tempfile.TemporaryDirectory()
        base = Path(self.folder.name)
        (base / "prompt.md").write_text("PROMPT", encoding="utf-8")
        link = SimpleNamespace(connected=True, pages={}, pump=lambda s=0: None)
        config = dict(CONFIG, chatgpt_profiles=list(profiles), pdfs_per_message=per_message, panel_profile="A")
        self.engine = engine.Engine(link, config, base / "prompt.md", lambda job: None, make_tab=self.make_tab)
        self.auto, self.base, self.jobs = auto, base, []

        page = SimpleNamespace(is_closed=lambda: False, bring_to_front=lambda: None, close=lambda: None)
        engine.panel = SimpleNamespace(
            open_tab=lambda *a, **k: page, wait_ready=lambda p: None, PanelError=panel.PanelError,
            fill=lambda p, chunks, log: self.filled.append(chunks),
            upload=lambda p, shot, log: self.uploads.append(self.engine.current["upload_name"]))

    def make_tab(self, link, profile, log):
        if profile in self.signed_out:
            raise ChatGPTError(f"Profile '{profile}' is not signed in to ChatGPT.")
        return FakeTab(self, profile)

    def add(self, count):
        for i in range(count):
            name = f"p{len(self.jobs)}.pdf"
            folder = self.base / name
            folder.mkdir()
            (folder / name).write_bytes(b"%PDF")
            job = {"id": name, "name": name, "upload_name": name, "dir": folder, "auto": self.auto, "status": "queued",
                   "step": "", "error": "", "warnings": [], "problems": [], "chunks": [], "decision": None,
                   "solo": False, "tries": 0, "result": None}
            self.jobs.append(job)
            self.engine.pending.append(job)

    def run(self, ticks=400):
        for _ in range(ticks):
            if not self.engine.busy():
                return
            self.engine.tick()

    def statuses(self):
        return [j["status"] for j in self.jobs]


def test_bulk_batches_across_accounts():
    world = World(lambda names, tab: echo(names), profiles=("A", "B", "C", "D", "E", "F"))
    world.add(200)
    world.engine.tick()
    assert sum(len(names) for _, names in world.sent) == 18, "6 accounts x 3 PDFs at once"
    world.run(2000)
    assert world.statuses() == ["done"] * 200 and len(set(world.uploads)) == 200
    assert {p for p, _ in world.sent} == set("ABCDEF")


def test_review_holds_the_panel_but_not_chatgpt():
    world = World(lambda names, tab: echo(names), auto=False)
    world.add(6)
    for _ in range(12):
        world.engine.tick()
    assert sum(s == "review" for s in world.statuses()) == 1 and sum(s == "ready" for s in world.statuses()) == 5
    assert not world.uploads
    world.engine.current["decision"] = "upload"
    world.engine.tick()
    assert len(world.uploads) == 1
    world.engine.tick()     # the next PDF moves into the panel
    world.engine.current["decision"] = "discard"
    world.engine.tick()
    assert len(world.uploads) == 1 and "discarded" in world.statuses()


def test_cut_off_reply_keeps_finished_pdfs():
    def brain(names, tab):
        if tab.count == 1:     # p0 finished, p1 stops halfway, p2 never started
            return reply(doc(names[0]))[:-20] + ',{"file_name": "' + names[1] + '", "chunks": [{"original_text": "half'
        return echo(names)
    world = World(brain, profiles=("A",))
    world.add(3)
    world.run()
    assert world.statuses() == ["done"] * 3
    assert "p0.pdf" not in world.retries[0].split("for these PDFs only")[1], "finished PDFs are not asked for again"


def test_chunk_stopping_mid_sentence_only_retries_that_pdf():
    def brain(names, tab):
        return reply(*[doc(n, text=WORDS if n != "p1.pdf" or tab.count > 1 else "scale (P") for n in names])
    world = World(brain, profiles=("A",))
    world.add(3)
    world.run()
    assert world.statuses() == ["done"] * 3 and len(world.retries) == 1 and tuple(world.sent[0][1])


def test_always_cut_off_batch_falls_back_to_one_pdf_at_a_time():
    world = World(lambda names, tab: "not json" if len(names) > 1 else echo(names), profiles=("A",))
    world.add(3)
    world.run()
    assert world.statuses() == ["done"] * 3
    assert [len(n) for _, n in world.sent] == [3, 1, 1, 1]


def test_pdf_that_never_works_fails_alone():
    bad = "p1.pdf"
    world = World(lambda names, tab: reply(*[doc(n) for n in names if n != bad]), profiles=("A",))
    world.add(3)
    world.run()
    assert world.statuses() == ["done", "failed", "done"]
    assert "nothing was filled" in world.jobs[1]["error"]
    assert [n for _, n in world.sent][-1] == [bad], "it got a try of its own before failing"


def test_refusal_is_quoted():
    world = World(lambda names, tab: "This PDF is scanned and I cannot read it.", profiles=("A",), per_message=1)
    world.add(1)
    world.run()
    assert world.statuses() == ["failed"] and "ChatGPT said: This PDF is scanned" in world.jobs[0]["error"]


def test_usage_limit_rests_account_and_others_continue():
    def brain(names, tab):
        return "LIMIT" if tab.profile == "A" else echo(names)
    world = World(brain)
    world.add(6)
    world.run()
    assert world.statuses() == ["done"] * 6
    assert world.engine.accounts[0].rest_until > time.time()
    assert "Limit" in world.engine.accounts[0].public()["state"]


def test_signed_out_account_is_skipped():
    world = World(lambda names, tab: echo(names), signed_out=("A",))
    world.add(6)
    world.run()
    assert world.statuses() == ["done"] * 6 and {p for p, _ in world.sent} == {"B"}
    assert world.engine.accounts[0].public()["state"] == "Signed out"


def test_auto_finds_logged_in_and_open_profiles_open_first():
    states = {"A": "no", "B": "yes", "C": "open", "D": "no", "E": "yes", "F": "open"}
    original, engine.chatgpt_login_state = engine.chatgpt_login_state, states.get
    try:
        found = [p["dir"] for p in engine.find_chatgpt_profiles([])]
        assert found == ["C", "F", "B", "E"]
        assert [p["dir"] for p in engine.find_chatgpt_profiles(["F", "B"])] == ["C", "E"]
    finally:
        engine.chatgpt_login_state = original


def test_login_state_from_cookie_file():
    with tempfile.TemporaryDirectory() as folder:
        base = Path(folder)
        original, engine.CHROME_USER_DATA = engine.CHROME_USER_DATA, base
        try:
            for name, data in (("in", engine.LOGIN_COOKIE + b" chatgpt.com"), ("out", b"other cookies")):
                (base / name / "Network").mkdir(parents=True)
                (base / name / "Network" / "Cookies").write_bytes(data)
            assert engine.chatgpt_login_state("in") == "yes"
            assert engine.chatgpt_login_state("out") == "no"
            assert engine.chatgpt_login_state("never-opened") == "no"
        finally:
            engine.CHROME_USER_DATA = original


def test_profile_signed_in_between_runs_is_picked_up():
    world = World(lambda names, tab: echo(names), profiles=("A", "B"), signed_out=("B",))
    world.add(6)
    world.run()
    assert {p for p, _ in world.sent} == {"A"}
    world.signed_out = ()
    world.sent.clear()
    world.engine.tick()     # idle between runs
    world.add(6)
    world.run()
    assert {p for p, _ in world.sent} == {"A", "B"}


def test_auto_failing_all_profiles_is_explained():
    world = World(lambda names, tab: echo(names), profiles=())
    world.add(1)
    world.run()
    assert world.statuses() == ["failed"] and "No ChatGPT account" in world.jobs[0]["error"]


def test_no_accounts_signed_in():
    world = World(lambda names, tab: echo(names), signed_out=("A", "B"))
    world.add(4)
    world.run()
    assert world.statuses() == ["failed"] * 4 and "No ChatGPT account" in world.jobs[0]["error"]


def test_chatgpt_error_then_success():
    world = World(lambda names, tab: "ERROR" if tab.count == 1 else echo(names), profiles=("A",))
    world.add(3)
    world.run()
    assert world.statuses() == ["done"] * 3 and len(world.sent) == 2


def test_chatgpt_failing_every_time_gives_up():
    world = World(lambda names, tab: "ERROR", profiles=("A",))
    world.add(3)
    world.run()
    assert world.statuses() == ["failed"] * 3 and "Something went wrong" in world.jobs[0]["error"]


def test_tab_crash_is_contained():
    world = World(lambda names, tab: RuntimeError("Target closed") if tab.count == 1 else echo(names), profiles=("A",))
    world.add(3)
    world.run()
    assert world.statuses() == ["done"] * 3


def test_upload_failure_keeps_chunks_for_retry():
    world = World(lambda names, tab: echo(names), profiles=("A",), per_message=1)
    world.add(2)
    good_upload, refused = engine.panel.upload, []

    def refuse(page, shot, log):
        if world.engine.current["id"] == "p0.pdf" and not refused:
            refused.append(True)
            raise panel.PanelError("HTTP 500")
        good_upload(page, shot, log)
    engine.panel.upload = refuse
    world.run()
    assert world.statuses() == ["failed", "done"] and "HTTP 500" in world.jobs[0]["error"]
    sent_before = len(world.sent)
    world.engine.retry(world.jobs[0])
    world.run()
    assert world.statuses() == ["done", "done"] and len(world.sent) == sent_before, "no new ChatGPT message"


def test_discard_while_waiting():
    world = World(lambda names, tab: echo(names), profiles=("A",), per_message=1)
    world.add(3)
    world.jobs[1]["decision"] = "discard"
    world.run()
    assert all("p1.pdf" not in names for _, names in world.sent)


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("ok", name)
            except Exception as e:
                failed += 1
                print("FAIL", name, type(e).__name__, str(e)[:300])
    sys.exit(1 if failed else 0)
