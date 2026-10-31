"""
Turns ChatGPT's reply into chunk records and checks them.

The reply must be JSON. The usual shape is one code block, but small slips are repaired here: text around the JSON,
trailing commas, key spelling ("Original Text"), a reply split into two blocks. Anything that can't be trusted to be
whole lands in problems and never reaches the panel.

problems = the reply can't be used, ask ChatGPT again. warnings = usable, but a person should look before it uploads.
"""

import html
import json
import re

MIN_WORDS, MAX_WORDS = 60, 260  # the prompt asks for about 100-200; outside this is worth a human look

SHARED_FIELDS = ("topic", "subtopic", "section", "reference")
TEXT_KEYS = ("original_text", "originaltext", "text", "content")
# citation markers ChatGPT adds when it reads a file: citeturn0file0, 【4:2†source】, leftovers of both
CITATION = re.compile(r"[^]*|【[^】]*】|[-]|\b(?:file)?cite(?:turn\d+\w*)+")
SENTENCE_END = re.compile(r"[.?!…][)\]”\"’']*$")
TRAILING_COMMA = re.compile(r",(\s*[}\]])")
TEMPLATE_VALUES = {"topic", "subtopic", "section", "reference", "single preferred topic", "single preferred subtopic",
                   "formatted citation", "the complete original text of the chunk."}
# ChatGPT won't answer this account until it clears. "reached the upper limit of normal" must not match.
USAGE_LIMIT = re.compile(r"reached (?:your|our)\b[^.\n]{0,40}\blimit|too many requests|usage cap|limit resets", re.I)


def _clean(value) -> str:
    if isinstance(value, list):
        value = "\n\n".join(str(v) for v in value)
    text = html.unescape(CITATION.sub("", str(value or ""))).replace("**", "").replace("\r", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _one_line(value) -> str:
    """Topic, Subtopic, Section and Reference are single-line inputs on the panel."""
    return " ".join(_clean(value).split())


def _normal_keys(data: dict) -> dict:
    return {re.sub(r"[\s-]+", "_", str(key).strip().lower()): value for key, value in data.items()}


def _documents(reply: str) -> list[dict]:
    """Every document in the reply, in order. When the whole JSON is cut off its outer object can't be read, but the
    scan then reaches the finished documents inside it, so their work is kept."""
    decoder, found, position = json.JSONDecoder(strict=False), [], 0
    for match in re.finditer(r"\{", reply):
        if match.start() < position:
            continue
        try:
            data, position = decoder.raw_decode(reply, match.start())
        except json.JSONDecodeError:
            continue
        data = _normal_keys(data)
        if isinstance(data.get("documents"), list):
            for item in data["documents"]:
                item = _normal_keys(item) if isinstance(item, dict) else {}
                if isinstance(item.get("chunks"), list):
                    found.append(item)
        elif isinstance(data.get("chunks"), list):
            found.append(data)
    return found


def _match(file_name, names: list[str]) -> str | None:
    wanted = _one_line(file_name).lower()
    for name in names:
        if name.lower() == wanted or (wanted and (name.lower() in wanted or wanted in name.lower())):
            return name
    return None


def _merge(parts: list[dict]) -> dict:
    """A long reply can arrive as "part 1" and "part 2"."""
    merged = dict(parts[0])
    merged["chunks"] = [chunk for part in parts for chunk in part["chunks"]]
    merged["complete"] = parts[-1].get("complete")
    return merged


def _whole_number(value):
    return int(value) if str(value).strip().isdigit() else None


def parse_batch(reply: str, names: list[str]) -> dict:
    """One result per PDF name. Each PDF stands alone: a good one is usable even if another in the reply is not."""
    docs = _documents(reply) or _documents(TRAILING_COMMA.sub(r"\1", reply))
    grouped, unmatched = {name: [] for name in names}, []
    for doc in docs:
        name = _match(doc.get("file_name"), names)
        grouped[name].append(doc) if name else unmatched.append(doc)
    if len(names) == 1:
        grouped[names[0]] += unmatched
    else:
        for doc, name in zip(unmatched, [n for n in names if not grouped[n]]):
            grouped[name].append(doc)

    if '"chunks"' in reply or '"documents"' in reply:
        missing = "The JSON is cut off or invalid." if not docs else "This PDF is missing from the JSON."
    else:
        missing = "The reply has no JSON."
    return {name: _finish(_merge(parts)) if parts else {"chunks": [], "declared": None, "problems": [missing],
                                                         "warnings": []}
            for name, parts in grouped.items()}


def parse(reply: str) -> dict:
    """The result for a reply about one PDF."""
    return parse_batch(reply, ["document.pdf"])["document.pdf"]


def _finish(data: dict) -> dict:
    """{"chunks": [{topic, subtopic, section, reference, text, words}], "declared", "problems", "warnings"}"""
    chunks = []
    for item in data["chunks"]:
        item = _normal_keys(item) if isinstance(item, dict) else {}
        chunk = {key: _one_line(item.get(key)) or _one_line(data.get(key)) for key in SHARED_FIELDS}
        chunk["text"] = _clean(next((item[key] for key in TEXT_KEYS if item.get(key)), ""))
        chunk["words"] = len(chunk["text"].split())
        chunks.append(chunk)

    declared = _whole_number(data.get("number_of_chunks"))
    complete = str(data.get("complete")).strip().lower() == "true"
    return {"chunks": chunks, "declared": declared, "problems": find_problems(chunks, declared, complete),
            "warnings": find_warnings(chunks)}


def find_problems(chunks: list[dict], declared, complete: bool) -> list[str]:
    problems = []
    if not chunks:
        problems.append("The JSON has no chunks.")
    if not complete:
        problems.append('The JSON does not end with "complete": true.')
    if declared != len(chunks):
        problems.append(f"number_of_chunks is {declared} but there are {len(chunks)} chunks.")
    for i, chunk in enumerate(chunks, 1):
        if not chunk["text"]:
            problems.append(f"Chunk {i}: original_text is empty.")
        elif not SENTENCE_END.search(chunk["text"]):
            problems.append(f"Chunk {i}: original_text stops in the middle of a sentence: ...{chunk['text'][-40:]}")
        if any(chunk[key].lower() in TEMPLATE_VALUES for key in (*SHARED_FIELDS, "text")):
            problems.append(f"Chunk {i}: still holds the template's example text.")
    return problems


def find_warnings(chunks: list[dict]) -> list[str]:
    warnings, seen = [], set()
    for i, chunk in enumerate(chunks, 1):
        for key in SHARED_FIELDS:
            if not chunk[key]:
                warnings.append(f"Chunk {i}: {key} is empty.")
        if chunk["text"] and not MIN_WORDS <= chunk["words"] <= MAX_WORDS:
            warnings.append(f"Chunk {i}: {chunk['words']} words (expected about 100-200).")
        if any(chunk[key].startswith("[") for key in SHARED_FIELDS):
            warnings.append(f"Chunk {i}: a template placeholder was left in.")
        if re.search(r"\.pdf\b", chunk["text"], re.I):
            warnings.append(f"Chunk {i}: mentions a file name.")
        if chunk["section"] and chunk["section"].lower() in seen:
            warnings.append(f"Chunk {i}: repeats the Section '{chunk['section']}'.")
        seen.add(chunk["section"].lower())
    return warnings
