# th80-screen control

Control the screen of an EPOMAKER TH80 Pro V2 from **Windows or Linux**: clock, pictures,
slideshows, GIFs and info cards (system stats, homelab status, weather, calendar, countdown,
status sign). Comes as a web app (TH80 Screen Studio) and a command-line tool.

## Connections

| Connection | ID | Screen control |
|---|---|---|
| USB cable | `320F:5055` | Yes (tested) |
| 2.4G dongle | `320F:5088` | Probably (the official tool supports it; untested here) |
| Bluetooth | `245A:8276` | No: detected and identified, but the keyboard offers no screen channel |

Whatever is on the screen keeps showing on any connection; the keyboard runs the clock itself.

## Setup

```
python3 -m pip install --user -r requirements.txt      # Windows: python -m pip ...
```

**Linux only, once:** allow your user to open the keyboard without sudo, then replug it:

```
sudo cp 70-th80-screen.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

On a headless machine (e.g. a Raspberry Pi) also make sure you're in `plugdev`:
`sudo usermod -aG plugdev $USER` (log out and back in).

Close Epomaker's own software while using this.

## Web app

- Windows: double-click `Start TH80 Screen Studio.cmd` (or `python app.py`)
- Linux: `./start.sh` (or `python3 app.py`)

It opens http://127.0.0.1:8780 and only accepts connections from the same computer.

| Feature | Windows | Linux |
|---|---|---|
| Clock / image switch, clock sync, uploads, all cards | ✅ | ✅ |
| Hotkey Ctrl+Alt+K | built in | add a desktop shortcut that runs `python3 /path/to/th80screen.py toggle` |
| Image while the PC is locked | ✅ | ✅ via systemd-logind (GNOME, KDE, Cinnamon, …) |
| Start at login | Startup folder | systemd user service, or desktop autostart without systemd |
| Bluetooth pairing detection | ✅ | ✅ via `bluetoothctl` |

## Command line

| Command | What it does | Writes to keyboard? |
|---|---|---|
| `th80screen.py scan` | List EPOMAKER keyboards on USB / Bluetooth / dongle and how each was identified | No |
| `th80screen.py info` | Show screen mode, frame count, clock | No |
| `th80screen.py clock` | Sync the clock and show it | Settings block only |
| `th80screen.py mode clock\|image` | Switch what the screen shows | Settings block only |
| `th80screen.py toggle` | Flip between clock and stored image | Settings block only |
| `th80screen.py image photo.png` | Upload a picture (fitted to 240×135) | Erases + rewrites image flash |
| `th80screen.py gif anim.gif` | Upload a GIF (sampled to ≤ 18 frames) | Erases + rewrites image flash |
| `th80screen.py stats [--every 30]` | Upload a CPU/RAM/disk card (refresh ≥ 15 min) | Erases + rewrites image flash |
| `th80screen.py preview card.png` | Render the stats card to a PNG only | No |

## Notes

- Image uploads rewrite flash memory, so auto-refresh has a 15-minute floor. Clock sync and
  clock/image switching only touch a small settings block and are fine to do often.
- The keyboard shipped with a **73-frame factory animation**. The first picture, GIF or card
  upload replaces it, and this tool can't restore it (it supports at most 18 frames).
- The keyboard itself rewrites some settings (mode, clock) when you switch connections.
- Identification never trusts the vendor ID alone (`320F` is the Evision chip maker's ID and
  is shared by other brands). Over Bluetooth the keyboard never says "EPOMAKER", so it is
  identified by its Bluetooth ID *and* its "TH80 Pro V2" name together.

Screen protocol adapted from [th80-studio](https://github.com/Clouddios/th80-studio) by
Clouddios (MIT License). Dongle ID from the official EPOMAKER Image Custom Tool v1.0.
