import argparse
import logging
from pathlib import Path
import signal
import time

from flask import Flask, Response, abort, jsonify, render_template, request

from .config import load_config
from .evidence import EVENT_ID
from .pipeline import FallPipeline
from .webrtc import register_webrtc_routes


def create_app(pipeline):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 131072
    register_webrtc_routes(app)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/status")
    def status():
        return jsonify(pipeline.status())

    @app.get("/api/events")
    def events():
        return jsonify(pipeline.events.recent())

    @app.get("/api/events/<event_id>/snapshot.jpg")
    def snapshot(event_id):
        if not EVENT_ID.fullmatch(event_id):
            abort(404)
        data = pipeline.events.evidence.get(event_id)
        if data is None:
            abort(404)
        return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/stream/dahua")
    def stream():
        overlay = request.args.get("overlay", "true").lower() not in {"false", "0", "off"}
        if not pipeline.subscribe(overlay):
            return jsonify(error="동시 MJPEG 접속 한도(6명)에 도달했습니다."), 503
        released = False

        def release():
            nonlocal released
            if not released:
                released = True
                pipeline.unsubscribe(overlay)

        def frames():
            previous = -1
            last_sent = 0.0
            try:
                while not pipeline.stop_event.is_set():
                    seq, jpeg = pipeline.jpeg(overlay)
                    if jpeg and (seq != previous or time.monotonic() - last_sent > 1):
                        yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                               + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                        previous, last_sent = seq, time.monotonic()
                    time.sleep(.05)
            finally:
                release()

        response = Response(frames(), mimetype="multipart/x-mixed-replace; boundary=frame",
                            headers={"Cache-Control": "no-store, no-cache", "X-Accel-Buffering": "no"})
        response.call_on_close(release)
        return response

    @app.get("/health")
    def health():
        state = pipeline.status()
        ready = state["analysis_online"]
        return jsonify(status="ok" if ready else "degraded", camera_online=state["online"],
                       analysis_online=ready), 200 if ready else 503

    @app.after_request
    def no_cache(response):
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    return app


def main():
    parser = argparse.ArgumentParser(description="산업안전 낙상관리시스템 · Pi5")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[2] / "config/cameras.yaml"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    config = load_config(args.config)
    pipeline = FallPipeline(config)
    app = create_app(pipeline)

    def shutdown(_sig, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    pipeline.start()
    try:
        app.run(host=config["server"]["host"], port=config["server"]["port"], threaded=True, use_reloader=False)
    finally:
        pipeline.stop()


if __name__ == "__main__":
    main()
