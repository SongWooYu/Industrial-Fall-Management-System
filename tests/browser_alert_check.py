"""Deterministic offline browser test. No camera, real alarm, TTS or network writes.

Run with a Python environment containing Playwright and system Chromium.
The application venv renders the real template without starting a pipeline.
"""
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
HTML = subprocess.check_output([
    str(ROOT / '.venv/bin/python'), '-c',
    'from unittest.mock import Mock; from industrial_fall.app import create_app; '
    'print(create_app(Mock()).test_client().get("/").get_data(as_text=True))',
], cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')}, text=True)

MOCK_AUDIO = """
window.audioCalls = []; window.clipCalls = []; window.tones = 0; window.cancellations = 0;
window.clipStops = 0; window.clipDecodeHang = false;
window.voices = [{name:'Korean test voice',lang:'ko-KR',localService:true}];
window.speechFailure = false; window.speechHang = false;
const synthesis = new EventTarget();
synthesis.getVoices = () => window.voices;
synthesis.cancel = () => { window.cancellations++; };
synthesis.speak = utterance => {
  window.audioCalls.push({text:utterance.text,volume:utterance.volume,lang:utterance.lang});
  if (window.speechHang) return;
  if (window.speechFailure) { utterance.onerror?.({error:'synthesis-failed'}); return; }
  utterance.onstart?.(); setTimeout(() => utterance.onend?.(), 1);
};
Object.defineProperty(window, 'speechSynthesis', {value:synthesis});
window.SpeechSynthesisUtterance = class {constructor(text) {this.text=text;}};
window.AudioContext = class {
  constructor() {this.state='suspended'; this.currentTime=0; this.destination={};}
  async resume() {this.state='running';}
  async decodeAudioData(data) {if(window.clipDecodeHang) return new Promise(()=>{});return {duration:2.88};}
  createBufferSource() {return {connect(node){this.gain=node.gain;},disconnect(){},
    start(){window.clipCalls.push({volume:this.gain.value});this.timer=setTimeout(()=>this.onended?.(),5);},
    stop(){window.clipStops++;clearTimeout(this.timer);this.onended?.();}};}
  createGain() {return {gain:{setValueAtTime(){},linearRampToValueAtTime(){}},connect(){},disconnect(){}};}
  createOscillator() {return {frequency:{setValueAtTime(){}},connect(){},disconnect(){},start(){window.tones++;},stop(){}};}
};
"""


def event(n, kind):
    return {'id': f'{n:032x}', 'type': kind, 'timestamp': '2026-10-29T06:00:00Z',
            'track': {'track_id': 1, 'temporal_probability': .9}}


def main():
    checks = ROOT / 'runtime/checks'
    checks.mkdir(parents=True, exist_ok=True)
    fixture = {'state': 'normal', 'online': True, 'events': [event(1, 'fall_confirmed')],
               'status_failure': False, 'clip_failure': False}
    requests, errors = [], []

    def route_request(route):
        request = route.request
        path = urlsplit(request.url).path
        requests.append((request.method, path))
        if path == '/':
            route.fulfill(content_type='text/html', body=HTML)
        elif path.startswith('/static/audio/'):
            if fixture['clip_failure']:
                route.fulfill(status=404, body='audio unavailable')
            else:
                route.fulfill(content_type='audio/mpeg', body=(ROOT / 'src/industrial_fall/static/audio' / Path(path).name).read_bytes())
        elif path.startswith('/static/'):
            name = Path(path).name
            # Keep tests isolated from live video, WHEP and connection telemetry.
            if name in ('stream-player.js', 'connection-history.js'):
                route.fulfill(content_type='text/javascript', body='')
            else:
                route.fulfill(content_type='text/css' if name.endswith('.css') else 'text/javascript',
                              body=(ROOT / 'src/industrial_fall/static' / name).read_text())
        elif path == '/api/status':
            if fixture['status_failure']:
                route.fulfill(status=503, body='offline')
                return
            route.fulfill(json={
                'analysis_online': fixture['online'], 'fall_state': fixture['state'],
                'error': None, 'person_count': 1, 'analysis_fps': 15.0,
                'inference_ms': 14.0, 'frame_age_ms': 60,
                'analysis': {'label': '움직임 분석 중', 'detail': '브라우저 전용 테스트 자료입니다.',
                             'motion_score': .9, 'decision_label': '테스트 판정'},
            })
        elif path == '/api/events':
            route.fulfill(json=fixture['events'])
        else:
            route.abort()

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage', '--mute-audio'])
        context = browser.new_context(viewport={'width': 1440, 'height': 1050})
        context.add_init_script(MOCK_AUDIO)
        context.route('**/*', route_request)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://industrial.test/')
        page.wait_for_function("document.getElementById('video-alarm').dataset.state === 'normal'")
        assert page.locator('#video-alarm').is_hidden(), 'Normal must not cover the image'
        assert page.locator('#video-alarm-title').text_content() == ''
        assert page.locator('#fall-notice').is_visible()
        assert page.evaluate('audioCalls.length') == 0, 'Old history must be silent'
        assert page.locator('#audio-voice').input_value() == 'natural'
        page.select_option('#audio-voice', 'browser')

        page.click('#audio-test')
        page.wait_for_function('audioCalls.length === 1')
        assert page.evaluate('audioCalls[0].text') == '낙상이 감지되었습니다.'
        assert page.locator('#audio-enable').get_attribute('aria-pressed') == 'false'
        assert page.locator('#event-count').inner_text() == '1'
        assert all(method == 'GET' for method, _ in requests), 'Sound test must be read-only'
        page.click('#acknowledge-fall')
        assert not page.locator('#fall-notice').is_visible()
        page.reload()
        page.wait_for_function("document.getElementById('video-alarm').dataset.state === 'normal'")
        assert not page.locator('#fall-notice').is_visible(), 'Tab acknowledgment survives reload'
        assert page.evaluate('audioCalls.length') == 0
        assert page.locator('#audio-voice').input_value() == 'browser'
        page.click('#audio-enable')
        page.wait_for_function('audioCalls.length === 1')
        page.wait_for_timeout(1100)
        assert page.evaluate('audioCalls.length') == 1, 'Enable must not speak old falls'

        for n, state, kind, caption in [
            (2, 'suspected', 'fall_suspected', '낙상 의심'),
            (3, 'fallen', 'fall_confirmed', '낙상 감지'),
            (4, 'recovering', 'fall_recovering', '낙상 회복 중'),
            (5, 'normal', 'fall_cleared', ''),
            (6, 'unobserved', 'tracking_lost', ''),
        ]:
            fixture['state'] = state
            fixture['events'].insert(0, event(n, kind))
            page.wait_for_function('(s) => document.getElementById("video-alarm").dataset.state === s', arg=state)
            assert page.locator('#video-alarm-title').text_content() == caption
            badge = page.locator('#video-alarm')
            if state in ('suspected', 'fallen', 'recovering'):
                assert badge.is_visible()
                expected_colors = {'suspected': 'rgb(246, 195, 67)',
                                   'fallen': 'rgb(195, 38, 52)',
                                   'recovering': 'rgb(9, 100, 129)'}
                assert badge.evaluate('el => getComputedStyle(el).backgroundColor') == expected_colors[state]
                assert page.locator('#video-alarm-title').evaluate('el => parseFloat(getComputedStyle(el).fontSize)') >= 28
                image = page.locator('.video-wrap').bounding_box()
                bounds = badge.bounding_box()
                assert 0 < bounds['y'] - image['y'] <= 20, 'Alert belongs at the top of the video'
                page.locator('.video-wrap').screenshot(path=str(checks / f'alert-{state}.png'))
            else:
                assert badge.is_hidden(), 'No video card for normal/no-track states'
            if state in ('fallen', 'recovering', 'normal', 'unobserved'):
                assert page.locator('#fall-notice').is_visible()
        assert page.evaluate('audioCalls.length') == 2, 'Only the new confirmed event speaks'
        page.wait_for_timeout(2100)
        assert page.evaluate('audioCalls.length') == 2, 'Repeated polls must not repeat speech'
        assert page.locator('.state-legend .active').count() == 0

        # A transition that clears between polls must still leave a visible record
        # and announce its new confirmation, without mislabelling current state.
        fixture['state'] = 'normal'
        fixture['events'][0:0] = [event(8, 'fall_cleared'), event(7, 'fall_confirmed')]
        page.wait_for_function('audioCalls.length === 3')
        assert page.locator('#video-alarm').is_hidden()
        assert page.locator('#fall-notice').is_visible()
        page.click('#acknowledge-fall')
        fixture['online'] = False
        page.wait_for_function("document.getElementById('video-alarm').dataset.state === 'offline'")
        assert page.locator('#video-alarm').is_hidden()
        fixture['status_failure'] = True
        page.wait_for_function("document.getElementById('alarm-title').textContent === '서버 연결 끊김'")
        assert page.locator('#video-alarm').is_hidden()
        fixture['status_failure'] = False
        fixture['online'] = True

        # Hidden tabs rebaseline; do not speak the backlog when returning.
        page.evaluate("Object.defineProperty(document,'hidden',{value:true,configurable:true}); document.dispatchEvent(new Event('visibilitychange'))")
        fixture['events'].insert(0, event(9, 'fall_confirmed'))
        page.wait_for_timeout(1100)
        page.evaluate("Object.defineProperty(document,'hidden',{value:false,configurable:true}); document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_function("document.getElementById('event-count').textContent === '9'")
        assert page.evaluate('audioCalls.length') == 3
        page.click('#audio-enable')  # Off.
        fixture['events'].insert(0, event(10, 'fall_confirmed'))
        page.wait_for_function("document.getElementById('event-count').textContent === '10'")
        assert page.evaluate('audioCalls.length') == 3

        page.eval_on_selector('#audio-volume', "el => {el.value=40;el.dispatchEvent(new Event('input'));}")
        page.click('#audio-test')
        assert page.evaluate('audioCalls.at(-1).volume') == .4
        assert page.locator('#audio-volume-value').inner_text() == '40%'
        page.evaluate('window.voices=[]')
        page.click('#audio-test')
        page.wait_for_function('tones === 1')
        assert '한국어 음성' in page.locator('#audio-status').inner_text()
        page.evaluate("window.voices=[{name:'Korean test voice',lang:'ko-KR',localService:true}];window.speechFailure=true")
        page.click('#audio-test')
        page.wait_for_function('tones === 2')
        assert '음성 재생 실패' in page.locator('#audio-status').inner_text()
        page.evaluate('window.speechFailure=false;window.speechHang=true')
        page.click('#audio-test')
        page.wait_for_function('tones === 3', timeout=6000)
        assert '응답 없음' in page.locator('#audio-status').inner_text()
        calls = page.evaluate('audioCalls.length')
        page.eval_on_selector('#audio-volume', "el => {el.value=0;el.dispatchEvent(new Event('input'));}")
        page.click('#audio-test')
        assert page.evaluate('audioCalls.length') == calls
        assert '0%' in page.locator('#audio-status').inner_text()

        fixture['state'] = 'fallen'
        page.wait_for_function("document.getElementById('video-alarm').dataset.state === 'fallen'")
        for theme in ('dark', 'light'):
            page.select_option('#theme', theme)
            for width in (390, 768, 1440):
                page.set_viewport_size({'width': width, 'height': 1050})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width)
                badge = page.locator('#video-alarm').bounding_box()
                image = page.locator('.video-wrap').bounding_box()
                assert badge['x'] >= image['x'] and badge['y'] >= image['y']
                assert badge['x'] + badge['width'] <= image['x'] + image['width']
                assert badge['y'] + badge['height'] <= image['y'] + image['height']
                transport = page.locator('.transport-label').bounding_box()
                assert badge['y'] + badge['height'] <= transport['y'], 'Warning and transport labels must not overlap'
                if theme == 'light' and width == 390:
                    page.locator('.video-wrap').screenshot(path=str(checks / 'alert-fallen-mobile.png'))
        # A connection failure must clear an active warning, not display stale AI state.
        fixture['online'] = False
        page.wait_for_function("document.getElementById('video-alarm').dataset.state === 'offline'")
        assert page.locator('#video-alarm').is_hidden()
        assert '분석 연결 확인 필요' in page.locator('#alarm-title').inner_text()
        fixture['online'] = True
        # Losing server history (restart or the 50-item bound) must not dismiss
        # this tab's unacknowledged historical fall, including after a reload.
        fixture['events'] = []
        fixture['state'] = 'unobserved'
        page.reload()
        page.wait_for_function("document.getElementById('alarm-title').textContent === '판정 대상 없음'")
        assert page.locator('#fall-notice').is_visible()
        assert page.locator('#event-count').inner_text() == '0'
        assert page.evaluate('audioCalls.length') == 0
        assert page.locator('#audio-enable').get_attribute('aria-pressed') == 'false'
        page.click('#acknowledge-fall')
        assert not page.locator('#fall-notice').is_visible()
        page.select_option('#audio-voice', 'natural')
        assert page.evaluate('clipCalls.length') == 0, 'Selecting a voice is silent'
        assert page.locator('#audio-enable').get_attribute('aria-pressed') == 'false'
        page.eval_on_selector('#audio-volume', "el => {el.value=60;el.dispatchEvent(new Event('input'));}")
        page.click('#audio-test')
        page.wait_for_function('clipCalls.length === 1')
        assert page.evaluate('clipCalls[0].volume') == .6
        assert page.evaluate('audioCalls.length') == 0, 'Natural mode must not use browser TTS'
        page.click('#audio-test')
        page.wait_for_function('clipCalls.length === 2')
        assert sum(path.endswith('.mp3') for _, path in requests) == 1, 'Reuse one cached buffer'
        page.reload()
        page.wait_for_function("document.getElementById('alarm-title').textContent === '판정 대상 없음'")
        assert page.locator('#audio-voice').input_value() == 'natural'
        assert page.evaluate('clipCalls.length') == 0
        page.click('#audio-enable')
        page.wait_for_function('tones === 1')
        assert page.evaluate('clipCalls.length') == 0, 'Enabling is not a fall announcement'
        fixture['events'].insert(0, event(11, 'fall_confirmed'))
        page.wait_for_function('clipCalls.length === 1')
        page.wait_for_timeout(1100)
        assert page.evaluate('clipCalls.length') == 1, 'Natural alerts are deduplicated too'
        page.click('#audio-enable')
        # A retired saved voice migrates silently to the supported default.
        page.evaluate("localStorage.setItem('industrial-voice-mode', 'espeak')")
        page.reload()
        page.wait_for_function("document.getElementById('alarm-title').textContent === '판정 대상 없음'")
        assert page.locator('#audio-voice option').count() == 2
        assert page.locator('#audio-voice').input_value() == 'natural'
        assert page.evaluate("localStorage.getItem('industrial-voice-mode')") == 'natural'
        assert page.evaluate('audioCalls.length + clipCalls.length + tones') == 0
        assert page.locator('#audio-enable').get_attribute('aria-pressed') == 'false'
        assert not any('espeak' in path for _, path in requests)
        # A missing file is reported and the original voice still works.
        fixture['clip_failure'] = True
        page.reload()
        page.wait_for_function("document.getElementById('alarm-title').textContent === '판정 대상 없음'")
        page.click('#audio-test')
        page.wait_for_function('audioCalls.length === 1')
        assert '기존 음성으로 대체' in page.locator('#audio-status').inner_text()
        fixture['clip_failure'] = False
        page.click('#audio-test')
        page.wait_for_function('clipCalls.length === 1')
        # Cancel an in-flight decode by changing voice: it must not play later.
        page.reload()
        page.wait_for_function("document.getElementById('alarm-title').textContent === '판정 대상 없음'")
        page.evaluate('window.clipDecodeHang=true')
        page.click('#audio-test')
        page.select_option('#audio-voice', 'browser')
        page.wait_for_timeout(6200)
        assert page.evaluate('audioCalls.length + clipCalls.length') == 0
        page.select_option('#audio-voice', 'natural')
        page.click('#audio-test')
        page.wait_for_function('audioCalls.length === 1', timeout=8000)
        assert '기존 음성으로 대체' in page.locator('#audio-status').inner_text()
        assert all(method == 'GET' for method, _ in requests)
        assert not errors, errors
        context.close()
        # Real Chromium decoding/playback, but physical audio is muted by the
        # launch flag. The mocked engines above cannot prove the MP3 is usable.
        real = browser.new_context(viewport={'width': 1440, 'height': 1050})
        real.route('**/*', route_request)
        page = real.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://industrial.test/')
        page.click('#audio-test')
        page.wait_for_function("document.getElementById('audio-status').textContent.includes('완료 · 안내형')", timeout=10000)
        assert not errors, errors
        real.close()
        browser.close()
    print('Browser alerts: states, persistent notice/ack, silent baseline, dedup, read-only sound test,')
    print('volume, muted/hidden tabs, unavailable/error/hung speech fallback and responsive layout PASS.')
    print('Two voices: preference, MP3 playback/cache, automatic alert, failure fallback, cancellation PASS.')
    print('Real Chromium MP3 decode/playback PASS (physical output muted).')


if __name__ == '__main__':
    main()
