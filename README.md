# Reelwright

**AI video editor and clip studio.** Drop in long videos or stream VODs; Reelwright transcribes them, finds the moments most likely to go viral, edits them for vertical (captions, facecam split-screen, jump cuts, music, stickers), and schedules them to YouTube, TikTok and Instagram.

Everything runs on your computer. AI calls go to Claude (Anthropic) with your own key.

## Run it

| How | Command |
|---|---|
| Desktop app (.exe) | `python scripts/build_exe.py`, then run `dist/Reelwright/Reelwright.exe` |
| Desktop app from source | double-click `Reelwright.pyw` (or `python Reelwright.pyw`) |
| In a browser | `python -m reelwright` → http://127.0.0.1:38765 |

Requirements from source: Python 3.12+, `pip install -r requirements.txt`, and `ffmpeg`/`ffprobe` on PATH (the .exe bundles them). An NVIDIA GPU makes transcription and rendering much faster (NVENC + CUDA Whisper); the CPU works too.

The packaged app stores data in `%LOCALAPPDATA%\Reelwright`; from source it uses `./data` (override with `REELWRIGHT_DATA`).

## What it does

**Find clips**
- Whisper transcription with word timestamps (GPU), scene-cut, silence and audio-energy detection
- Claude ranks the best standalone moments (hook, payoff, emotion) and snaps them to sentence boundaries; offline heuristic when no key is set
- Semantic search: "find where chat goes crazy"
- Virality Lab: scores a video or clip 0-100 with a breakdown, compared against the most-viewed similar YouTube videos right now, plus title ideas and the strongest moments
- Custom training: style profiles learn from reference channels, your 👍/👎 clip ratings and how your published posts performed; the active profile steers clip picks, titles and the default look
- Learning memory: every 👍/👎, published clip and deleted AI pick is kept as a lesson (even after the clip or project is gone). The latest lessons steer the very next scan without retraining, and training compares the AI's predicted scores with real views to correct itself
- Personal ranking model: a small model trained on your own lessons (loudness, speech pace, energy spikes, scene cuts, chat, hook shape, length…) re-ranks every scan — cross-validated, and only switched on once it beats guessing. Works offline. Post view counts are pulled daily so it learns from results, and the style profile retrains itself after 10+ new lessons
- Learns your edits: the caption style / layout / zoom you keep choosing becomes the default, and how you re-trim AI picks (e.g. always cutting the first 1.5 s) is applied to new picks automatically. See it all in Virality Lab → “What Reelwright has learned”
- Deep scans: transcript lines tagged with measured loudness, fast speech and live-chat spikes (YouTube chat replay is downloaded with the VOD); long streams are scanned in 40-minute parts in parallel, then a judge pass ranks every candidate on one scale

**OBS link** (optional, page “OBS link”)
- Connect OBS directly (OBS 28+ WebSocket): replay-buffer saves and, optionally, finished recordings are imported and analyzed automatically. Remote buttons: ✂ Clip that (save replay), replay buffer start/stop, record start/stop
- OBS on another PC: either map its recording folder to a shared drive, or run the single-file **Reelwright Relay** there (download it from the OBS link page; `python reelwright_relay.py --token …`). It finds this PC on the network by itself and streams each saved clip here. The network receiver is a separate port that accepts only token-authenticated clip uploads; the rest of Reelwright stays bound to this PC

**Edit**
- Timeline with waveform, thumbnails, scene ticks, drag-to-trim, In/Out marks, transcript selection → clip
- 9:16 / 1:1 / 4:5 / 16:9, crop or blurred fit, face auto-reframe, zoom (punch-in)
- Split-screen facecam + gameplay layout with automatic facecam detection
- Animated word-by-word captions (Bold / Clean / Boxed / Neon; low / middle / high), title card, social-handle sticker
- AI jump cuts (remove silences; captions re-timed) and filler-word removal ("um", "uh"), speed, fades, colour presets and sliders, sharpen
- Auto cover: the most clickable frame of each render (sharp, well-lit, colourful, faces) becomes the YouTube custom thumbnail and TikTok cover
- Loudness normalisation (−14 LUFS), noise reduction, background music with level
- Ask AI: "make it punchier, cut the first sentence, warmer colours"
- Montage maker: join several clips into one highlight video
- GPU (NVENC) rendering with x264 fallback

