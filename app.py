"""TH80 Screen Studio - local web interface for the EPOMAKER TH80 Pro V2 screen.

Run:  python app.py            (opens http://127.0.0.1:8780 in the browser)
      pythonw app.py --quiet   (background, no browser; used by "start at login")
"""
import base64
import ctypes
import io
import json
import os
import sys
import threading
import time
import traceback
import shutil
import subprocess
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

import cards
import th80screen as kb

ROOT = Path(__file__).resolve().parent
SETTINGS_FILE = ROOT / 'settings.json'
HOST, PORT = '127.0.0.1', 8780
IS_WINDOWS, IS_LINUX = kb.IS_WINDOWS, kb.IS_LINUX
STARTUP_VBS = Path(os.environ.get('APPDATA', '')) / r'Microsoft\Windows\Start Menu\Programs\Startup\TH80 Screen Studio.vbs'
SYSTEMD_UNIT = Path.home() / '.config' / 'systemd' / 'user' / 'th80-screen-studio.service'
XDG_AUTOSTART = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'autostart' / 'th80-screen-studio.desktop'

DEFAULT_SETTINGS = {
    'sync_clock_on_start': True,
    'hotkey': False,           # Ctrl+Alt+K toggles clock / image
    'lock_switch': False,      # show image while the PC is locked
    'auto': {'enabled': False, 'card': 'stats', 'every': 30},
    'cards': {
        'homelab': {'hosts': [
            {'name': 'Pi 5 (rpi55)', 'target': '192.168.100.133:22'},
            {'name': 'Pi 4 (rpi4)', 'target': '192.168.100.115:22'},
            {'name': 'Router', 'target': '192.168.100.1:80'},
            {'name': 'Open WebUI', 'target': '192.168.100.133:3001'},
            {'name': 'LearnHarness', 'target': '192.168.100.133:3000'},
            {'name': 'Proxy manager', 'target': '192.168.100.115:81'},
            {'name': 'llama-server', 'target': '127.0.0.1:8080'},
            {'name': 'Home Assistant', 'target': '192.168.100.133:8123'},
        ]},
        'weather': {'city': ''},
        'calendar': {'ics_url': ''},
        'daily': {'title': '', 'text': '', 'countdown': ''},
        'status': {'text': 'In a meeting', 'subtext': 'back soon', 'color': '#c62828'},
    },
    'last_upload': None,
}

device_lock = threading.Lock()
settings_lock = threading.Lock()
job = {'running': False, 'label': '', 'progress': 0, 'error': None, 'done_at': None}
auto_state = {'next_run': None, 'last_error': None}


# ---------------------------------------------------------------- settings --

def _merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        out[k] = _merge(base[k], v) if isinstance(base.get(k), dict) and isinstance(v, dict) else v
    return out


def load_settings():
    try:
        return _merge(DEFAULT_SETTINGS, json.loads(SETTINGS_FILE.read_text(encoding='utf-8')))
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT_SETTINGS))


settings = load_settings()


def save_settings():
    with settings_lock:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding='utf-8')


# ------------------------------------------------------------------ device --

def device_info():
    with device_lock, kb.Keyboard() as k:
        c = k.read_config()
    unbcd = lambda v: (v >> 4) * 10 + (v & 0xF)
    s, m, h, _wd, day, mon, yr = c[35:42]
    return {'connected': True, 'mode': {0: 'clock', 1: 'image'}.get(c[33], f'unknown ({c[33]})'),
            'frames': c[34], 'delay_ms': int.from_bytes(c[43:45], 'little'),
            'clock_set': f'20{unbcd(yr):02d}-{unbcd(mon):02d}-{unbcd(day):02d} {unbcd(h):02d}:{unbcd(m):02d}:{unbcd(s):02d}'}


def set_mode(mode):
    with device_lock, kb.Keyboard() as k:
        cfg = k.read_config()
        k.write_config(kb.with_clock(cfg, datetime.now()) if mode == 'clock' else kb.with_mode(cfg, 1))


def toggle_mode():
    with device_lock:
        return kb.toggle()


