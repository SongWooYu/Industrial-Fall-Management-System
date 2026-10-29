"""Optional Playwright + system Chromium smoke test; never emits real events."""
import argparse
import base64
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    Path("runtime/checks").mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/usr/bin/chromium", headless=True,
                                    args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(viewport={"width": 1440, "height": 1050})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_function("document.getElementById('connection').textContent.includes('AI 분석 중')", timeout=25000)
        page.wait_for_function("(document.querySelector('video')?.readyState >= 2) || document.querySelector('.stream').naturalWidth > 100", timeout=25000)
        print("Default transport:", page.locator('.transport-label').inner_text())
        page.select_option('#stream-mode', 'overlay')
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
        context.route('**/api/webrtc/dahua/whep', lambda route: route.fulfill(status=503, body='unavailable'))
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_function("document.querySelector('.stream').naturalWidth > 100", timeout=25000)
        assert 'MJPEG' in page.locator('.transport-label').inner_text()
        assert 'overlay=false' in page.locator('.stream').get_attribute('src')
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
