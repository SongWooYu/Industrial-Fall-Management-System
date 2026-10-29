"""Same-origin WHEP signaling only. Media flows directly browser <-> Pi5."""
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from flask import Response, abort, jsonify, request
from .request_origin import same_origin_request
from .connection_history import access_scope, connection_id

PATHS = {"dahua": "dahuaclean"}
BASE = "http://127.0.0.1:8889"
SESSION = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def register_webrtc_routes(app, history=None):
    opener = build_opener(ProxyHandler({}), NoRedirect())

    @app.route("/api/webrtc/<camera_id>/whep", methods=["POST"])
    @app.route("/api/webrtc/<camera_id>/whep/<session_id>", methods=["DELETE"])
    def whep(camera_id, session_id=None):
        if camera_id not in PATHS or (session_id and not SESSION.fullmatch(session_id)):
            abort(404)
        if not same_origin_request():
            abort(403)
        if request.method == "POST" and request.mimetype != "application/sdp":
            abort(415)
        if request.content_length is None and request.method == "POST":
            abort(411)
        if (request.content_length or 0) > 131072:
            abort(413)
        path = f"/{PATHS[camera_id]}/whep"
        target = BASE + path + ("/" + session_id if session_id else "")
        cid, access = connection_id(request.args.get("cid")), access_scope()
        def record(event, **fields):
            if history is not None and request.method == "POST":
                history.server(event, cid, access, transport="webrtc", **fields)
        try:
            with opener.open(Request(target, data=request.get_data() if session_id is None else None,
                                     method=request.method, headers={"Content-Type": "application/sdp"}), timeout=8) as upstream:
                body = upstream.read(131073)
                if len(body) > 131072:
                    raise ValueError("SDP too large")
                response = Response(body, status=upstream.status, mimetype="application/sdp")
                location = upstream.headers.get("Location")
                if location:
                    parsed = urlsplit(location)
                    identifier = parsed.path.removeprefix(path + "/")
                    if (parsed.netloc and parsed.netloc != "127.0.0.1:8889") or not parsed.path.startswith(path + "/") or not SESSION.fullmatch(identifier):
                        raise ValueError("Invalid WHEP session")
                    response.headers["Location"] = f"/api/webrtc/{camera_id}/whep/{identifier}"
                response.headers["Cache-Control"] = "no-store"
                record("whep_accepted", http_status=upstream.status)
                return response
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            fields = {"reason": "upstream_unavailable"}
            if isinstance(exc, HTTPError):
                fields = {"reason": "upstream_http", "http_status": exc.code}
            elif isinstance(exc, ValueError):
                fields = {"reason": "invalid_response"}
            record("whep_failed", **fields)
            return jsonify(error="WebRTC 연결을 사용할 수 없습니다. MJPEG로 전환합니다."), 503
