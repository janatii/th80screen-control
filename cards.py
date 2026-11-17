"""240x135 info cards for the TH80 Pro V2 screen."""
import json
import os
import re
import shutil
import socket
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from PIL import Image, ImageDraw, ImageFont

W, H = 240, 135
BG = (12, 14, 20)
FG = (235, 235, 240)
DIM = (130, 135, 150)
TRACK = (40, 44, 56)
GOOD, WARN, BAD = (80, 200, 120), (240, 190, 60), (235, 80, 80)
UA = {'User-Agent': 'th80-screen/1.0'}


# Pillow searches the system font folders for a bare file name (Windows\Fonts, /usr/share/fonts, ...).
FONTS = {False: ('segoeui.ttf', 'arial.ttf', 'DejaVuSans.ttf', 'NotoSans-Regular.ttf',
                 'LiberationSans-Regular.ttf', 'Helvetica.ttc'),
         True: ('segoeuib.ttf', 'arialbd.ttf', 'DejaVuSans-Bold.ttf', 'NotoSans-Bold.ttf',
                'LiberationSans-Bold.ttf', 'Helvetica.ttc')}
_font_cache = {}


def font(size, bold=False):
    key = (size, bold)
    if key not in _font_cache:
        for name in FONTS[bold]:
            try:
                _font_cache[key] = ImageFont.truetype(name, size)
                break
            except OSError:
                pass
        else:
            _font_cache[key] = ImageFont.load_default(size)
    return _font_cache[key]


def _system_disk():
    return os.environ.get('SystemDrive', 'C:') + '\\' if os.name == 'nt' else '/'


def _canvas(title, bg=BG):
    img = Image.new('RGB', (W, H), bg)
    d = ImageDraw.Draw(img)
    if title:
        d.text((10, 7), _clip(d, title, font(14, True), 170), font=font(14, True), fill=FG)
        d.text((W - 10, 9), datetime.now().strftime('%H:%M'), font=font(12), fill=DIM, anchor='ra')
    return img, d


def _clip(d, text, f, width):
    text = str(text)
    if d.textlength(text, font=f) <= width:
        return text
    while text and d.textlength(text + '…', font=f) > width:
        text = text[:-1]
    return text + '…'


def _level(pct):
    return GOOD if pct < 60 else WARN if pct < 85 else BAD


