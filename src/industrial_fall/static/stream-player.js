/* WHEP first; bounded startup/stall detection; exactly one active transport. */
(() => {
  const players = new Map();
  const blank = "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=";
  let overlays = false;
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
    if (url) fetch(url, {method:"DELETE", keepalive:true}).catch(() => {});
  }
  function stop(img) {
    const state = players.get(img);
    players.delete(img);
    if (state) {
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
    clearTimeout(state.timer); clearInterval(state.watchdog);
    if (state.pc) { state.pc.onconnectionstatechange = null; state.pc.close(); state.pc = null; }
    if (state.video) { state.video.pause(); state.video.srcObject = null; state.video.remove(); state.video = null; }
    release(state.session); state.session = null;
    state.mode = "mjpeg";
    img.hidden = false;
    if (!visible(img)) { stop(img); return; }
    // A fallback changes transport, not the selected overlay mode.
    img.dataset.streaming = "true";
    img.src = `/stream/${encodeURIComponent(img.dataset.camera)}?overlay=${overlays}&t=${Date.now()}`;
    label(img, `MJPEG · ${reason}`);
  }
  async function start(img) {
    if (!img) return;
    stop(img);
    if (!visible(img)) return;
    const state = {mode:"connecting", session:null};
    players.set(img, state);
    if (overlays || !window.RTCPeerConnection) {
      fallback(img, state, overlays ? "오버레이 확인" : "미리보기"); return;
    }
    label(img, "WebRTC 연결 중…");
    state.timer = setTimeout(() => fallback(img, state, "연결 시간 초과"), 12000);
    const active = () => players.get(img) === state && state.mode !== "mjpeg";
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
      const response = await fetch(`/api/webrtc/${encodeURIComponent(img.dataset.camera)}/whep`, {
        method:"POST", headers:{"Content-Type":"application/sdp"}, body:pc.localDescription.sdp,
        signal:AbortSignal.timeout(10000)
      });
      const session = response.headers.get("Location");
      if (!active()) { release(session); return; }
      state.session = session;
      if (!response.ok || !session) throw new Error("WHEP unavailable");
      await pc.setRemoteDescription({type:"answer", sdp:await response.text()});
      let frames = -1, lastFrame = performance.now();
      state.watchdog = setInterval(async () => {
        if (!active() || !visible(img)) return;
        try {
          const stats = await pc.getStats();
          stats.forEach(report => {
            if (report.type === "inbound-rtp" && report.kind === "video" && report.framesDecoded !== undefined) {
              if (report.framesDecoded > frames) { frames = report.framesDecoded; lastFrame = performance.now(); }
            }
          });
          if (performance.now() - lastFrame > 5000) fallback(img, state, "영상 정지 감지");
        } catch (_) { fallback(img, state, "재생 오류"); }
      }, 1500);
    } catch (_) { fallback(img, state, "WebRTC 사용 불가"); }
  }
  document.querySelectorAll(".stream").forEach(img => img.addEventListener("error", () => {
    const state = players.get(img);
    if (state?.mode === "mjpeg" && visible(img)) {
      clearTimeout(state.retry);
      state.retry = setTimeout(() => {
        if (players.get(img) === state && visible(img)) img.src = `/stream/${img.dataset.camera}?overlay=${overlays}&t=${Date.now()}`;
      }, 3000);
    }
  }));
  window.addEventListener("pagehide", () => document.querySelectorAll(".stream").forEach(stop));
  const resume = () => document.querySelectorAll(".stream").forEach(img => visible(img) ? start(img) : stop(img));
  document.addEventListener("visibilitychange", resume);
  window.addEventListener("pageshow", resume);
  window.addEventListener("online", resume);
  document.getElementById("reconnect").addEventListener("click", resume);
  resume();
})();
