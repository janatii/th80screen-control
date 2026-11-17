"""th80screen - control the screen of an EPOMAKER TH80 Pro V2 from Windows.

Works on Windows and Linux (on Linux, install 70-th80-screen.rules once for USB/HID access).

Commands:
  info                 read the screen settings (read-only, writes nothing)
  scan                 find EPOMAKER keyboards on USB, Bluetooth or the 2.4G dongle (read-only)
  toggle               flip between clock and stored image (bind it to a desktop shortcut)
  clock                sync the keyboard clock to this PC and show it
  mode clock|image     switch what the screen shows (keeps stored images)
  image FILE           upload a picture (PNG/JPG/...), fitted to 240x135
  gif FILE             upload an animated GIF (sampled to at most 18 frames)
  stats                upload a one-off system stats card (CPU/RAM/disk)
  stats --every MIN    keep refreshing the stats card (minimum 15 minutes)
  preview [OUT.png]    render the stats card to a PNG without touching the keyboard

Protocol adapted from th80-studio by Clouddios (MIT License,
https://github.com/Clouddios/th80-studio): report ID 4, 64-byte packets on the
vendor interface (usage page 0xFF1C, usage 0x92), 16-bit sum checksum of bytes
3..63, 24-bit little-endian offsets, 48-byte screen configuration block.

Every image/GIF/stats upload erases and rewrites the keyboard's flash, so it
takes a little while and should not be done constantly. Clock sync and mode
changes only rewrite the small configuration block.
"""
import argparse
import os
import re
import sys
import time
from datetime import datetime

VID, PID = 0x320F, 0x5055
PRODUCT = 'EPOMAKER TH80 Pro V2'
USAGE_PAGE, USAGE = 0xFF1C, 0x92
W, H = 240, 135
FRAME_BYTES = 65536
MAX_FRAMES = 18
MIN_STATS_MINUTES = 15


# ---------------------------------------------------------------- protocol --

def _checksum(packet):
    packet[1:3] = (sum(packet[3:]) & 0xFFFF).to_bytes(2, 'little')
    return bytes(packet)


def packet(command, data=b'', position=0):
    if not 1 <= command <= 255 or len(data) > 56 or not 0 <= position <= 0xFFFFFF:
        raise ValueError('invalid command, length or position')
    p = bytearray(64)
    p[0] = 4
    p[3] = command
    p[4] = len(data)
    p[5:8] = position.to_bytes(3, 'little')
    p[8:8 + len(data)] = data
    return _checksum(p)


def config_query(offset):
    """Read-only query for 4 bytes of the 48-byte config block."""
    if offset not in range(0, 48, 4):
        raise ValueError('invalid config offset')
    p = bytearray(64)
    p[0] = 4
    p[3] = 5
    p[4] = 4
    p[5:8] = offset.to_bytes(3, 'little')
    return _checksum(p)


def with_mode(config, mode):
    """mode 0 = clock, 1 = stored image/animation."""
    c = bytearray(config)
    c[33] = mode
    return bytes(c)