def start_job(label, frames_fn):
    """frames_fn() -> (list_of_PIL_frames, delay_ms_or_None). Runs the upload in the background."""
    if job['running']:
        raise RuntimeError('Another upload is still running.')
    frames, delay = frames_fn()  # render/validate first so bad input never touches the keyboard
    if not 1 <= len(frames) <= kb.MAX_FRAMES:
        raise ValueError(f'Use 1 to {kb.MAX_FRAMES} frames.')
    encoded = [kb.encode_frame(f) for f in frames]
    job.update(running=True, label=label, progress=0, error=None, done_at=None)

    def run():
        try:
            def progress(p):
                job['progress'] = p
            with device_lock, kb.Keyboard() as k:
                k.upload_frames(encoded, delay, progress)
            settings['last_upload'] = {'label': label, 'at': datetime.now().strftime('%Y-%m-%d %H:%M')}
            save_settings()
        except Exception as e:  # report to the UI
            job['error'] = str(e)
        finally:
            job.update(running=False, done_at=time.time())

    threading.Thread(target=run, daemon=True).start()


def decode_image(data_url):
    raw = base64.b64decode(data_url.split(',', 1)[-1])
    if len(raw) > 30 * 2**20:
        raise ValueError('File too large (30 MB max).')
    return Image.open(io.BytesIO(raw))


def card_opts(kind, override=None):
    opts = dict(settings['cards'].get(kind, {}))
    opts.update(override or {})
    return opts


def png_data_url(img):
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()


# ------------------------------------------------------------- automation --

def auto_loop():
    while True:
        time.sleep(5)
        a = settings['auto']
        if not a.get('enabled'):
            auto_state['next_run'] = None
            continue
        every = max(kb.MIN_STATS_MINUTES, int(a.get('every', 30)))
        now = time.time()
        if auto_state['next_run'] is None:
            auto_state['next_run'] = now + 3
        if now >= auto_state['next_run'] and not job['running']:
            auto_state['next_run'] = now + every * 60
            try:
                kind = a.get('card', 'stats')
                start_job(f'Auto: {kind} card', lambda: ([cards.render(kind, card_opts(kind)).convert('RGB')], None))
                auto_state['last_error'] = None
            except Exception as e:
                auto_state['last_error'] = str(e)


def is_locked():
    if IS_WINDOWS:
        import psutil
        return any(p.info['name'] and p.info['name'].lower() == 'logonui.exe' for p in psutil.process_iter(['name']))
    if IS_LINUX:
        # systemd-logind's LockedHint, set by GNOME, KDE, Cinnamon, xfce4-screensaver, swaylock (with logind), ...
        session = os.environ.get('XDG_SESSION_ID')
        if not session:
            out = subprocess.run(['loginctl', 'show-user', os.environ.get('USER', ''), '-p', 'Display', '--value'],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            session = out or None
        if not session:
            return False
        out = subprocess.run(['loginctl', 'show-session', session, '-p', 'LockedHint', '--value'],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return out == 'yes'
    return False


def lock_loop():
    was_locked = None
    while True:
        time.sleep(3)
        if not settings.get('lock_switch'):
            was_locked = None
            continue
        try:
            locked = is_locked()
            if was_locked is not None and locked != was_locked and not job['running']:
                set_mode('image' if locked else 'clock')
            was_locked = locked
        except Exception:
            pass


class Hotkey:
    """Global Ctrl+Alt+K via RegisterHotKey on its own message-loop thread."""
    MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, VK_K, WM_HOTKEY, WM_QUIT = 0x1, 0x2, 0x4000, 0x4B, 0x312, 0x12

    SUPPORTED = IS_WINDOWS
    LINUX_HINT = ('Global hotkeys are set in your desktop on Linux: add a custom shortcut (e.g. Ctrl+Alt+K) '
                  f'that runs:  python3 "{ROOT / "th80screen.py"}" toggle')

    def __init__(self):
        self.thread_id = None
        self.error = None

    def start(self):
        if not self.SUPPORTED:
            self.error = self.LINUX_HINT
            return
        if self.thread_id:
            return
        ready = threading.Event()
        threading.Thread(target=self._run, args=(ready,), daemon=True).start()
        ready.wait(2)

    def stop(self):
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, self.WM_QUIT, 0, 0)
            self.thread_id = None

    def _run(self, ready):
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, self.MOD_CONTROL | self.MOD_ALT | self.MOD_NOREPEAT, self.VK_K):
            self.error = 'Ctrl+Alt+K is already used by another program.'
            ready.set()
            return
        self.error = None
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == self.WM_HOTKEY and not job['running']:
                    try:
                        toggle_mode()
                    except Exception:
                        pass
        finally:
            user32.UnregisterHotKey(None, 1)


