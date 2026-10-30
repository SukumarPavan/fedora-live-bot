# DeskBot: floating chibi robot companion (Fedora GNOME Wayland)

## Goal
A cute floating chibi robot (silver head, glowing red ring eyes, original design) that
roams the desktop, shows live expressions, and talks with you by voice in real time.
You can interrupt it by talking. Two toggles on the bot: **Screen** (it sees your
screen) and **Cam** (it sees you). Cloud APIs only, free tiers. Every option is
editable in a no-code Settings window.

## Locked decisions (gate 1, 2026-10-30)
| Decision | Chosen | Why |
| --- | --- | --- |
| Target desktop | Fedora GNOME Wayland | User's setup |
| Voice engines | Universal (any OpenAI-compatible) + Gemini Live, switchable | Universal covers any API; Live sounds most human |
| Default providers | Groq (ears: Whisper) + Gemini (brain/eyes, Live) | Best free quality |
| Delivery | Zip + `install.sh` (venv, launcher, autostart, uninstaller) | No GitHub or remote PC available |
| Bot window | Qt via XWayland (`QT_QPA_PLATFORM=xcb`), override-redirect overlay | Native Wayland forbids app-set position and always-on-top |
| Screen capture | xdg-desktop-portal ScreenCast (jeepney D-Bus) + `gst-launch-1.0 pipewiresrc` subprocess | Only sanctioned live capture on GNOME Wayland; restore token = pick monitor once |
| Fallback capture | `mss` (X11/Windows), custom command (e.g. spectacle) | No-code escape hatch |
| Audio | sounddevice (PortAudio→PipeWire), 16 kHz mic, 24 kHz player | Full control: instant stop and lip-sync level |
| VAD | Silero ONNX (2 MB, CPU), downloaded at install | Accurate barge-in; tiny, not an AI chat model |
| STT | OpenAI-compatible `/audio/transcriptions` (Groq whisper-large-v3-turbo) | Free, fast |
| LLM | OpenAI-compatible chat, streaming, images as data URLs | Works with Groq/Gemini/OpenRouter/OpenAI/Mistral/DeepSeek/etc. |
| TTS | Edge neural voices (free, no key); OpenAI-compatible `/audio/speech`; espeak-ng fallback | Free and natural |
| Live mode | google-genai `client.aio.live.connect`, resumption + compression, local barge gate | Verified SDK API 2.34 |
| Python | system `python3 -m venv` (3.13/3.14 wheels verified for all deps) | Simple |
| Art | Procedural QPainter drawing | Live expressions, lip-sync, any size, no asset files |
| Language (gate 2) | English + Telugu mix: English voice + Telugu voice, auto-switch per sentence by script | User speaks Tenglish |
| Proactive (gate 2) | "Sometimes": only on clear screen/cam change, at most every ~2 min, slider | Saves free quota |
| Echo cancel (gate 2) | install.sh adds PipeWire `monitor.mode` echo-cancel (nofail) and sets it as default mic; undone by uninstall.sh | Speakers without self-interruption |
| Defaults (gate 2) | Name "Ultron Jr", always listening while mic on, keys in 0600 JSON, Windows untested, no RPM | Approved |
| Model names | "auto" for Live (pick first `bidiGenerateContent` model), `gemini-flash-latest` alias for brain, Fetch-models buttons | Model names change; avoid hardcoding guesses |

## Phases
### Phase 1: Skeleton, settings, bot art, overlay window
Verified when: offscreen render of every expression → PNG sheet looks right; app starts under Xvfb without errors.
- 1.1 settings.py (defaults, presets, load/save 0600) — S
- 1.2 robot_art.py procedural robot + expressions — M
- 1.3 bot_window.py overlay, float/hop/roam, drag, hover buttons, menu, bubble — M

### Phase 2: Audio + Universal voice engine
Verified when: fake OpenAI-compatible server test passes end to end (STT → streamed LLM → sentence TTS → player) and barge-in cancels playback.
- 2.1 audio_io.py mic + player (level, stop, robot effect) — S
- 2.2 vad.py Silero + endpointing/barge state machine — S
- 2.3 tts.py edge/openai/espeak — S
- 2.4 engine_universal.py — M

### Phase 3: Eyes
Verified when: mss backend returns JPEG under Xvfb; portal code path covered by self-test (untestable here).
- 3.1 vision.py camera thread, screen backends, change detection, composite — M
- 3.2 portal_screencast.py — M
- 3.3 proactive comments loop — S

### Phase 4: Gemini Live engine
Verified when: unit test with mocked session object (no key available here).
- 4.1 engine_live.py — M

### Phase 5: Settings UI, chat window, self-test
Verified when: settings dialog opens under Xvfb, round-trips every key.
### Phase 6: install.sh, uninstall.sh, .desktop, autostart, README, NOTES, zip

## Not yet specified
- Phase 3: real GNOME behaviour of pipewiresrc on the user's GPU (DMA-BUF): fog, needs user's self-test output.
- Phase 1: XWayland override-redirect stacking above fullscreen apps: fog, needs user testing.

## Always / Never
- Always: cloud APIs for intelligence; every option in Settings UI; never store screenshots/camera frames on disk.
- Never: local LLM; RPM in this round; copying the reference artwork.

## Progress
| Phase | Status |
| --- | --- |
| 1 | done: expression sheet rendered, app runs under Xvfb |
| 2 | done: fake-API end-to-end + barge-in + carry-over tests pass |
| 3 | done: mss frame captured under Xvfb; portal path untested (needs GNOME) |
| 4 | done: mocked LiveServerMessage tests; real connect reached auth check only |
| 5 | done: all 8 settings tabs render; single instance verified |
| 6 | done: install.sh -y ran clean in a temp HOME; zip delivered |
| User test on Fedora | pending: `deskbot --selftest --screen` output needed |
