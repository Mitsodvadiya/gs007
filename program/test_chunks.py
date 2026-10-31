"""python -m pytest test_chunks.py  (or python test_chunks.py)"""

import json
from pathlib import Path

import chunks

TESTDATA = Path(__file__).parent / "testdata"
WORDS =" ".join(["word"] * 120) + " (P<0.001)."


def reply(**changes) -> dict:
    item = {"topic": "Pancreas Divisum", "subtopic": "Recurrent Acute Pancreatitis", "section": "One",
            "reference": "JAMA. 2026;335(8):682–692.", "original_text": WORDS}
    data = {"topic": "Pancreas Divisum", "subtopic": "Recurrent Acute Pancreatitis",
            "reference": "JAMA. 2026;335(8):682–692.", "number_of_chunks": 2,
            "chunks": [item, {**item, "section": "Two"}], "complete": True}
    return {**data, **changes}


def as_page(data: dict) -> str:
    """What the page gives us for a code block: a language label, then the code."""
    return "JSON\n" + json.dumps(data, indent=2, ensure_ascii=False)


def test_complete_reply():
    out = chunks.parse(as_page(reply()))
    assert out["problems"] == [] and out["warnings"] == []
    assert [c["section"] for c in out["chunks"]] == ["One", "Two"]
    assert out["chunks"][0]["text"].endswith("(P<0.001).")
    assert out["chunks"][0]["reference"].startswith("JAMA. 2026;335(8)")


def test_text_around_the_json_and_paragraphs():
    data = reply()
    data["chunks"][0]["original_text"] = f"{WORDS}\n\nA second paragraph “quoted” with a brace }} too."
    out = chunks.parse("Here it is:\n" + as_page(data) + "\n\nLet me know if you want changes.")
    assert out["problems"] == []
    assert "\n\n" in out["chunks"][0]["text"] and out["chunks"][0]["text"].endswith("too.")


def test_shared_fields_fill_gaps():
    data = reply()
    del data["chunks"][0]["topic"], data["chunks"][0]["reference"]
    chunk = chunks.parse(as_page(data))["chunks"][0]
    assert chunk["topic"] == "Pancreas Divisum" and chunk["reference"].startswith("JAMA")


