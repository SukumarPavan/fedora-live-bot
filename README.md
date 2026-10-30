# DeskBot: your floating robot sidekick

A cute chibi robot (silver head, glowing red eyes) that floats around your desktop,
pulls faces, and talks with you **by voice in real time**. Turn on **Screen** and it
sees what you're doing; turn on **Cam** and it sees you. Talk over it and it stops
to listen. The AI runs in the cloud on free tiers (Groq, Gemini, OpenRouter or any
OpenAI-compatible API), so nothing heavy runs on your PC.

Made for **Fedora (GNOME Wayland)**. Should also run on KDE, X11 and Windows 11.

## Install (Fedora)

```bash
unzip deskbot.zip && cd deskbot
./install.sh
```

The installer:
1. installs a few system packages with `dnf` (it asks for your password),
2. sets up a private Python environment in `~/.local/share/deskbot`,
3. adds **DeskBot** to your app menu and a `deskbot` command,
4. optionally turns on PipeWire **echo cancellation**, so you can use speakers,
5. optionally starts DeskBot when you log in.

## First run: add your free keys

Settings opens automatically. On the **Brain (AI)** tab, paste:

| Key | Used for | Get it |
| --- | --- | --- |
| **Groq** | Hearing you (Whisper) | https://console.groq.com/keys |
| **Gemini** | Thinking, seeing, and Live mode | https://aistudio.google.com/apikey |

Click **Test brain**, then on the **Hearing** tab click **Test hearing**. Done.

## Using it

| Do this | What happens |
| --- | --- |
| Just talk | It listens while the mic is on, answers out loud, and changes its face |
| Talk while it's talking | It stops and listens (barge-in) |
| Hover over it | Buttons appear: **Screen**, **Cam**, **Mic**, **Engine**, **Chat**, **Settings** |
| Right-click it | Menu: toggles, movement, voice engine, hide, quit |
| Drag it | Moves it. It remembers the spot. |
| Click it | It reacts. A click while it talks makes it stop. |
| Double-click it | Opens the chat window, where you can type instead of talk |

**Two voice engines** (switch with the Engine button or in Settings):
- **Universal**: works with any OpenAI-compatible provider. It hears you with Whisper, thinks with a vision model, and speaks with free Edge neural voices. You can mix providers.
- **Gemini Live**: Google's real-time voice. It's the most natural and fastest, and gets a live video feed. Free-tier sessions are short, but the bot reconnects on its own.

**English + Telugu:** the bot replies in the mix you use. Telugu sentences switch to a Telugu voice automatically.

## Privacy
- Screen and camera are **off at every start**. A red dot on its forehead means the camera is on; a little monitor icon means screen sight is on. GNOME also shows its own screen-sharing icon.
- Frames stay in memory and are only sent to the AI provider you picked. Nothing is saved to disk.
- API keys stay in `~/.config/deskbot/settings.json`, readable only by you.

## Troubleshooting

```bash
deskbot --selftest            # checks audio, packages, portal, keys
deskbot --selftest --screen   # also grabs one real screen frame
deskbot --reset-position      # bot lost off-screen
```

- **It keeps interrupting itself:** enable echo cancellation (re-run `install.sh`), use headphones, raise *Interrupt needs voice confidence* (Hearing tab), or tick *Mute the mic while the bot talks*.
- **Screen sight fails on GNOME:** you must pick a monitor in GNOME's dialog the first time. Packages needed: `pipewire-gstreamer gstreamer1-plugins-good`. *Forget chosen monitor* (Eyes tab) brings the dialog back.
- **The bot can't stay on top / move:** keep *Use XWayland* on (Look & Move tab). If it still misbehaves, switch *Window mode* to *Normal window* and restart.
- **Free limit reached:** switch the brain to another provider (Groq ↔ Gemini ↔ OpenRouter) in Settings.
- Logs: `~/.local/state/deskbot/deskbot.log`. Known limitations: [NOTES.md](NOTES.md).

## Uninstall

```bash
~/.local/share/deskbot/uninstall.sh
```

## Windows 11 (untested)
Install Python 3.12+, run `install_windows.bat`, then `run_deskbot.bat`. Use headphones.