def with_clock(config, now):
    if not 2000 <= now.year <= 2099:
        raise ValueError('clock only supports years 2000-2099')
    bcd = lambda v: ((v // 10) << 4) | (v % 10)
    c = bytearray(with_mode(config, 0))
    c[35:42] = bytes([bcd(now.second), bcd(now.minute), bcd(now.hour),
                      (now.weekday() + 1) % 7,  # Sunday = 0
                      bcd(now.day), bcd(now.month), bcd(now.year - 2000)])
    return bytes(c)


def with_frames(config, count, delay_ms=None):
    if not 1 <= count <= MAX_FRAMES:
        raise ValueError(f'use 1 to {MAX_FRAMES} frames')
    c = bytearray(with_mode(config, 1))
    c[34] = count
    c[46] = count
    if count > 1:
        if not 60 <= delay_ms <= 65535:
            raise ValueError('frame delay must be 60-65535 ms')
        c[43:45] = delay_ms.to_bytes(2, 'little')
    return bytes(c)


def encode_frame(image):
    """240x135 RGB -> RGB565 big-endian, padded to a 64 KiB flash slot."""
    if image.size != (W, H):
        raise ValueError(f'frame must be {W}x{H}')
    out = bytearray()
    for r, g, b in image.convert('RGB').getdata():
        out += (((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)).to_bytes(2, 'big')
    out += bytes(FRAME_BYTES - len(out))
    return bytes(out)


# ------------------------------------------------------------------ device --

BUS_NAMES = {1: 'USB', 2: 'Bluetooth', 3: 'I2C', 4: 'SPI'}


# Over Bluetooth LE the TH80 Pro V2 reports VID 0x245A / PID 0x8276 and the name "TH80 Pro V2-<n>",
# with no "EPOMAKER" anywhere (observed 2026-11-17 on this keyboard).
BT_IDS = {(0x245A, 0x8276)}
# The official "Image Custom Tool" (v1.0, Dec 2024) supports only these USB IDs, no Bluetooth.
# 0x5088 sits next to the TH80 Pro V2's 0x5055 and is most likely its 2.4G dongle (inferred, untested).
DONGLE_IDS = {(0x320F, 0x5088)}


def epomaker_evidence(d):
    """Why a HID device is believed to be an EPOMAKER keyboard, or None. Never trusts the vendor ID
    alone: 0x320F is the Evision chip maker's ID and is shared by several keyboard brands."""
    text = f"{d.get('manufacturer_string') or ''} {d.get('product_string') or ''}".upper()
    if 'EPOMAKER' in text:
        return 'reports the EPOMAKER name'
    named_th80 = text.strip().startswith('TH80')
    known_bt = (d.get('vendor_id'), d.get('product_id')) in BT_IDS
    if known_bt and named_th80:
        return 'known TH80 Pro V2 Bluetooth ID and name'
    if (d.get('vendor_id'), d.get('product_id')) in DONGLE_IDS:
        return 'dongle ID listed in the official Image Custom Tool (likely TH80 Pro V2 2.4G)'
    if d.get('vendor_id') == VID and 'TH80' in text:
        return 'Evision chip with TH80 name'
    return None


def _is_epomaker(d):
    return epomaker_evidence(d) is not None


_bt_cache = {'at': 0.0, 'rows': []}


def _bt_paired_names(max_age=60):
    """Names of Epomaker devices paired over Bluetooth, from Windows (cached for max_age seconds)."""
    if time.monotonic() - _bt_cache['at'] < max_age:
        return _bt_cache['rows']
    _bt_cache.update(at=time.monotonic(), rows=_bt_query())
    return _bt_cache['rows']


IS_WINDOWS = sys.platform == 'win32'
IS_LINUX = sys.platform.startswith('linux')
MISSING_LIBS = 'Missing library: run  python3 -m pip install --user "hidapi>=0.15,<0.16" Pillow psutil'


def _hid():
    """The hidapi module. On Linux prefer the hidraw backend: it reports usage pages and
    sees Bluetooth devices, which the libusb backend does not."""
    if IS_LINUX:
        try:
            import hidraw
            return hidraw
        except ImportError:
            pass
    try:
        import hid
        return hid
    except ImportError:
        raise OSError(MISSING_LIBS)


def _bt_query():
    return _bt_query_windows() if IS_WINDOWS else _bt_query_linux() if IS_LINUX else []


def _bt_query_linux():
    import subprocess
    run = lambda *a: subprocess.run(['bluetoothctl', *a], capture_output=True, text=True, timeout=10).stdout
    try:
        listing = run('devices', 'Paired') or run('paired-devices')
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in listing.splitlines():
        parts = line.split(' ', 2)
        if len(parts) == 3 and parts[0] == 'Device' and re.search(r'EPOMAKER|TH80', parts[2], re.I):
            try:
                connected = 'Connected: yes' in run('info', parts[1])
            except (OSError, subprocess.SubprocessError):
                connected = False
            rows.append({'name': parts[2].strip(), 'connected': connected})
    return rows


def _bt_query_windows():
    import subprocess
    ps = ("Get-PnpDevice -ErrorAction SilentlyContinue | Where-Object { $_.InstanceId -match '^(BTHENUM|BTHLE)' "
          "-and $_.FriendlyName -match 'EPOMAKER|TH80' } | ForEach-Object { \"$($_.Status)|$($_.FriendlyName)\" }")
    try:
        out = subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True, text=True,
                             timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        if '|' in line:
            status, name = line.split('|', 1)
            rows.append({'name': name.strip(), 'connected': status.strip() == 'OK'})
    return rows


def scan():
    """Find Epomaker keyboards on every transport. Read-only.

    Returns a list of {'name', 'transport', 'vid', 'pid', 'screen'} plus Bluetooth pairings
    the OS knows about but that expose no HID interface right now."""
    hid = _hid()
    found = {}
    for d in hid.enumerate():
        if not _is_epomaker(d):
            continue
        bus = BUS_NAMES.get(d.get('bus_type'), 'unknown')
        if bus == 'USB' and (d['vendor_id'], d['product_id']) in DONGLE_IDS:
            bus = '2.4G dongle'
        elif bus == 'USB' and d['product_id'] != PID:
            bus = 'USB (unknown model)'
        key = (d['vendor_id'], d['product_id'], bus)
        entry = found.setdefault(key, {'name': d.get('product_string') or 'EPOMAKER device', 'transport': bus,
                                       'vid': f"{d['vendor_id']:04x}", 'pid': f"{d['product_id']:04x}",
                                       'screen': False, 'evidence': epomaker_evidence(d)})
        if d.get('usage_page') == USAGE_PAGE and d.get('usage') == USAGE:
            entry['screen'] = True
    result = list(found.values())
    if not any(r['transport'] == 'Bluetooth' for r in result):
        for bt in _bt_paired_names():
            result.append({'name': bt['name'], 'transport': 'Bluetooth' + ('' if bt['connected'] else ' (paired, not connected)'),
                           'vid': None, 'pid': None, 'screen': False,
                           'evidence': 'Bluetooth pairing name only (weak)'})
    return result


class Keyboard:
    def __enter__(self):
        hid = _hid()
        found = [d for d in hid.enumerate()
                 if _is_epomaker(d) and d.get('usage_page') == USAGE_PAGE and d.get('usage') == USAGE]
        # prefer the known wired TH80 Pro V2; any other Epomaker screen channel (BT/dongle) is a fallback
        wired = [d for d in found if d['vendor_id'] == VID and d['product_id'] == PID]
        found = wired or found
        if not found:
            seen = scan()
            if seen:
                where = ', '.join(f"{s['name']} via {s['transport']}" for s in seen)
                raise OSError(f'Found {where}. To change the screen, use the USB cable or the 2.4G dongle '
                              '(what is on the screen now keeps showing).')
            raise OSError('No EPOMAKER keyboard found. Plug the keyboard in with the USB cable '
                          'and close Epomaker software.')
        if len(found) > 1:
            raise OSError('More than one EPOMAKER screen found; connect only one.')
        self.h = hid.device()
        try:
            self.h.open_path(found[0]['path'])
        except OSError as e:
            if IS_LINUX:
                raise OSError('The keyboard was found but could not be opened (permission denied?). Install the '
                              'udev rule once: sudo cp 70-th80-screen.rules /etc/udev/rules.d/ && sudo udevadm '
                              'control --reload && sudo udevadm trigger, then replug the keyboard.') from e
            raise OSError(f'Could not open the keyboard ({e}). Close Epomaker software and try again.') from e
        for _ in range(10):  # drain stale input
            if not self.h.read(64, 20):
                break
        return self

    def __exit__(self, *exc):
        self.h.close()

    def transact(self, request, timeout=3.0):
        if self.h.write(request) != len(request):
            raise OSError('incomplete USB write')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = bytes(self.h.read(64, 100))
            if len(resp) >= 8 and resp[:4] == request[:4]:
                if request[3] != 5 and resp[7] in (0xFE, 0xFF):
                    raise OSError(f'keyboard rejected command 0x{request[3]:02x}: {resp.hex()}')
                return resp
        raise TimeoutError(f'no reply to command 0x{request[3]:02x}; stopped (nothing retried)')

    def read_config(self):
        return b''.join(self.transact(config_query(o))[4:8] for o in range(0, 48, 4))

    def cmd(self, command, data=b'', position=0):
        if command == 2:
            time.sleep(0.1)
        return self.transact(packet(command, data, position), 30 if command == 0x23 else 3)

    def write_config(self, config):
        if len(config) != 48:
            raise ValueError('config must be 48 bytes')
        self.cmd(1)
        self.cmd(6, config)
        self.cmd(2)

    def upload_frames(self, frames, delay_ms=None, progress=None):
        if any(len(f) != FRAME_BYTES for f in frames):
            raise ValueError('bad frame size')
        if progress is None:
            progress = lambda pct: print(f'\r  uploading {pct:3d}%', end='' if pct < 100 else '\n', flush=True)
        config = with_frames(self.read_config(), len(frames), delay_ms)
        data = b''.join(frames)
        data += data  # the keyboard stores two copies
        self.write_config(config)
        self.cmd(0x23)  # erase image area
        self.cmd(1)
        total = len(data)
        for off in range(0, total, 56):
            self.cmd(0x21, data[off:off + 56], off)
            if off % (56 * 64) == 0:
                progress(off * 100 // total)
        self.cmd(2)
        progress(100)


# ------------------------------------------------------------------- media --

def fit(image):
    from PIL import Image, ImageOps
    image = ImageOps.exif_transpose(image).convert('RGBA')
    bg = Image.new('RGBA', image.size, 'black')
    bg.alpha_composite(image)
    return ImageOps.fit(bg.convert('RGB'), (W, H), method=Image.Resampling.LANCZOS)


def load_gif(path):
    from bisect import bisect_right
    from PIL import Image
    with Image.open(path) as src:
        n = getattr(src, 'n_frames', 1)
        if n <= 1:
            return [fit(src)], None
        ends, total = [], 0
        for i in range(n):
            src.seek(i)
            total += max(10, int(src.info.get('duration', 100) or 100))
            ends.append(total)
        count = min(n, MAX_FRAMES)
        delay = min(65535, max(60, round(total / count)))
        picks = [min(n - 1, bisect_right(ends, i * total / count)) for i in range(count)]
        frames = []
        for i in picks:
            src.seek(i)
            frames.append(fit(src))
        return frames, delay


def stats_card():
    import cards  # shared with the web app
    return cards.stats_card()


# ---------------------------------------------------------------- commands --

def cmd_info(_):
    with Keyboard() as kb:
        c = kb.read_config()
    mode = {0: 'clock', 1: 'image/animation'}.get(c[33], f'unknown ({c[33]})')
    unbcd = lambda v: (v >> 4) * 10 + (v & 0xF)
    s, m, h, wd, day, mon, yr = c[35:42]
    print(f'Screen mode : {mode}')
    print(f'Frames      : {c[34]} (delay {int.from_bytes(c[43:45], "little")} ms)')
    print(f'Clock (last set): 20{unbcd(yr):02d}-{unbcd(mon):02d}-{unbcd(day):02d} '
          f'{unbcd(h):02d}:{unbcd(m):02d}:{unbcd(s):02d}')
    print(f'Raw config  : {c.hex(" ")}')


def cmd_scan(_):
    rows = scan()
    if not rows:
        print('No EPOMAKER keyboard found on USB, Bluetooth or a dongle.')
    for r in rows:
        ids = f" [{r['vid']}:{r['pid']}]" if r['vid'] else ''
        print(f"{r['name']}{ids} via {r['transport']} - screen control: {'yes' if r['screen'] else 'no'}"
              f" - identified by: {r['evidence']}")


def cmd_clock(_):
    with Keyboard() as kb:
        cfg = kb.read_config()
        now = datetime.now()
        kb.write_config(with_clock(cfg, now))
    print(f'Clock set to {now:%Y-%m-%d %H:%M:%S} and screen switched to clock.')


def cmd_mode(args):
    with Keyboard() as kb:
        cfg = kb.read_config()
        kb.write_config(with_clock(cfg, datetime.now()) if args.which == 'clock' else with_mode(cfg, 1))
    print(f'Screen switched to {args.which}.')


def toggle():
    """Flip between clock and stored image. Returns the new mode."""
    with Keyboard() as kb:
        cfg = kb.read_config()
        new = 'image' if cfg[33] == 0 else 'clock'
        kb.write_config(with_mode(cfg, 1) if new == 'image' else with_clock(cfg, datetime.now()))
    return new


def cmd_toggle(_):
    print(f'Screen switched to {toggle()}.')


def cmd_image(args):
    from PIL import Image
    with Image.open(args.file) as src:
        frame = encode_frame(fit(src))
    with Keyboard() as kb:
        kb.upload_frames([frame])
    print('Image uploaded.')


def cmd_gif(args):
    frames, delay = load_gif(args.file)
    print(f'{len(frames)} frame(s)' + (f', {delay} ms each' if delay else ''))
    encoded = [encode_frame(f) for f in frames]
    with Keyboard() as kb:
        kb.upload_frames(encoded, delay)
    print('Animation uploaded.')


def cmd_stats(args):
    if args.every is not None and args.every < MIN_STATS_MINUTES:
        sys.exit(f'--every must be at least {MIN_STATS_MINUTES} minutes (each update rewrites the keyboard flash).')
    while True:
        frame = encode_frame(stats_card())
        with Keyboard() as kb:
            kb.upload_frames([frame])
        print(f'Stats card uploaded at {datetime.now():%H:%M}.')
        if args.every is None:
            break
        time.sleep(args.every * 60)


def cmd_preview(args):
    stats_card().save(args.out)
    print(f'Preview saved to {os.path.abspath(args.out)}')


def main():
    p = argparse.ArgumentParser(description='Control the EPOMAKER TH80 Pro V2 screen.')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('info', help='read screen settings (read-only)').set_defaults(fn=cmd_info)
    sub.add_parser('scan', help='find EPOMAKER keyboards on USB, Bluetooth or dongle (read-only)').set_defaults(fn=cmd_scan)
    sub.add_parser('clock', help='sync the clock and show it').set_defaults(fn=cmd_clock)
    sub.add_parser('toggle', help='flip between clock and stored image (bind this to a desktop shortcut)'
                   ).set_defaults(fn=cmd_toggle)
    m = sub.add_parser('mode', help='switch between clock and stored image')
    m.add_argument('which', choices=['clock', 'image'])
    m.set_defaults(fn=cmd_mode)
    i = sub.add_parser('image', help='upload a picture')
    i.add_argument('file')
    i.set_defaults(fn=cmd_image)
    g = sub.add_parser('gif', help='upload an animated GIF')
    g.add_argument('file')
    g.set_defaults(fn=cmd_gif)
    s = sub.add_parser('stats', help='upload a system stats card')
    s.add_argument('--every', type=int, metavar='MIN', help=f'refresh every MIN minutes (>= {MIN_STATS_MINUTES})')
    s.set_defaults(fn=cmd_stats)
    v = sub.add_parser('preview', help='render the stats card to a PNG only')
    v.add_argument('out', nargs='?', default='th80-preview.png')
    v.set_defaults(fn=cmd_preview)
    args = p.parse_args()
    try:
        args.fn(args)
    except (OSError, TimeoutError, ValueError) as e:
        sys.exit(f'Error: {e}')


if __name__ == '__main__':
    main()
