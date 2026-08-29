import os
import json
import urllib.request
import urllib.parse
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

# ── 配置 ──
# Wasmer 只上报 CF Worker，不再直接调用旧 wxsend，避免重复天气卡。
CARD_BASE = 'https://skin.2088x.com/'
WORKER_URL = os.environ.get('WORKER_URL', 'https://skin.2088x.com/weather')
PLATFORM_KEY = os.environ.get('PLATFORM_KEY', 'wasmer')
PLATFORM_FLAG = os.environ.get('PLATFORM_FLAG', '🌐')

CITIES = {
    '花莲': {'lat': '23.98', 'lon': '121.60', 'tz': 'Asia/Taipei', 'off': 8},
    '广州': {'lat': '23.13', 'lon': '113.26', 'tz': 'Asia/Shanghai', 'off': 8},
    '大阪': {'lat': '34.69', 'lon': '135.50', 'tz': 'Asia/Tokyo', 'off': 9},
    '奥伊米亚康': {'lat': '63.46', 'lon': '142.79', 'tz': 'Asia/Yakutsk', 'off': 9},
}

WMO = {
    0: ['晴', '☀️'], 1: ['大体晴', '🌤️'], 2: ['多云', '⛅'], 3: ['阴', '☁️'],
    45: ['雾', '🌫️'], 48: ['冻雾', '🌫️'],
    51: ['毛毛雨', '🌦️'], 53: ['中毛毛雨', '🌦️'], 55: ['密毛毛雨', '🌧️'],
    56: ['冻毛毛雨', '🌧️'], 57: ['密冻毛毛雨', '🌧️'],
    61: ['小雨', '🌧️'], 63: ['中雨', '🌧️'], 65: ['大雨', '🌧️'],
    66: ['冻雨', '🌧️'], 67: ['密冻雨', '🌧️'],
    71: ['小雪', '🌨️'], 73: ['中雪', '🌨️'], 75: ['大雪', '🌨️'], 77: ['雪粒', '🌨️'],
    80: ['阵雨', '🌦️'], 81: ['中阵雨', '🌧️'], 82: ['暴雨', '⛈️'],
    85: ['阵雪', '🌨️'], 86: ['大阵雪', '🌨️'],
    95: ['雷暴', '⛈️'], 96: ['雷暴+冰雹', '⛈️'], 99: ['强雷暴+冰雹', '⛈️'],
}


UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'


def http_json(url, timeout=15):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_weather(city):
    """返回 (normalized, source)；normalized 统一为 Open-Meteo 风格字段"""
    c = CITIES[city]
    api = ('https://api.open-meteo.com/v1/forecast?latitude=' + c['lat'] +
           '&longitude=' + c['lon'] +
           '&current=temperature_2m,relative_humidity_2m,apparent_temperature,weathercode,windspeed_10m,winddirection_10m,uv_index'
           '&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max'
           '&timezone=' + urllib.parse.quote(c['tz']) + '&forecast_days=1')
    try:
        d = http_json(api)
        if not d.get('error'):
            return d, 'Open-Meteo'
    except Exception:
        pass
    # wttr.in 兜底 → 归一化成 Open-Meteo 形状
    try:
        d = http_json('https://wttr.in/' + urllib.parse.quote(city) + '?format=j1')
        cc = d['current_condition'][0]
        w0 = d['weather'][0]
        rain = max(int(h.get('chanceofrain', 0)) for h in w0.get('hourly', [{}]))
        norm = {
            'current': {
                'temperature_2m': cc['temp_C'],
                'apparent_temperature': cc.get('FeelsLikeC', cc['temp_C']),
                'relative_humidity_2m': cc.get('humidity', ''),
                'weathercode': int(cc.get('weatherCode', 0)),
                'windspeed_10m': cc.get('windspeedKmph', ''),
                'weatherDesc': cc['weatherDesc'][0]['value'],
            },
            'daily': {
                'temperature_2m_max': [w0['maxtempC']],
                'temperature_2m_min': [w0['mintempC']],
                'precipitation_probability_max': [rain],
            },
        }
        return norm, 'wttr.in'
    except Exception:
        return None, 'none'


def build_push(city, data, source):
    cur = data['current']
    daily = data['daily']
    temp = cur['temperature_2m']
    feels = cur.get('apparent_temperature', '')
    humidity = cur.get('relative_humidity_2m', '')
    wind = cur.get('windspeed_10m', '')
    wcode = int(cur.get('weathercode', 0))
    tmax = daily['temperature_2m_max'][0]
    tmin = daily['temperature_2m_min'][0]
    rain = daily['precipitation_probability_max'][0]

    if source == 'wttr.in':
        wdesc = cur.get('weatherDesc', '未知')
        wemoji = '🌡️'
    else:
        wmo = WMO.get(wcode, ['未知', '🌡️'])
        wdesc, wemoji = wmo[0], wmo[1]

    now = datetime.now(timezone.utc) + timedelta(hours=CITIES[city]['off'])
    date_str = now.strftime('%Y-%m-%d')

    title = '🌤 ' + city + '天气 ' + date_str + ' ' + str(temp) + '°C (wasmer验收)'

    content = (
        '📍 ' + city + '天气 ' + date_str + '\n\n' +
        wemoji + ' 天气：' + wdesc + '\n' +
        '🌡️ 当前：' + str(temp) + '°C（体感 ' + str(feels) + '°C）\n' +
        '📊 范围：' + str(tmin) + '°C ~ ' + str(tmax) + '°C\n' +
        '💧 湿度：' + str(humidity) + '%\n' +
        '🌬️ 风力：' + str(wind) + ' km/h\n' +
        '🌧️ 降水概率：' + str(rain) + '%\n\n' +
        '数据来源：' + source
    )

    qs = urllib.parse.urlencode({
        'city': city, 'date': date_str, 'wemoji': wemoji,
        'temp': temp, 'feels': feels, 'tmax': tmax, 'tmin': tmin,
        'humidity': humidity, 'wind': wind, 'rain': rain, 'source': source,
    })
    card_url = CARD_BASE + '?' + qs
    return title, content, card_url


