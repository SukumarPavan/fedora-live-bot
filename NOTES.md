# DeskBot notes: gotchas, limits and negatives (keep updated)

## Not yet verified on real hardware (built in a cloud sandbox)
- GNOME Wayland: XWayland overlay stacking, portal screen capture via `pipewiresrc`, PipeWire echo-cancel, real mic/speaker/webcam. Run `deskbot --selftest --screen` first and report any FAIL.
- Real API round trips (no keys here). Fake-server tests cover the Universal pipeline. Mocked-message tests cover the Live engine. A real Gemini/Groq call was only tested as far as "invalid key".
- Edge TTS host is blocked in the sandbox, so voices were never heard here.
- Windows 11 path: written, never run.

## Platform (GNOME Wayland)
- Native Wayland apps cannot set their own position or stay on top. The bot runs under XWayland (`QT_QPA_PLATFORM=xcb`) as an override-redirect overlay. It also floats above fullscreen apps; use "Hide for 10 minutes".
- Qt 6.5+ xcb plugin needs `xcb-util-cursor`, or the app will not start.
- On Wayland, apps cannot read the global mouse position. Eyes follow the cursor only while it is over X11 windows (the bot itself); otherwise the bot glances randomly.
- Screen capture: xdg portal ScreenCast → PipeWire node + fd → `gst-launch-1.0 pipewiresrc` → JPEG on stdout. GNOME asks for a monitor once; the restore token is saved. GNOME shows a sharing icon while capture is on.
- Mutter only sends frames when the screen changes, so the last frame is kept.
- XWayland windows may look blurry with fractional scaling.
- GNOME has no tray icon by default: all controls are on the bot (hover buttons, right-click menu).
- Laptop IPU6/MIPI webcams often are not V4L2-compatible, so OpenCV can't open them.

## Audio
- Speakers + mic means the bot hears itself. Defences: PipeWire echo-cancel (`monitor.mode`, `nofail`, set as default mic), a stricter barge-in threshold while the bot speaks, a half-duplex option, or headphones.
- A broken PipeWire module config can stop all audio. That's why the installer adds `flags = [ nofail ]`.
- Whisper invents text on noise ("Thank you.", "Thanks for watching"). These are filtered out.
- A pause mid-sentence ends the turn. If you keep talking before the bot answers, the first half is kept and merged with the second (carry-over).

## APIs
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