hotkey = Hotkey()


def startup_enabled():
    if IS_WINDOWS:
        return STARTUP_VBS.exists()
    return SYSTEMD_UNIT.exists() or XDG_AUTOSTART.exists()


def set_startup(enabled):
    if IS_WINDOWS:
        if enabled:
            pyw = Path(sys.executable).with_name('pythonw.exe')
            exe = pyw if pyw.exists() else Path(sys.executable)
            STARTUP_VBS.write_text(
                'Set sh = CreateObject("WScript.Shell")\r\n'
                f'sh.CurrentDirectory = "{ROOT}"\r\n'
                f'sh.Run """{exe}"" ""{ROOT / "app.py"}"" --quiet", 0, False\r\n', encoding='utf-8')
        elif STARTUP_VBS.exists():
            STARTUP_VBS.unlink()
        return
    if not IS_LINUX:
        raise ValueError('Start at login is only supported on Windows and Linux.')
    # Linux: a systemd user service when systemd is there (works headless too), else an XDG autostart entry
    use_systemd = shutil.which('systemctl') is not None
    if enabled:
        if use_systemd:
            SYSTEMD_UNIT.parent.mkdir(parents=True, exist_ok=True)
            SYSTEMD_UNIT.write_text(
                '[Unit]\nDescription=TH80 Screen Studio\nAfter=graphical-session.target\n\n'
                f'[Service]\nWorkingDirectory={ROOT}\nExecStart={sys.executable} {ROOT / "app.py"} --quiet\n'
                'Restart=on-failure\n\n[Install]\nWantedBy=default.target\n', encoding='utf-8')
            subprocess.run(['systemctl', '--user', 'daemon-reload'], timeout=15)
            subprocess.run(['systemctl', '--user', 'enable', SYSTEMD_UNIT.name], timeout=15)
        else:
            XDG_AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
            XDG_AUTOSTART.write_text(
                '[Desktop Entry]\nType=Application\nName=TH80 Screen Studio\n'
                f'Exec="{sys.executable}" "{ROOT / "app.py"}" --quiet\nX-GNOME-Autostart-enabled=true\n',
                encoding='utf-8')
    else:
        if SYSTEMD_UNIT.exists():
            subprocess.run(['systemctl', '--user', 'disable', SYSTEMD_UNIT.name], timeout=15)
            SYSTEMD_UNIT.unlink()
            subprocess.run(['systemctl', '--user', 'daemon-reload'], timeout=15)
        if XDG_AUTOSTART.exists():
            XDG_AUTOSTART.unlink()


# --------------------------------------------------------------------- http --

def api_state(_body):
    try:
        dev = device_info() if not job['running'] else {'connected': True, 'busy': True}
    except Exception as e:
        dev = {'connected': False, 'error': str(e)}
    if not dev.get('busy'):
        try:
            dev['seen'] = kb.scan()
        except Exception:
            dev['seen'] = []
    nr = auto_state['next_run']
    return {'device': dev, 'job': job, 'settings': settings, 'startup': startup_enabled(),
            'platform': 'windows' if IS_WINDOWS else 'linux' if IS_LINUX else sys.platform,
            'hotkey_supported': Hotkey.SUPPORTED, 'hotkey_hint': None if Hotkey.SUPPORTED else Hotkey.LINUX_HINT,
            'hotkey_error': hotkey.error, 'auto_next': datetime.fromtimestamp(nr).strftime('%H:%M') if nr else None,
            'auto_error': auto_state['last_error'], 'max_frames': kb.MAX_FRAMES, 'min_every': kb.MIN_STATS_MINUTES}


def api_clock(_body):
    set_mode('clock')
    return {'ok': True, 'message': f'Clock synced to {datetime.now():%H:%M:%S}.'}


def api_mode(body):
    mode = body.get('mode')
    if mode not in ('clock', 'image'):
        raise ValueError('mode must be clock or image')
    set_mode(mode)
    return {'ok': True, 'message': f'Screen switched to {mode}.'}