**Import & publish**
- Upload files or import links from YouTube, Twitch, Kick and 1,000+ sites (yt-dlp)
- Channels: connect Twitch / Kick / YouTube channels, browse VODs, auto-import new streams on an interval, optionally run Autopilot on each
- Publish to YouTube (resumable upload, native scheduled publishing), TikTok (Content Posting API) and Instagram Reels (Graph API), with AI-written titles, descriptions and captions
- Autopilot: when a scan finishes, render the top N clips and schedule them across platforms at a set interval; uploads can be queued before rendering is done
- Queue with retry/cancel, upcoming-posts calendar, activity log

## Connecting accounts

- **Claude**: Settings → paste an API key from console.anthropic.com.
- **YouTube**: Google Cloud → enable YouTube Data API v3 → OAuth client (Desktop app) → upload its JSON on Publish → Sign in. Unverified Google projects can only upload private videos until verified.
- **TikTok**: TikTok for Developers app with Login Kit + Content Posting API (`user.info.basic`, `video.publish`), redirect URI shown on the Publish page. Until TikTok audits the app, posts are private (`SELF_ONLY`).
- **Instagram**: Business/Creator account linked to a Facebook Page; a Meta app (Facebook Login for Business) token with `instagram_basic` + `instagram_content_publish`, plus the Instagram account ID.

TikTok and Instagram APIs have no native scheduling, so scheduled posts to them are held locally and posted at the time — Reelwright must be running then. YouTube schedules natively (`publishAt`).

## Privacy policy and terms

`reelwright/static/legal/privacy.html` and `terms.html` are shown in the app (Settings → Legal, or `/legal/privacy.html`). Before
submitting TikTok, Meta or Google apps for review, fill in the highlighted placeholders (provider name, contact email, governing law)
and publish both files at a public URL (any static host, e.g. GitHub Pages): the review forms ask for those URLs. A test fails if the code
starts contacting a service the privacy policy doesn't mention.

## Develop

```
python scripts/verify.py       # compile + UI syntax (node) + lint (ruff, if installed) + tests: run before calling a change done
python -m reelwright doctor    # toolchain health: ffmpeg, NVENC, CUDA, Claude key, disk, yt-dlp age, OBS link, publishing, learning
python -m reelwright relay --help   # the OBS relay, from a source checkout
```
The same diagnostics are in Settings → Diagnostics. Activity shows Claude usage (calls, prompt-cache hit rate, estimated cost per feature) and the event log (`events.log` in the data folder, rotated at 2 MB). Virality Lab → “What Reelwright has learned” exports/imports the learning memory as JSON (backup, or carry it to another PC).

### Phone app: simulator, demo mode, mobile tests

- **Phone simulator.** In Settings → Phone remote, turn the remote on, then under *Developer* tick **Phone simulator** and press **Open phone simulator**. It opens the real phone app from the local listener at iPhone 15 / SE, Pixel 8, small Android or tablet size, with rotation, a notch/home-bar frame and a Wi-Fi / slow / offline toggle. It pairs itself through a shortcut that only works from this PC (127.0.0.1) and only while that toggle is on; turning the toggle off revokes the simulator's device.
- **Demo mode.** `python -m reelwright demo --open` (or **Start a demo sandbox** in the same panel) runs a separate copy of Reelwright in a new temporary data folder, seeded with sample projects (small generated videos), AI clips to review, rendered clips, sandbox accounts (one not approved), scheduled posts, a preset and a long form. In demo mode nothing is posted (uploads return a `demo.invalid` link), no AI provider is called, only sandbox accounts look connected, and it refuses to run on your real library folder.
- **Mobile tests.** `pip install -r requirements-dev.txt && python -m playwright install chromium`, then `python scripts/mobile_test.py`. It starts a demo server on free ports and drives the phone app with Playwright's iPhone 15 and Pixel 8 profiles (touch, viewport, user agent): pairing, every tab, a review swipe, trim + re-render, a preset, a schedule move, a publish refused by account approval and an action queued offline then sent. Screenshots per page per device go to `%TEMP%\reelwright-mobile-test` (`--out` to change). It isn't part of `verify.py`; the API-level tests in `tests/test_remote_mobile.py` are.
