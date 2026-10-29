"""Optional Playwright + system Chromium smoke test; never emits real events."""
import argparse
import base64
import time
from pathlib import Path
from playwright.sync_api import sync_playwright


def wait_receipt(context, page, url, cid, events):
    # Assert persisted server evidence, not truthiness of a JS Promise in polling.
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        history = context.request.get(url + '/api/video-connections', timeout=4000).json()['events']
        receipts = [e for e in history if e['connection_id'] == cid and e['event'] in events]
        if receipts:
            return receipts
        page.wait_for_timeout(500)
    raise AssertionError(f'No persisted video receipt for {cid}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--check-audio", action="store_true", help="Check natural clip playback with physical output muted")
    args = parser.parse_args()
    Path("runtime/checks").mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/usr/bin/chromium", headless=True,
                                    args=["--no-sandbox", "--disable-dev-shm-usage", "--mute-audio"])
        context = browser.new_context(viewport={"width": 1440, "height": 1050})
        page = context.new_page()
        errors = []
        sessions = []
        def remember_session(request):
            if request.method == 'POST' and '/api/video-connections' in request.url:
                data = request.post_data_json
                if data.get('event') == 'attempt':
                    sessions.append(data['connection_id'])
        page.on('request', remember_session)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_function("document.getElementById('connection').textContent.includes('AI 분석 중')", timeout=25000)
        page.wait_for_function("(document.querySelector('video')?.readyState >= 2) || document.querySelector('.stream').naturalWidth > 100", timeout=25000)
        print("Default transport:", page.locator('.transport-label').inner_text())
        if args.check_audio:
            assert page.locator('#audio-voice').input_value() == 'natural'
            assert page.locator('#audio-voice option').count() == 2
            page.click('#audio-test')
            page.wait_for_function("document.getElementById('audio-status').textContent.includes('완료 · 안내형')", timeout=10000)
            assert page.locator('#audio-enable').get_attribute('aria-pressed') == 'false'
            page.locator('.alert-controls').screenshot(path='runtime/checks/voice-select.png')
            print('Natural MP3 playback on deployed origin: OK (physical output muted)')
        cid = sessions[-1]
        receipts = wait_receipt(context, page, args.url, cid, {'rtc_playing','mjpeg_decoded'})
        print('Default receipt:', [{k:v for k,v in e.items() if k in ('event','frames_decoded','local_candidate','remote_candidate','protocol','access')} for e in receipts])
        assert page.locator('#video-log-filter').input_value() == 'all'
        rtc_received = any(e['event'] == 'rtc_playing' for e in receipts)
        if rtc_received:
            receipt_row = page.locator(f'.connection-row[data-connection-id="{cid}"][data-event="rtc_playing"]')
            receipt_row.wait_for(state='attached', timeout=12000)
            page.select_option('#video-log-filter', 'webrtc')
            assert receipt_row.count() == 1
            assert '복호화' in receipt_row.inner_text()
            page.select_option('#video-log-filter', 'mjpeg')
            assert receipt_row.count() == 0
            page.select_option('#video-log-filter', 'all')
            print('Actual RTC receipt visible in default and WebRTC history: OK')
        page.select_option('#stream-mode', 'overlay')
        if rtc_received:
            ended = wait_receipt(context, page, args.url, cid, {'ended'})
            assert any(e['transport'] == 'webrtc' for e in ended)
        page.wait_for_function("document.querySelector('.stream').naturalWidth > 100 && document.querySelector('.stream').src.includes('overlay=true')")
        assert page.locator('video').count() == 0
        page.screenshot(path="runtime/checks/industrial-monitor.png", full_page=True)
        print("Annotated display: OK")
        page.select_option('#theme', 'light')
        assert page.locator('html').get_attribute('data-theme') == 'light'
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path="runtime/checks/industrial-mobile.png", full_page=True)
        page.goto('about:blank')
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_function("document.documentElement.dataset.theme === 'light'")
        context.close()

        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.route('**/api/webrtc/dahua/whep*', lambda route: route.fulfill(status=503, body='unavailable'))
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_function("document.querySelector('.stream').naturalWidth > 100", timeout=25000)
        assert 'MJPEG' in page.locator('.transport-label').inner_text()
        assert 'overlay=false' in page.locator('.stream').get_attribute('src')
        from urllib.parse import urlparse, parse_qs
        cid = parse_qs(urlparse(page.locator('.stream').get_attribute('src')).query)['cid'][0]
        wait_receipt(context, page, args.url, cid, {'mjpeg_decoded'})
        history = context.request.get(args.url + '/api/video-connections').json()['events']
        session = [e for e in history if e['connection_id'] == cid]
        assert any(e['event'] == 'fallback' and e['reason'] == 'whep_failed' for e in session)
        assert not any(e['event'] == 'rtc_playing' for e in session)
        print("Persistent fallback history / actual MJPEG receipt: OK")
        page.select_option('#video-log-filter', 'webrtc')
        page.locator(f'.connection-row[data-connection-id="{cid}"][data-event="fallback"]').wait_for(state='attached', timeout=12000)
        assert page.locator('.connection-row[data-event="mjpeg_decoded"]').count() == 0
        page.select_option('#video-log-filter', 'all')
        print("WebRTC failure -> clean MJPEG: OK")
        # Browser-local fixtures only. No mutation API or native alarm is invoked.
        simulated = context.request.get(args.url + '/api/status').json()
        simulated.update(analysis_online=True, error=None, fall_state='fallen')
        context.route('**/api/status', lambda route: route.fulfill(json=simulated))
        event = {"id": 'a'*32, "type": "fall_confirmed", "timestamp": "2026-10-29T05:00:00Z",
                 "track": {"track_id": 7, "temporal_probability": .9},
                 "snapshot_url": '/api/events/' + 'a'*32 + '/snapshot.jpg'}
        context.route('**/api/events', lambda route: route.fulfill(json=[event]))
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jupsAAAAASUVORK5CYII=')
        context.route('**/snapshot.jpg', lambda route: route.fulfill(content_type='image/png', body=png))
        page.wait_for_function("document.getElementById('alarm').dataset.state === 'fallen'")
        assert '현장 확인 필요' in page.locator('#alarm-title').inner_text()
        assert page.locator('#video-alarm').is_visible()
        assert page.locator('#video-alarm-title').inner_text() == '낙상 감지'
        assert page.locator('#video-alarm').evaluate('el => getComputedStyle(el).backgroundColor') == 'rgb(195, 38, 52)'
        assert page.get_by_text('현재 AI 판정', exact=True).count() == 0
        warning = page.locator('#video-alarm').bounding_box()
        video = page.locator('.video-wrap').bounding_box()
        assert 0 < warning['y'] - video['y'] <= 20
        page.get_by_role('button', name='낙상 순간 캡처 확인').click()
        assert page.locator('#evidence').is_visible()
        page.keyboard.press('Escape')
        assert not page.locator('#evidence').is_visible()
        print("Fall alert / evidence dialog (browser-local fixture): OK")
        assert not errors, errors
        browser.close()
        print("Browser checks passed; no real fall event was generated.")


if __name__ == "__main__":
    main()
