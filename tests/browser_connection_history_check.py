"""Offline UI coverage for transport history. No camera or server writes."""
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
HTML = subprocess.check_output([
    str(ROOT / '.venv/bin/python'), '-c',
    'from unittest.mock import Mock; from industrial_fall.app import create_app; '
    'print(create_app(Mock()).test_client().get("/").get_data(as_text=True))',
], cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')}, text=True)


def row(n, event, transport, **fields):
    result = dict(id=f'{n:032x}', connection_id='a' * 32, timestamp='2026-10-29T07:00:00Z',
                  event=event, transport=transport, source='browser', access='domain_url')
    result.update(fields)
    return result


def main():
    rows = [
        row(1, 'attempt', 'connecting', reason='automatic'),
        row(2, 'whep_accepted', 'webrtc', source='server', http_status=201),
        row(3, 'rtc_playing', 'webrtc', frames_decoded=21, width=1280, height=720,
            local_candidate='host', remote_candidate='relay', protocol='udp', ice_state='connected'),
        row(4, 'ended', 'webrtc', reason='pagehide'),
        row(5, 'whep_failed', 'webrtc', source='server', reason='upstream_http', http_status=503),
        row(6, 'fallback', 'mjpeg', reason='whep_failed'),
        row(7, 'mjpeg_decoded', 'mjpeg', reason='whep_failed'),
        row(8, 'attempt', 'connecting', reason='overlay_selected', overlay=True),
        row(9, 'fallback', 'mjpeg', reason='overlay_selected', overlay=True),
        row(10, 'mjpeg_decoded', 'mjpeg', reason='overlay_selected', overlay=True),
        row(11, 'mjpeg_error', 'mjpeg', reason='stream_error'),
    ]
    fixture = {'events': list(reversed(rows)), 'limit': 200, 'persistent': True,
               'storage_error': False, 'dropped': 0}
    requests, errors = [], []

    def route_request(route):
        path = urlsplit(route.request.url).path
        requests.append((route.request.method, path))
        if path == '/':
            route.fulfill(content_type='text/html', body=HTML)
        elif path == '/api/video-connections':
            route.fulfill(json=fixture)
        elif path.startswith('/static/'):
            name = Path(path).name
            active = name in ('connection-history.js', 'dashboard.css')
            route.fulfill(content_type='text/css' if name.endswith('.css') else 'text/javascript',
                          body=(ROOT / 'src/industrial_fall/static' / name).read_text() if active else '')
        else:
            route.abort()

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage', '--mute-audio'])
        page = browser.new_page(viewport={'width': 1440, 'height': 1050})
        page.route('**/*', route_request)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://industrial.test/')
        page.wait_for_function("document.querySelectorAll('.connection-row').length === 11")
        assert page.locator('#video-log-filter').input_value() == 'all'
        assert 'WebRTC 1 / MJPEG 2건' in page.locator('#video-log-summary').inner_text()
        assert '영상 수신 확인 아님' in page.locator('[data-event="whep_accepted"] strong').inner_text()
        assert '복호화 21프레임' in page.locator('[data-event="rtc_playing"]').inner_text()
        assert 'host ↔ relay / udp' in page.locator('[data-event="rtc_playing"]').inner_text()
        assert 'WebRTC 연결 종료' in page.locator('[data-event="ended"] strong').inner_text()
        page.select_option('#video-log-filter', 'webrtc')
        assert page.locator('.connection-row').count() == 6
        assert page.locator('[data-event="fallback"]').count() == 1
        assert page.locator('[data-event="mjpeg_decoded"]').count() == 0
        page.select_option('#video-log-filter', 'mjpeg')
        assert page.locator('.connection-row').count() == 6
        assert page.locator('[data-event="rtc_playing"]').count() == 0
        page.select_option('#video-log-filter', 'failed')
        assert page.locator('.connection-row').count() == 3
        assert page.locator('[data-failed="false"]').count() == 0
        page.select_option('#video-log-filter', 'all')
        assert page.locator('.connection-row').count() == 11
        for width in (390, 768, 1440):
            page.set_viewport_size({'width': width, 'height': 1050})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        checks = ROOT / 'runtime/checks'
        checks.mkdir(parents=True, exist_ok=True)
        page.locator('#connection-history').screenshot(path=str(checks / 'connection-history-all.png'))
        fixture['events'] = []
        page.reload()
        page.wait_for_function("document.getElementById('video-log-summary').textContent.includes('최근 0/200건')")
        assert page.locator('#video-log-filter').input_value() == 'all'
        assert '이 조건에 해당하는 연결 기록이 없습니다.' in page.locator('#video-log-events').inner_text()
        assert not errors, errors
        assert all(method == 'GET' for method, _ in requests)
        browser.close()
    print('Connection history: all-by-default, RTC/MJPEG/failure filters, receipt vs WHEP, empty state and mobile PASS.')


if __name__ == '__main__':
    main()