def test_cut_off_json_is_rejected():
    text = as_page(reply())
    for size in range(len(text) // 4, len(text) - 2, 97):  # wherever the reply stops, never a usable result
        assert chunks.parse(text[:size])["problems"], size
    assert "cut off" in chunks.parse(text[:len(text) // 2])["problems"][0]


def test_chunk_stopping_at_p_is_rejected():
    """The real failure: ChatGPT's page dropped everything after "(P" in "P<0.001"."""
    data = reply()
    data["chunks"][1]["original_text"] = "chest-pain scores were 23 versus 0 on a 100-point visual analogue scale (P"
    problems = " | ".join(chunks.parse(as_page(data))["problems"])
    assert "Chunk 2" in problems and "middle of a sentence" in problems


def test_old_text_format_is_rejected():
    out = chunks.parse("Chunk 1\nTopic: A\nSection: B\nOriginal Text: scale (P")
    assert out["chunks"] == [] and out["problems"] == ["The reply has no JSON."]


def test_incomplete_flags_are_rejected():
    assert any("complete" in p for p in chunks.parse(as_page(reply(complete=False)))["problems"])
    assert any("number_of_chunks" in p for p in chunks.parse(as_page(reply(number_of_chunks=3)))["problems"])
    assert any("no chunks" in p for p in chunks.parse(as_page(reply(chunks=[], number_of_chunks=0)))["problems"])


def test_warnings():
    data = reply()
    data["chunks"][0].update(section="Two", reference="", original_text="Too short.")
    data["chunks"][1]["subtopic"] = ""
    data["reference"] = data["subtopic"] = ""
    data["chunks"][1]["topic"] = "[single preferred Topic]"
    warnings = " | ".join(chunks.parse(as_page(data))["warnings"])
    for expected in ("2 words", "placeholder", "repeats the Section", "reference is empty", "subtopic is empty"):
        assert expected in warnings, expected


def test_citation_markers_are_removed():
    for marker in ("【4:2†source】", "citeturn0file0", "citeturn0file0"):
        data = reply()
        data["chunks"][0]["original_text"] = f"{WORDS}{marker}"
        text = chunks.parse(as_page(data))["chunks"][0]["text"]
        assert text == WORDS, marker


def test_sample_complete_reply():
    out = chunks.parse((TESTDATA / "sample_complete_reply.txt").read_text(encoding="utf-8"))
    assert out["problems"] == [] and out["warnings"] == []
    assert len(out["chunks"]) == 7
    assert sum(c["text"].replace(" ", "").count("P<") for c in out["chunks"]) == 5, "no P value may be lost"


def test_sample_cut_off_reply_is_rejected():
    out = chunks.parse((TESTDATA / "sample_cut_off_reply.txt").read_text(encoding="utf-8"))
    assert out["chunks"] == [] and out["problems"]


def test_trailing_commas_and_key_spelling():
    data = reply()
    data["chunks"] = [{"Topic": "A", "Sub-Topic": "x", "Section": "One", "Reference": "R", "Original Text": WORDS}]
    data["number_of_chunks"] = "1"
    data["complete"] = "true"
    out = chunks.parse(as_page(data).replace('"complete": "true"', '"complete": "true",'))
    assert out["problems"] == [], out["problems"]
    assert out["chunks"][0]["topic"] == "A" and out["chunks"][0]["text"] == WORDS


def test_two_part_reply_is_merged():
    one, two = reply(number_of_chunks=4), reply(number_of_chunks=4)
    one.pop("complete")
    out = chunks.parse("Part 1:\n" + as_page(one) + "\n\nPart 2:\n" + as_page(two))
    assert len(out["chunks"]) == 4 and out["problems"] == []


def test_braces_before_the_json():
    assert chunks.parse("Using {this} format: {x}\n" + as_page(reply()))["problems"] == []


def test_html_entities_and_paragraph_lists():
    data = reply()
    data["chunks"][0]["original_text"] = f"{WORDS} Scores differed (P&lt;0.05)."
    data["chunks"][1]["original_text"] = [WORDS, "Second paragraph."]
    out = chunks.parse(as_page(data))
    assert out["chunks"][0]["text"].endswith("(P<0.05).")
    assert out["chunks"][1]["text"] == f"{WORDS}\n\nSecond paragraph."


def test_newlines_in_single_line_fields():
    data = reply()
    data["chunks"][0]["reference"] = "N Engl J Med.\n2009;360(22)"
    assert chunks.parse(as_page(data))["chunks"][0]["reference"] == "N Engl J Med. 2009;360(22)"


def test_template_echo_is_rejected():
    data = reply()
    data["chunks"][0]["original_text"] = "The complete Original Text of the chunk."
    assert any("template" in p for p in chunks.parse(as_page(data))["problems"])


def test_file_name_in_text_warns():
    data = reply()
    data["chunks"][0]["original_text"] = f"{WORDS} See source.pdf."
    assert any("file name" in w for w in chunks.parse(as_page(data))["warnings"])


def test_usage_limit_message_is_recognised():
    assert chunks.USAGE_LIMIT.search("You've reached our limit of messages. Try again later.")
    assert chunks.USAGE_LIMIT.search("You've reached your file upload limit.")
    assert not chunks.USAGE_LIMIT.search("Long-term limits of radiofrequency ablation were reported.")
    assert not chunks.USAGE_LIMIT.search("Enzyme levels reached the upper limit of normal.")


def batch_reply(*names):
    return as_page({"documents": [{**reply(), "file_name": n} for n in names], "complete": True})


def test_batch_matches_each_pdf():
    out = chunks.parse_batch(batch_reply("b.pdf", "a.pdf"), ["a.pdf", "b.pdf"])
    assert list(out) == ["a.pdf", "b.pdf"] and all(r["problems"] == [] and len(r["chunks"]) == 2 for r in out.values())


def test_batch_without_file_names_goes_by_order():
    data = {"documents": [reply(), reply()], "complete": True}
    out = chunks.parse_batch(as_page(data), ["a.pdf", "b.pdf"])
    assert all(r["problems"] == [] for r in out.values())


def test_batch_cut_off_keeps_finished_documents():
    text = batch_reply("a.pdf", "b.pdf", "c.pdf")
    out = chunks.parse_batch(text[:int(len(text) * 0.5)], ["a.pdf", "b.pdf", "c.pdf"])
    assert out["a.pdf"]["problems"] == []
    assert out["b.pdf"]["problems"] and out["c.pdf"]["problems"] and not out["c.pdf"]["chunks"]


def test_batch_missing_pdf():
    out = chunks.parse_batch(batch_reply("a.pdf"), ["a.pdf", "b.pdf"])
    assert out["a.pdf"]["problems"] == [] and out["b.pdf"]["problems"] == ["This PDF is missing from the JSON."]


def test_batch_only_top_level_complete():
    data = {"documents": [{k: v for k, v in reply().items() if k != "complete"}], "complete": True}
    assert any("complete" in p for p in chunks.parse(as_page(data))["problems"])


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