def _rows(d, rows, top=34):
    """rows: (label, value_text, pct_or_None, color_or_None)"""
    small = font(13)
    step = max(18, min(32, (H - top - 4) // max(1, len(rows))))
    y = top
    for label, value, pct, color in rows:
        d.text((10, y), _clip(d, label, small, 120), font=small, fill=(200, 200, 210))
        d.text((W - 10, y), _clip(d, value, small, 105), font=small, fill=color or (200, 200, 210), anchor='ra')
        if pct is not None and step >= 28:
            d.rounded_rectangle((10, y + 18, W - 10, y + 24), 3, fill=TRACK)
            d.rounded_rectangle((10, y + 18, 10 + max(6, int((W - 20) * min(100, pct) / 100)), y + 24), 3,
                                fill=color or _level(pct))
        y += step


def _big_text(d, text, box, color=FG, max_size=40, min_size=12, bold=True):
    """Fit text (wrapped, up to 3 lines) centred inside box=(x0,y0,x1,y1)."""
    x0, y0, x1, y1 = box
    words = str(text).split() or ['']
    for size in range(max_size, min_size - 1, -2):
        f = font(size, bold)
        lines, line = [], ''
        for w in words:
            test = (line + ' ' + w).strip()
            if d.textlength(test, font=f) <= x1 - x0:
                line = test
            else:
                if line:
                    lines.append(line)
                line = w
        lines.append(line)
        lh = size * 1.2
        if len(lines) <= 3 and all(d.textlength(l, font=f) <= x1 - x0 for l in lines) and lh * len(lines) <= y1 - y0:
            y = y0 + ((y1 - y0) - lh * len(lines)) / 2
            for l in lines:
                d.text(((x0 + x1) / 2, y + lh / 2), l, font=f, fill=color, anchor='mm')
                y += lh
            return
    d.text(((x0 + x1) / 2, (y0 + y1) / 2), _clip(d, text, font(min_size, bold), x1 - x0),
           font=font(min_size, bold), fill=color, anchor='mm')


def _hex(color, default=(200, 40, 40)):
    m = re.fullmatch(r'#?([0-9a-fA-F]{6})', str(color or ''))
    return tuple(int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4)) if m else default


# ------------------------------------------------------------------ cards --

def stats_card(_opts=None):
    import psutil
    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    disk = shutil.disk_usage(_system_disk())
    img, d = _canvas(socket.gethostname().split('.')[0] or 'PC')
    _rows(d, [('CPU', f'{cpu:.0f}%', cpu, None),
              ('RAM', f'{ram.used / 2**30:.1f}/{ram.total / 2**30:.0f} GB', ram.percent, None),
              ('DISK', f'{disk.free / 2**30:.0f} GB free', disk.used * 100 / disk.total, None)])
    return img


def _check(target, timeout=1.5):
    host, _, port = target.strip().rpartition(':')
    if not host or not port.isdigit():
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def homelab_card(opts):
    """opts['hosts']: list of {'name', 'target': 'host:port'}"""
    hosts = [h for h in (opts or {}).get('hosts', []) if h.get('name') and h.get('target')][:8]
    with ThreadPoolExecutor(8) as pool:
        up = list(pool.map(lambda h: _check(h['target']), hosts))
    img, d = _canvas(f'Homelab {sum(up)}/{len(hosts)} up' if hosts else 'Homelab')
    if not hosts:
        _big_text(d, 'Add servers in the app', (10, 30, W - 10, H - 6), DIM, 20)
        return img
    f = font(12)
    cols = 2 if len(hosts) > 4 else 1
    per_col = -(-len(hosts) // cols)
    colw = (W - 20) // cols
    for i, (h, ok) in enumerate(zip(hosts, up)):
        x = 10 + (i // per_col) * colw
        y = 34 + (i % per_col) * 24
        d.ellipse((x, y + 4, x + 9, y + 13), fill=GOOD if ok else BAD)
        d.text((x + 15, y), _clip(d, h['name'], f, colw - 22), font=f, fill=FG if ok else (220, 150, 150))
    return img


WMO = {0: 'Clear', 1: 'Mostly clear', 2: 'Partly cloudy', 3: 'Overcast', 45: 'Fog', 48: 'Fog',
       51: 'Drizzle', 53: 'Drizzle', 55: 'Drizzle', 56: 'Freezing drizzle', 57: 'Freezing drizzle',
       61: 'Light rain', 63: 'Rain', 65: 'Heavy rain', 66: 'Freezing rain', 67: 'Freezing rain',
       71: 'Light snow', 73: 'Snow', 75: 'Heavy snow', 77: 'Snow grains', 80: 'Showers', 81: 'Showers',
       82: 'Heavy showers', 85: 'Snow showers', 86: 'Snow showers', 95: 'Thunderstorm',
       96: 'Thunderstorm', 99: 'Thunderstorm'}


def _get_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10) as r:
        return json.load(r)


def weather_card(opts):
    city = (opts or {}).get('city', '').strip()
    if not city:
        raise ValueError('Enter a city for the weather card.')
    geo = _get_json('https://geocoding-api.open-meteo.com/v1/search?count=1&name=' + urllib.parse.quote(city))
    if not geo.get('results'):
        raise ValueError(f'City not found: {city}')
    place = geo['results'][0]
    q = urllib.parse.urlencode({
        'latitude': place['latitude'], 'longitude': place['longitude'], 'timezone': 'auto', 'forecast_days': 1,
        'current': 'temperature_2m,weather_code,wind_speed_10m',
        'daily': 'temperature_2m_max,temperature_2m_min,precipitation_probability_max'})
    w = _get_json('https://api.open-meteo.com/v1/forecast?' + q)
    cur, day = w['current'], w['daily']
    img, d = _canvas(place['name'])
    d.text((10, 30), f"{cur['temperature_2m']:.0f}°", font=font(46, True), fill=FG)
    d.text((W - 10, 40), _clip(d, WMO.get(cur['weather_code'], '—'), font(15, True), 120),
           font=font(15, True), fill=FG, anchor='ra')
    d.text((W - 10, 62), f"wind {cur['wind_speed_10m']:.0f} km/h", font=font(12), fill=DIM, anchor='ra')
    rain = day['precipitation_probability_max'][0]
    d.text((10, 104), f"↑ {day['temperature_2m_max'][0]:.0f}°   ↓ {day['temperature_2m_min'][0]:.0f}°",
           font=font(14), fill=(200, 200, 210))
    d.text((W - 10, 104), f"rain {rain if rain is not None else 0}%", font=font(14), fill=(120, 170, 240), anchor='ra')
    return img


def _ics_time(value, params):
    value = value.strip()
    if re.fullmatch(r'\d{8}', value):
        return datetime.strptime(value, '%Y%m%d'), True
    dt = datetime.strptime(value[:15], '%Y%m%dT%H%M%S')
    if value.endswith('Z'):
        dt = dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return dt, False  # TZID times are treated as local time


def next_events(ics_text, count=3):
    text = re.sub(r'\r?\n[ \t]', '', ics_text)
    now = datetime.now()
    events = []
    for block in re.findall(r'BEGIN:VEVENT(.*?)END:VEVENT', text, re.S):
        props = {}
        for line in block.splitlines():
            if ':' in line:
                key, value = line.split(':', 1)
                name, *params = key.split(';')
                props.setdefault(name.upper(), (value, params))
        if 'DTSTART' not in props:
            continue
        try:
            start, all_day = _ics_time(*props['DTSTART'])
        except ValueError:
            continue
        end = start + timedelta(days=1) if all_day else start
        if end >= now:
            summary = props.get('SUMMARY', ('(no title)', []))[0].replace('\\,', ',').replace('\\n', ' ')
            events.append((start, all_day, summary))
    return sorted(events)[:count]


def calendar_card(opts):
    url = (opts or {}).get('ics_url', '').strip()
    if not url.lower().startswith(('https://', 'http://', 'webcal://')):
        raise ValueError('Enter an iCal (.ics) link for the calendar card.')
    url = re.sub(r'^webcal://', 'https://', url, flags=re.I)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
        events = next_events(r.read().decode('utf-8', 'replace'))
    img, d = _canvas('Next up')
    if not events:
        _big_text(d, 'Nothing scheduled', (10, 30, W - 10, H - 6), DIM, 22)
        return img
    today = date.today()
    rows = []
    for start, all_day, summary in events:
        when = ('today' if start.date() == today else 'tomorrow' if start.date() == today + timedelta(days=1)
                else start.strftime('%a %d %b'))
        rows.append((summary, when if all_day else f"{when} {start:%H:%M}", None, None))
    _rows(d, rows, top=34)
    return img


def daily_card(opts):
    opts = opts or {}
    title = opts.get('title', '').strip()
    text = opts.get('text', '').strip()
    target = opts.get('countdown', '').strip()
    img, d = _canvas(title or datetime.now().strftime('%A %d %B'))
    if target:
        days = (date.fromisoformat(target) - date.today()).days
        big = 'Today!' if days == 0 else f'{abs(days)} day{"s" if abs(days) != 1 else ""}' + (' ago' if days < 0 else '')
        _big_text(d, big, (10, 30, W - 10, 92), FG, 40)
        _big_text(d, text or f'until {target}', (10, 94, W - 10, H - 6), DIM, 16, bold=False)
    else:
        _big_text(d, text or 'Have a good day', (10, 30, W - 10, H - 6), FG, 30)
    return img


def status_card(opts):
    opts = opts or {}
    color = _hex(opts.get('color'), (200, 40, 40))
    img = Image.new('RGB', (W, H), color)
    d = ImageDraw.Draw(img)
    fg = (20, 20, 24) if sum(color) > 450 else (255, 255, 255)
    sub = opts.get('subtext', '').strip()
    _big_text(d, opts.get('text', 'In a meeting') or ' ', (12, 10, W - 12, H - (40 if sub else 10)), fg, 44)
    if sub:
        _big_text(d, sub, (12, H - 38, W - 12, H - 8), fg, 18, bold=False)
    return img


CARDS = {'stats': stats_card, 'homelab': homelab_card, 'weather': weather_card,
         'calendar': calendar_card, 'daily': daily_card, 'status': status_card}


def render(kind, opts=None):
    if kind not in CARDS:
        raise ValueError(f'unknown card: {kind}')
    return CARDS[kind](opts or {})
