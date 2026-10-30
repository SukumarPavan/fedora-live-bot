# DeskBot notes: gotchas, limits and negatives (keep updated)

## Not yet verified on real hardware (built in a cloud sandbox)
- GNOME Wayland: XWayland overlay stacking, portal screen capture via `pipewiresrc`, PipeWire echo-cancel, real mic/speaker/webcam. Run `deskbot --selftest --screen` first and report any FAIL.
- Real API round trips (no keys here). Fake-server tests cover the Universal pipeline. Mocked-message tests cover the Live engine. A real Gemini/Groq call was only tested as far as "invalid key".
- Edge TTS host is blocked in the sandbox, so voices were never heard here.
- Windows 11 path: written, never run.

## Visibility (2026-10-31: "is it working or not?")
- A status pill under the bot always shows Listening / Thinking / Speaking / Live connected / the current error. Click it to open Settings → Check, which has a Test button for mic, hearing, brain, voice, Live, screen and camera.
- Never silent: Universal gives up after 20 s with a message. Live warns if it heard you but sent no reply within 12 s. Live connect has a 15 s timeout. Reconnects show "Live reconnecting in N s" instead of spamming bubbles.
- Brain first-word timeout is 8 s per model. API clients are reused per event loop, saving a TLS handshake per call.

## Platform (GNOME Wayland)
- Screen share: the D-Bus flow is verified against a fake portal (tests/test_portal_fake.py). The user's earlier timeout matches "GNOME's dialog not answered". The pill now says "Pick a screen in GNOME's dialog", Start waits 90 s, and switching off mid-dialog no longer crashes (old bug: `self.session` was None after a stop() race).
- Native Wayland apps cannot set their own position or stay on top. The bot runs under XWayland (`QT_QPA_PLATFORM=xcb`) as an override-redirect overlay. It also floats above fullscreen apps; use "Hide for 10 minutes".
- Qt 6.5+ xcb plugin needs `xcb-util-cursor`, or the app will not start.
- On Wayland, apps cannot read the global mouse position. Eyes follow the cursor only while it is over X11 windows (the bot itself); otherwise the bot glances randomly.
- Screen capture: xdg portal ScreenCast → PipeWire node + fd → `gst-launch-1.0 pipewiresrc` → JPEG on stdout. GNOME asks for a monitor once; the restore token is saved. GNOME shows a sharing icon while capture is on.
- Mutter only sends frames when the screen changes, so the last frame is kept.
- XWayland windows may look blurry with fractional scaling.
- GNOME has no tray icon by default: all controls are on the bot (hover buttons, right-click menu).
- Laptop IPU6/MIPI webcams often are not V4L2-compatible, so OpenCV can't open them.

## Audio
- **Speakers + internal mic, no echo cancel (the user's setup):** the bot heard its own voice. It cut itself off (a VAD can't tell its voice from yours) and answered its own words. Fixed by the echo guard: while the bot is audible, plus 0.8 s of tail, a block counts as you only if speech is likely AND the mic is `echo_margin`× louder than the learned echo level. Transcripts made mostly of the bot's recent words are dropped.
- The silent-mic alarm only fires if the mic never had sound since it started, and only on 2 checks in a row. Noise-gated headsets send exact zeros when quiet. The first alarm triggers one automatic mic re-check.
- If the `parecord` recorder dies, the mic restarts automatically (up to 3×/min), then says so.
- **Real bug on the user's Fedora (2026-10-31):** install.sh made the PipeWire echo-cancel source the SYSTEM default mic, and it delivered pure digital silence. The bot never heard anything, and Test hearing sent silence to Whisper, which made up words. Fixed: never set it as default. The installer and the app restore a real default. On Linux the mic is recorded with `parecord` from a named PipeWire source. "Auto" probes each source and picks one with sound (peak ≥ 1e-4). Settings has a live mic meter, and Test hearing refuses to transcribe silence.
- The launcher `cd`s into the installed app: `python -m deskbot` run inside a repo checkout otherwise imports the checkout, not the installed copy.
- If no speaker opens, `Player.play` drops audio. Before, it set `active=True` forever and the engine hung after its first reply.
- Speakers + mic means the bot hears itself. Defences: PipeWire echo-cancel (`monitor.mode`, `nofail`, set as default mic), a stricter barge-in threshold while the bot speaks, a half-duplex option, or headphones.
- A broken PipeWire module config can stop all audio. That's why the installer adds `flags = [ nofail ]`.
- Whisper invents text on noise ("Thank you.", "Thanks for watching"). These are filtered out.
- A pause mid-sentence ends the turn. If you keep talking before the bot answers, the first half is kept and merged with the second (carry-over).

