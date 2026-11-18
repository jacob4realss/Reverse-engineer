# My Video Saver

A small personal site that saves TikTok videos without the watermark.
It was designed after studying how SnapTik works with REA, but it uses its
own code, its own design and the open-source [yt-dlp](https://github.com/yt-dlp/yt-dlp)
instead of SnapTik's private server.

> Use it for your own videos, or videos you have permission to save.
> Respect creators' rights and TikTok's terms.

## How it works

```
Your browser ──/api/info?url=…──────▶ server.py ──yt-dlp──▶ TikTok
             ◀── title, creator, thumbnail, qualities ──
Your browser ──/api/download?url=…&quality=…──▶ server.py ──▶ TikTok video file
             ◀────────────── the video, streamed through ───────────
```

- `index.html`: the page (paste box, preview card, download buttons).
- `server.py`: a Python server with two API addresses:
  - `/api/info`: looks the video up and lists the available qualities.
  - `/api/download`: fetches the chosen file and passes it to you.
    TikTok's file addresses only work for the server that looked them up,
    so the video goes through this server.

Qualities:

| Button          | What you get                                                  |
| --------------- | ------------------------------------------------------------- |
| Best quality    | The highest-resolution file (often H.265; plays on iPhones)   |
| Most compatible | The best H.264 file (plays on practically every device)       |
| Sound only      | The audio track                                               |

TikTok's own watermarked copy is never offered.

## Run it

Requirements: Python 3.9+ and yt-dlp.

```bash
pip install yt-dlp
python3 server.py
```

Then open <http://127.0.0.1:8000>.

## Settings

| Variable       | Default     | Meaning                                                         |
| -------------- | ----------- | --------------------------------------------------------------- |
| `HOST`         | `127.0.0.1` | Address to listen on. Use `0.0.0.0` when hosting it online.     |
| `PORT`         | `8000`      | Port to listen on.                                              |
| `APP_PASSWORD` | (none)      | If set, the page asks for this password before every request.   |
| `RATE_LIMIT`   | `10`        | API requests allowed per visitor per minute.                    |

Example with a password:

```bash
APP_PASSWORD='choose-something-long' python3 server.py
```

## Put it online with Render

Create a **Web Service** from this repository with:

| Setting        | Value                             |
| -------------- | --------------------------------- |
| Branch         | the branch that holds this folder |
| Root Directory | `my-video-saver`                  |
| Runtime        | Python 3                          |
| Build Command  | `pip install -r requirements.txt` |
| Start Command  | `python3 server.py`               |
| Instance Type  | Free                              |

Environment variables:

| Key            | Value                                   |
| -------------- | --------------------------------------- |
| `HOST`         | `0.0.0.0`                               |
| `APP_PASSWORD` | a long password only you know           |

Render sets `PORT` itself. On the free plan the service sleeps when idle,
so the first visit after a break takes up to a minute.

## Safety built in

- Only TikTok links are accepted (`tiktok.com`, `www.`, `m.`, `vm.`, `vt.`),
  so the server can't be used to fetch other websites.
- Per-visitor rate limit and an optional password.
- Downloads larger than 300 MB are refused.
- No ads, analytics or third-party scripts.

## Keeping it working

TikTok changes things regularly. If lookups start failing, update yt-dlp:

```bash
pip install --upgrade yt-dlp
```