def api_images(body):
    images = body.get('images') or []
    delay = body.get('delay_ms')

    def frames():
        out = []
        for d in images:
            with decode_image(d) as im:
                out.append(kb.fit(im))
        if len(out) > 1 and not 60 <= int(delay or 0) <= 65535:
            raise ValueError('Slide time must be between 0.06 and 65 seconds.')
        return out, (int(delay) if len(out) > 1 else None)

    start_job('Slideshow' if len(images) > 1 else 'Picture', frames)
    return {'ok': True}


def api_gif(body):
    def frames():
        raw = base64.b64decode(body.get('data', '').split(',', 1)[-1])
        tmp = io.BytesIO(raw)
        return kb.load_gif(tmp)
    start_job('GIF', frames)
    return {'ok': True}


def api_card_preview(body):
    kind = body.get('card')
    return {'image': png_data_url(cards.render(kind, card_opts(kind, body.get('options'))))}


def api_card_upload(body):
    kind = body.get('card')
    opts = card_opts(kind, body.get('options'))
    start_job(f'{kind.capitalize()} card', lambda: ([cards.render(kind, opts).convert('RGB')], None))
    return {'ok': True}


def api_settings(body):
    for key in ('sync_clock_on_start', 'hotkey', 'lock_switch'):
        if key in body:
            settings[key] = bool(body[key])
    if 'auto' in body:
        a = body['auto']
        every = int(a.get('every', settings['auto']['every']))
        if every < kb.MIN_STATS_MINUTES:
            raise ValueError(f'Auto refresh must be at least {kb.MIN_STATS_MINUTES} minutes.')
        if a.get('card', 'stats') not in cards.CARDS:
            raise ValueError('unknown card')
        settings['auto'] = {'enabled': bool(a.get('enabled')), 'card': a.get('card', 'stats'), 'every': every}
        auto_state['next_run'] = None
    if 'cards' in body:
        for kind, opts in body['cards'].items():
            if kind in cards.CARDS and isinstance(opts, dict):
                settings['cards'][kind] = opts
    if 'startup' in body:
        set_startup(bool(body['startup']))
    (hotkey.start if settings['hotkey'] else hotkey.stop)()
    save_settings()
    return {'ok': True, 'hotkey_error': hotkey.error if settings['hotkey'] and Hotkey.SUPPORTED else None}


ROUTES = {('GET', '/api/state'): api_state, ('POST', '/api/clock'): api_clock, ('POST', '/api/mode'): api_mode,
          ('POST', '/api/images'): api_images, ('POST', '/api/gif'): api_gif,
          ('POST', '/api/card/preview'): api_card_preview, ('POST', '/api/card/upload'): api_card_upload,
          ('POST', '/api/settings'): api_settings}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        host = (self.headers.get('Host') or '').split(':')[0]
        if host not in ('127.0.0.1', 'localhost'):
            return self._send(403, {'error': 'forbidden host'})
        path = self.path.split('?')[0]
        if method == 'GET' and path in ('/', '/index.html'):
            return self._send(200, (ROOT / 'static' / 'index.html').read_bytes(), 'text/html; charset=utf-8')
        fn = ROUTES.get((method, path))
        if not fn:
            return self._send(404, {'error': 'not found'})
        if method == 'POST' and self.headers.get('X-TH80') != '1':
            return self._send(403, {'error': 'missing header'})  # blocks cross-site requests
        try:
            length = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(length) or b'{}') if method == 'POST' else {}
            self._send(200, fn(body))
        except (OSError, ValueError, RuntimeError, KeyError) as e:
            self._send(400, {'error': str(e)})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {'error': f'{type(e).__name__}: {e}'})

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')


def main():
    quiet = '--quiet' in sys.argv
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        if not quiet:
            webbrowser.open(f'http://{HOST}:{PORT}/')  # already running
        return
    if settings.get('sync_clock_on_start'):
        try:
            set_mode('clock') if device_info()['mode'] == 'clock' else None
        except Exception:
            pass
    if settings.get('hotkey'):
        hotkey.start()
    threading.Thread(target=auto_loop, daemon=True).start()
    threading.Thread(target=lock_loop, daemon=True).start()
    url = f'http://{HOST}:{PORT}/'
    print(f'TH80 Screen Studio running at {url}  (Ctrl+C to stop)')
    if not quiet:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
