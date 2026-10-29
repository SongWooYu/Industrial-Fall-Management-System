import argparse
import logging
from pathlib import Path
import signal
import time

from flask import Flask, Response, abort, jsonify, render_template, request

from .config import load_config
from .connection_history import ConnectionHistory, access_scope, connection_id
from .evidence import EVENT_ID
from .pipeline import FallPipeline
from .request_origin import same_origin_request
from .webrtc import register_webrtc_routes


def create_app(pipeline, connection_history=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 131072
    history = connection_history if connection_history is not None else ConnectionHistory()
    app.extensions["connection_history"] = history
    register_webrtc_routes(app, history)

    @app.route("/api/video-connections", methods=["GET", "POST"])
    def video_connections():
        if request.method in {"GET", "HEAD"}:
            return jsonify(history.snapshot())
        if not same_origin_request():
            abort(403)
        if request.mimetype != "application/json":
            abort(415)
        if request.content_length is None or request.content_length > 2048:
            abort(413)
        try:
            accepted = history.record(request.get_json(silent=True), access=access_scope())
        except ValueError:
            return jsonify(error="Invalid connection observation"), 400
        return jsonify(ok=accepted), 202 if accepted else 429

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
        cid, access = connection_id(request.args.get("cid")), access_scope()
        fields = {"transport": "mjpeg", "overlay": overlay}
        if not pipeline.subscribe(overlay):
            history.server("mjpeg_rejected", cid, access, reason="viewer_limit", **fields)
            return jsonify(error="동시 MJPEG 접속 한도(6명)에 도달했습니다."), 503
        history.server("mjpeg_open", cid, access, **fields)
        released = False
        started = time.monotonic()

        def release():
            nonlocal released
            if not released:
                released = True
                pipeline.unsubscribe(overlay)
                history.server("mjpeg_closed", cid, access, reason="stream_closed",
                               elapsed_ms=min(604800000, int((time.monotonic() - started) * 1000)), **fields)

        def frames():
            previous = -1
            last_sent = 0.0
            try:
                while not pipeline.stop_event.is_set():
                    seq, jpeg = pipeline.jpeg(overlay)
                    if jpeg and (seq != previous or time.monotonic() - last_sent > 1):
                        if previous == -1:
                            # A server write is NOT proof the browser decoded a frame.
                            history.server("mjpeg_sent", cid, access, **fields)
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
    history = ConnectionHistory(Path(config["project_root"]) / "runtime/logs/video-connections.jsonl")
    app = create_app(pipeline, history)

    def shutdown(_sig, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    pipeline.start()
    try:
        app.run(host=config["server"]["host"], port=config["server"]["port"], threaded=True, use_reloader=False)
    finally:
        pipeline.stop()
        history.close()


if __name__ == "__main__":
    main()
