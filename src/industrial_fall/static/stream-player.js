/* WHEP first; bounded startup/stall detection; exactly one active transport. */
(() => {
  const players = new Map();
  const blank = "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=";
  let overlays = false, pendingReports = 0;
  const reasons = {"오버레이 확인":"overlay_selected", "미리보기":"rtc_unsupported",
    "연결 시간 초과":"startup_timeout", "브라우저 재생 제한":"play_blocked", "연결 끊김":"ice_failed",
    "영상 정지 감지":"video_stalled", "재생 오류":"stats_error", "WebRTC 사용 불가":"rtc_error",
    "WHEP 협상 실패":"whep_failed"};
  function report(state, event, fields = {}) {
    const payload = {connection_id:state.id, sequence:++state.sequence, event, transport:state.mode,
      overlay:state.overlay, elapsed_ms:Math.min(604800000, Math.round(performance.now()-state.started)), ...fields};
    // No retry queue: telemetry failure must never block video playback.
    if (pendingReports >= 8) return;
    pendingReports++;
    fetch("/api/video-connections", {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify(payload), keepalive:true, signal:AbortSignal.timeout(4000)})
      .catch(() => {}).finally(() => pendingReports--);
  }
  function mjpegUrl(img, state) {
    return `/stream/${encodeURIComponent(img.dataset.camera)}?overlay=${state.overlay}&cid=${state.id}&t=${Date.now()}`;
  }
  const selector = document.getElementById("stream-mode");
  selector.addEventListener("change", () => {
    overlays = selector.value === "overlay";
    document.querySelectorAll(".stream").forEach(start);
  });
  // During RTC the fallback img itself is hidden, but its video panel is active.
  function visible(img) { return !document.hidden && !img.parentElement.closest("[hidden]"); }
  function label(img, text) {
    const caption = img.closest(".video-wrap").querySelector(".transport-label");
    if (caption) caption.textContent = text;
  }
  function release(url) {
    if (url) fetch(url, {method:"DELETE", keepalive:true, signal:AbortSignal.timeout(10000)}).catch(() => {});
  }
  function stop(img, reason = "restart") {
    const state = players.get(img);
    players.delete(img);
    if (state) {
      report(state, "ended", {reason});
      clearTimeout(state.timer); clearInterval(state.watchdog); clearTimeout(state.retry);
      if (state.pc) { state.pc.onconnectionstatechange = null; state.pc.close(); }
      if (state.video) { state.video.pause(); state.video.srcObject = null; state.video.remove(); }
      release(state.session);
    }
    img.dataset.streaming = "false";
    img.src = blank;
    img.hidden = false;
  }
  function fallback(img, state, reason) {
    if (players.get(img) !== state || state.mode === "mjpeg") return;
    const ice = state.pc?.iceConnectionState;
    clearTimeout(state.timer); clearInterval(state.watchdog);
    if (state.pc) { state.pc.onconnectionstatechange = null; state.pc.close(); state.pc = null; }
    if (state.video) { state.video.pause(); state.video.srcObject = null; state.video.remove(); state.video = null; }
    release(state.session); state.session = null;
    state.mode = "mjpeg";
    report(state, "fallback", {reason:reasons[reason], ...(ice ? {ice_state:ice} : {}),
      ...(state.whepStatus ? {http_status:state.whepStatus} : {})});
    img.hidden = false;
    if (!visible(img)) { stop(img, "hidden"); return; }
    // A fallback changes transport, not the selected overlay mode.
    img.dataset.streaming = "true";
    img.src = mjpegUrl(img, state);
    label(img, `MJPEG 연결 중 · ${reason}`);
    state.watchdog = setInterval(() => {
      if (players.get(img) !== state || !visible(img) || state.mjpegFailed) return;
      // Multipart load events vary: decoded dimensions prove a bitmap, not FPS.
      if (!state.mjpegReady && img.naturalWidth > 1 && img.naturalHeight > 1) {
        state.mjpegReady = true;
        report(state, "mjpeg_decoded", {reason:reasons[reason], width:img.naturalWidth, height:img.naturalHeight});
        label(img, `MJPEG · ${reason}`);
      }
    }, 500);
  }
  async function start(img) {
    if (!img) return;
    stop(img);
    if (!visible(img)) return;
    // randomUUID is unavailable on plain LAN HTTP in some browsers.
    const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2,"0")).join("");
    const state = {id, sequence:0, started:performance.now(), mode:"connecting", session:null, overlay:overlays};
    players.set(img, state);
    report(state, "attempt", {reason:overlays ? "overlay_selected" : "automatic"});
    if (overlays || !window.RTCPeerConnection) {
      fallback(img, state, overlays ? "오버레이 확인" : "미리보기"); return;
    }
    label(img, "WebRTC 연결 중…");
    state.timer = setTimeout(() => fallback(img, state, "연결 시간 초과"), 12000);
    const active = () => players.get(img) === state && state.mode !== "mjpeg";
    let failure = "WebRTC 사용 불가";
    try {
      const pc = state.pc = new RTCPeerConnection({iceServers:[]});
      const video = state.video = document.createElement("video");
      video.className = "rtc-video"; video.autoplay = true; video.muted = true; video.playsInline = true;
      video.hidden = true; video.setAttribute("aria-label", img.alt);
      img.before(video);
      pc.addTransceiver("video", {direction:"recvonly"});
      pc.ontrack = event => {
        if (!active()) return;
        video.srcObject = event.streams[0] || new MediaStream([event.track]);
        video.play().catch(() => fallback(img, state, "브라우저 재생 제한"));
      };
      pc.onconnectionstatechange = () => {
        if (["failed", "closed"].includes(pc.connectionState)) fallback(img, state, "연결 끊김");
      };
      video.addEventListener("playing", () => {
        if (!active()) return;
        clearTimeout(state.timer); state.mode = "webrtc";
        img.hidden = true; video.hidden = false;
        label(img, "WebRTC 원본 · 박스 OFF / AI 분석 ON");
      });
      await pc.setLocalDescription(await pc.createOffer());
      await new Promise(resolve => {
        if (pc.iceGatheringState === "complete") return resolve();
        const done = () => { clearTimeout(timer); pc.removeEventListener("icegatheringstatechange", changed); resolve(); };
        const changed = () => { if (pc.iceGatheringState === "complete") done(); };
        const timer = setTimeout(done, 2000);
        pc.addEventListener("icegatheringstatechange", changed);
      });
      if (!active()) return;
      // Do not abort a POST on tab change: collect and DELETE a late session too.
      failure = "WHEP 협상 실패";
      const response = await fetch(`/api/webrtc/${encodeURIComponent(img.dataset.camera)}/whep?cid=${state.id}`, {
        method:"POST", headers:{"Content-Type":"application/sdp"}, body:pc.localDescription.sdp,
        signal:AbortSignal.timeout(10000)
      });
      const session = response.headers.get("Location");
      if (!active()) { release(session); return; }
      state.whepStatus = response.status;
      state.session = session;
      if (!response.ok || !session) throw new Error("WHEP unavailable");
      await pc.setRemoteDescription({type:"answer", sdp:await response.text()});
      let frames = 0, lastFrame = performance.now(), checking = false;
      state.watchdog = setInterval(async () => {
        if (!active() || !visible(img) || checking) return;
        checking = true;
        try {
          const stats = await pc.getStats();
          if (!active()) return;
          let pair;
          stats.forEach(item => {
            if (item.type === "transport" && item.selectedCandidatePairId) pair = stats.get(item.selectedCandidatePairId);
          });
          if (!pair) stats.forEach(item => {
            if (item.type === "candidate-pair" && item.nominated && item.state === "succeeded") pair = item;
          });
          stats.forEach(report => {
            if (report.type === "inbound-rtp" && (report.kind || report.mediaType) === "video" && report.framesDecoded !== undefined) {
              if (report.framesDecoded > frames) { frames = report.framesDecoded; lastFrame = performance.now(); }
            }
          });
          if (frames > 0 && state.mode === "webrtc" && !state.rtcReported && pair) {
            state.rtcReported = true;
            const local = stats.get(pair.localCandidateId), remote = stats.get(pair.remoteCandidateId);
            report(state, "rtc_playing", {frames_decoded:frames, width:video.videoWidth, height:video.videoHeight,
              local_candidate:local?.candidateType || "unknown", remote_candidate:remote?.candidateType || "unknown",
              protocol:remote?.protocol || local?.protocol || "unknown", ice_state:pc.iceConnectionState});
          }
          if (performance.now() - lastFrame > 5000) fallback(img, state, "영상 정지 감지");
        } catch (_) { fallback(img, state, "재생 오류"); }
        finally { checking = false; }
      }, 1500);
    } catch (_) { fallback(img, state, failure); }
  }
  document.querySelectorAll(".stream").forEach(img => img.addEventListener("error", () => {
    const state = players.get(img);
    if (state?.mode === "mjpeg" && visible(img)) {
      state.mjpegFailed = true; state.mjpegReady = false;
      report(state, "mjpeg_error", {reason:"stream_error"});
      label(img, "MJPEG 오류 · 재연결 중");
      clearTimeout(state.retry);
      state.retry = setTimeout(() => {
        if (players.get(img) === state && visible(img)) {
          state.mjpegFailed = false; img.src = mjpegUrl(img, state);
        }
      }, 3000);
    }
  }));
  window.addEventListener("pagehide", () => document.querySelectorAll(".stream").forEach(img => stop(img, "pagehide")));
  const resume = () => document.querySelectorAll(".stream").forEach(img => visible(img) ? start(img) : stop(img, "hidden"));
  document.addEventListener("visibilitychange", resume);
  window.addEventListener("pageshow", event => { if (event.persisted) resume(); });
  window.addEventListener("online", resume);
  document.getElementById("reconnect").addEventListener("click", resume);
  resume();
})();
