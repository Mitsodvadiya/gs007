# GastroAGI Chunk Uploader

Turns PDF articles into knowledge chunks with ChatGPT and uploads them to the GastroAGI admin panel (`admin.gastroagi.com/upload-documents`).

**To install it, follow [SETUP.md](SETUP.md).**

1. You pick PDFs in the Chrome extension.
2. Every Chrome profile signed in to ChatGPT works through them at the same time, 3 PDFs per message, using `program/prompt.md`.
3. The program reads the JSON reply and checks each PDF's chunks.
4. One panel tab fills in each PDF's chunks, using Add More Chunks for each extra chunk.
5. It clicks Upload, straight away or after your review.

It works in your own signed-in Chrome with Playwright over remote debugging, on Windows.

## Settings

`program/config.json`, read when the program starts:

| Setting | What it does |
|---|---|
| `chatgpt_profiles` | `"auto"` finds every signed-in Chrome profile, including open ones. To choose by hand, list profile folders, such as `["Default", "Profile 2"]`. |
| `skip_profiles` | Profiles `"auto"` must not use, by folder, name or email. |
| `pdfs_per_message` | PDFs in one ChatGPT message. |
| `panel_profile` | The profile folder signed in to the admin panel. Uploading is one PDF at a time. |
| `panel_url` | The panel's upload page. |
| `port` | Local port. The extension's `manifest.json` and `panel.js` use the same one. |

The program looks for signed-in profiles when it starts and before each new set of PDFs. Chrome locks an open profile's login file, so an open profile is confirmed by opening its ChatGPT tab. If it turns out signed out, the tab closes and the profile is skipped.

## What happens with each ChatGPT reply

The program reads ChatGPT's stored copy of the reply, because the page drops text after `<` (as in `P<0.001`) outside code blocks. It falls back to the page text if that copy can't be read.

| ChatGPT's reply | What the program does |
|---|---|
| Complete JSON | Fills the panel |
| Small slips: text around the JSON, trailing commas, key spelling, HTML codes, citation marks, two-part reply | Repairs them and fills the panel |
| Cut off partway through a message of 3 PDFs | Keeps the finished PDFs and asks again only for the rest |
| One PDF stops mid-sentence, has a wrong chunk count, no `"complete": true`, or template text left in | Asks again for that PDF, 3 tries in all |
| Still incomplete after that | Tries each remaining PDF alone in a new chat, then fails that PDF only |
| Refusal or question, such as an unreadable scanned PDF | Asks again, then fails and shows what ChatGPT said |
| ChatGPT error, timeout or a closed tab | Puts the PDFs back in the queue, 3 tries in all |
| Usage limit message | Rests that account 60 minutes; the others carry on |

A PDF that never becomes complete fails on its own and nothing is filled for it. **Try again** on its card reruns it. If only the upload failed, its chunks are reused.

**Upload automatically** skips your review for PDFs with no warnings. Any warning (empty field, a chunk under 60 or over 260 words, a repeated Section, a file name in the text) still waits for you.

## Files

| Path | What it holds |
|---|---|
| `extension/` | The Chrome side panel: the PDF form and progress |
| `program/server.py` | Local server on port 8765; keeps jobs on disk |
| `program/engine.py` | The queue: accounts, retries and the upload order |
| `program/chatgpt_controller.py` | Drives ChatGPT in Chrome |
| `program/chunks.py` | Reads the chunks out of ChatGPT's reply and checks them |
| `program/panel.py` | Fills the panel and clicks Upload |
| `program/prompt.md` | The ChatGPT prompt. The next batch uses any edit |
| `program/jobs/<id>/` | Each PDF: the file, `reply_0.md` (plus one for each retry), `chunks.json`, `state.json`, `job.log`, `after_upload.png` |

## Tests

Run these in `program/`. They use made-up replies and a local copy of the form, and never touch ChatGPT or the real panel. `test_panel.py` needs `playwright install chromium` first.

```bash
python test_chunks.py
```

```bash
python test_engine.py
```

```bash
python test_panel.py
```

## If a website changes

- **Panel fields not found:** `FIELD_SELECTORS` at the top of `program/panel.py`. The fields have no ids, so they are matched by placeholder text.
- **ChatGPT changed its page:** selectors are at the top of `program/chatgpt_controller.py`.
