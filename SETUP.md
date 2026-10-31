# Setup guide

This tool turns PDF articles into knowledge chunks with ChatGPT and uploads them to the GastroAGI admin panel. It runs on your own PC in your own Chrome. Setup takes about 15 minutes.

## What you need

- A Windows PC with Google Chrome
- [Python 3.10 or newer](https://www.python.org/downloads/). In the installer, tick **Add python.exe to PATH**.
- [Git](https://git-scm.com/download/win), or use **Code > Download ZIP** on GitHub instead
- Your own GastroAGI admin login
- At least one ChatGPT account

## 1. Get the project

```bash
git clone https://github.com/Mitsodvadiya/gastroagi-chunk-uploader.git
```

## 2. Install it

Open the `gastroagi-chunk-uploader` folder and double-click **setup.bat**. It installs what the program needs and puts a **GastroAGI Chunk Uploader** icon on your desktop.

Keep the project folder where it is. If you move it, run **setup.bat** again to fix the icon.

## 3. Turn on remote debugging in Chrome

This lets the program work in your Chrome. You only do it once.

1. Open `chrome://inspect/#remote-debugging` in Chrome.
2. Turn remote debugging **on**.

## 4. Add the Chrome extension

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and choose the `extension` folder inside the project.
4. Click the puzzle icon in the toolbar and pin **GastroAGI Chunk Uploader**.

## 5. Sign in

1. Sign in to `https://admin.gastroagi.com` in the Chrome profile you want to upload from.
2. Sign in to `https://chatgpt.com` in that profile, and in any other profiles you want to use. The program finds them by itself. More accounts means more PDFs at once.

## 6. Tell the program your admin profile

Skip this if your admin login is in your first Chrome profile.

1. In the Chrome profile with the admin login, open `chrome://version`.
2. Look at **Profile Path**. The last part is the folder name, such as `Default` or `Profile 3`.
3. Open `program/config.json` in Notepad and set `panel_profile` to that name:

```json
"panel_profile": "Profile 3",
```

## 7. Run it

1. Double-click the **GastroAGI Chunk Uploader** icon on your desktop and keep the black window open.
2. Click the extension icon in Chrome. The side panel opens and shows **Connected**, plus the ChatGPT accounts it found.
3. Choose your PDFs. Leave **Upload automatically** unticked for now.
4. Press **Start**.
5. Chrome asks **Allow remote debugging?** Click **Allow**. It may appear in another profile's window, so check them all. You have 4 minutes.
6. When a PDF shows **Ready to review**, check the chunks in the panel tab.
7. Press **Upload now** to upload, or **Discard** to upload nothing.

The admin panel saves for real, so test with an article you want to keep. Adding the same PDF twice creates duplicates.

## Running many PDFs

Add as many PDFs as you like. Each ChatGPT account takes 3 PDFs per message, and all accounts work at the same time. Uploads go one PDF at a time, about 15 seconds each.

Tick **Upload automatically** so you don't have to press Upload for every PDF. PDFs with warnings still wait for you. Check a small batch in the knowledge base first, because the program checks that chunks are complete, not that the medical content is correct.

If you close the program, unfinished PDFs carry on next time you start it.

## Good to know

- ChatGPT plans have usage limits. An account that reaches its limit rests for 60 minutes while the others continue.
- OpenAI's terms don't allow automated use of ChatGPT or getting around usage limits, so an account could be restricted. Using several accounts raises that risk.
- PDFs and ChatGPT replies stay on your PC in `program/jobs/`.
- To keep a profile out of it, add it to `skip_profiles` in `program/config.json`, then restart the program.

## If something goes wrong

| What you see | What to do |
|---|---|
| Side panel says **Offline** | Double-click the desktop icon and keep the window open. |
| Desktop icon is missing or doesn't open | Run **setup.bat** again. |
| `python` is not recognized | Reinstall Python and tick **Add python.exe to PATH**. |
| **Chrome refused the connection** | Press **Try again**, then click **Allow** in Chrome. |
| Stuck on **Click Allow**, then **Failed** | Allow may be in another Chrome window. Check step 3, then press **Try again**. |
| **The panel is signed out** | Sign in to the admin panel in the profile from step 6, then press **Try again**. |
| **No ChatGPT account is signed in** | Sign in to `chatgpt.com` in Chrome, then press **Try again**. |
| An account shows **Signed out** | Sign in to ChatGPT in that profile. It's checked again on your next PDFs. |
| A PDF **Failed** with a ChatGPT message | Read the message. Scanned PDFs often can't be read. |
| After changing a file or setting | Close the black window and double-click the desktop icon again. |