## APIs: measured with a real free key on 2026-10-30
- **"It just floats and ignores me" (v0.1 bug):** the default `gemini-flash-latest` was overloaded (503). With `reasoning_effort=none` it hung for over 40 s with no bytes, then the SDK retried, so the bot sat "thinking" for minutes. Fixes: default `gemini-flash-lite-latest` (~0.9 s), backup models, a 12 s first-word timeout per model, failed models tried last for 3 min, and a v1→v2 settings migration.
- `gemini-2.5-flash` returns 404 "no longer available to new users". The 3.6/3.7/3.8 flash models were slow (21 s) or hung (> 30 s). Lite models (flash-lite-latest, 3.5/3.1-flash-lite) answered in about 1 s, with vision.
- `gemini-flash-lite-latest` rejects `reasoning_effort` with a vague 400. Any 400 while that optional parameter is sent now retries without it.
- Groq has NO vision chat model now (gpt-oss, qwen3.8, allam only). Use Gemini as the brain if Screen or Cam should work. Groq Whisper: 1.2 s, exact transcripts.
- Groq TTS (`canopylabs/orpheus`) needs terms accepted in the Groq console before use.
- Gemini Live: `gemini-2.5-flash-native-audio-latest` (what "auto" picks) answered in 1.3 s with a correct transcript. `gemini-3.8-live` cut in after one word (too eager). `gemini-3.1-flash-live-preview` timed out.
- Real Universal turn (VAD → Groq Whisper → Gemini lite → TTS): 2.6–3.6 s total.
- A TTS engine that fails is skipped for 5 min, so a blocked Edge doesn't add a timeout to every sentence.
- With the old language note the bot answered English questions in romanized Tenglish. The note now says: match the last message, Telugu only in Telugu script.
- Free-tier limits and model names change. Defaults: `gemini-flash-latest` alias, Live model `auto` (picks a `bidiGenerateContent` model at connect time), plus Fetch-models buttons.
- Gemini's OpenAI-compatible endpoint returns **400, not 401**, for a bad key; the reason is only in the body. `friendly_error` reads the body.
- `reasoning_effort` and image input are retried without them if a provider rejects them (remembered per model).
- Edge TTS is an unofficial free endpoint. Fallback chain: OpenAI-compatible TTS → espeak-ng.
- Gemini Live free sessions are short. Context compression and session resumption are on; it reconnects with backoff and stops retrying on a rejected key.
- Only the newest user turn carries images (screen ≤1280 px, cam ≤640 px JPEG). History keeps text only.

## Privacy
- Screen and camera always start OFF. Frames live in memory only. Keys are stored in `~/.config/deskbot/settings.json` (0600), not in the system keyring.

## Dev pitfalls hit while building
- `pkill -f "python -m deskbot"` inside a shell whose own command line contains that text kills the shell. Use `pgrep -f "[v]env/..."`.
- Qt quits when the last *normal* window closes; Tool/overlay windows don't count. The app sets `setQuitOnLastWindowClosed(False)`.
- The installer refuses root (`DESKBOT_ALLOW_ROOT=1` overrides, for testing only).
- PySide6 meta-package pulls WebEngine (~300 MB). Requirements use `PySide6-Essentials` only.