def post_to_worker(city, data, source):
    cur, daily = data['current'], data['daily']
    wcode = int(cur.get('weathercode', 0))
    desc = cur.get('weatherDesc') if source == 'wttr.in' else WMO.get(wcode, ['未知'])[0]
    payload = {
        'platform': PLATFORM_KEY, 'city': city, 'flag': PLATFORM_FLAG, 'name': PLATFORM_KEY,
        'temp': float(cur['temperature_2m']), 'desc': desc,
        'humidity': float(cur.get('relative_humidity_2m', 0) or 0),
        'wind': float(cur.get('windspeed_10m', 0) or 0),
        'rain': float(daily['precipitation_probability_max'][0] or 0), 'source': source,
    }
    req = urllib.request.Request(WORKER_URL, data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json', 'User-Agent': UA}, method='POST')
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, r.read().decode()[:200]


def push_wxsend(title, content, card_url):
    body = json.dumps({
        'token': WX_TOKEN, 'title': title,
        'content': content, 'card_url': card_url,
    }).encode('utf-8')
    req = urllib.request.Request(
        WX_URL, data=body,
        headers={'Content-Type': 'application/json', 'User-Agent': UA},
        method='POST')
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, r.read().decode()[:300]


def report_worker(city='奥伊米亚康'):
    data, source = fetch_weather(city)
    if not data:
        print(f'[wasmer] weather fetch failed: {city}', flush=True)
        return False
    try:
        status, resp = post_to_worker(city, data, source)
        print(f'[wasmer] worker report {city}: HTTP {status} {resp}', flush=True)
        return 200 <= status < 300
    except Exception as e:
        print(f'[wasmer] worker report failed: {type(e).__name__}: {e}', flush=True)
        return False


def report_loop():
    # 启动即补报一次；之后每20分钟心跳/天气上报，确保08:00前KV有今日数据。
    while True:
        report_worker('奥伊米亚康')
        time.sleep(1200)


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        self.send_response(code)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode('utf-8'))

    def do_GET(self):
        path, _, query = self.path.partition('?')
        qs = urllib.parse.parse_qs(query)

        if path.startswith('/push'):
            city = qs.get('city', ['花莲'])[0]
            if city not in CITIES:
                self._json(400, {'ok': False, 'error': 'unknown city: ' + city,
                                 'supported': list(CITIES)})
                return
            try:
                data, source = fetch_weather(city)
                if not data:
                    self._json(502, {'ok': False, 'error': 'all weather sources failed'})
                    return
                worker_status, worker_resp = post_to_worker(city, data, source)
                self._json(200, {'ok': 200 <= worker_status < 300, 'city': city, 'source': source,
                                 'worker_status': worker_status, 'worker_resp': worker_resp,
                                 'message': '已上报 CF Worker；由 Worker 统一聚合推送'})
            except Exception as e:
                self._json(500, {'ok': False, 'err': type(e).__name__ + ': ' + str(e)})
        elif path.startswith('/debug'):
            out = {}
            try:
                req = urllib.request.Request('https://ts.2088x.com/',
                                             headers={'User-Agent': UA})
                with urllib.request.urlopen(req, timeout=15) as r:
                    out['get_home'] = [r.status, r.read(150).decode('utf-8', 'ignore')]
            except urllib.error.HTTPError as e:
                out['get_home'] = ['HTTPError', e.code, e.read(200).decode('utf-8', 'ignore')]
            except Exception as e:
                out['get_home'] = [type(e).__name__, str(e)]
            try:
                body = json.dumps({'token': 'debug-invalid-token',
                                   'title': 'debug', 'content': 'debug'}).encode()
                req = urllib.request.Request(
                    WX_URL, data=body,
                    headers={'Content-Type': 'application/json', 'User-Agent': UA},
                    method='POST')
                with urllib.request.urlopen(req, timeout=15) as r:
                    out['post_badtoken'] = [r.status, r.read(150).decode('utf-8', 'ignore')]
            except urllib.error.HTTPError as e:
                out['post_badtoken'] = ['HTTPError', e.code, e.read(250).decode('utf-8', 'ignore')]
            except Exception as e:
                out['post_badtoken'] = [type(e).__name__, str(e)]
            self._json(200, out)
        elif path.startswith('/test-net'):
            try:
                r = http_json('https://api.ipify.org?format=json')
                self._json(200, {'net': 'ok', 'resp': r})
            except Exception as e:
                self._json(200, {'net': 'fail', 'err': type(e).__name__ + ': ' + str(e)})
        else:
            self._json(200, {'status': 'ok', 'app': 'wx-node-test',
                             'routes': ['/push?city=花莲|广州|大阪', '/test-net']})


if __name__ == '__main__':
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', 80))
    server = HTTPServer((host, port), Handler)
    print(f'Starting server on http://{host}:{port}', flush=True)
    threading.Thread(target=report_loop, daemon=True, name='worker-weather-report').start()
    server.serve_forever()
