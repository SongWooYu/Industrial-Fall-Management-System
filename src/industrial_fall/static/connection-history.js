(() => {
  const $ = id => document.getElementById(id);
  const titles = {attempt:"영상 연결 시도", fallback:"MJPEG로 전환", rtc_playing:"WebRTC 영상 수신 확인",
    mjpeg_decoded:"MJPEG 이미지 수신 확인", mjpeg_error:"MJPEG 오류 · 재시도", ended:"브라우저 연결 종료",
    whep_accepted:"WHEP 협상 응답 (영상 수신 아님)", whep_failed:"WHEP 협상 실패",
    mjpeg_open:"MJPEG 요청 수락", mjpeg_sent:"MJPEG 첫 프레임 송신", mjpeg_closed:"MJPEG 송신 종료",
    mjpeg_rejected:"MJPEG 접속 한도 초과"};
  const reasons = {automatic:"WebRTC 우선", overlay_selected:"박스·관절 모드 선택 (의도된 MJPEG)",
    rtc_unsupported:"브라우저 WebRTC 미지원", startup_timeout:"연결 시간 초과", play_blocked:"자동 재생 제한",
    ice_failed:"ICE 연결 실패/종료", video_stalled:"5초 이상 새 프레임 없음", stats_error:"재생 상태 조회 실패",
    whep_failed:"WHEP 협상 실패", rtc_error:"WebRTC 초기화 오류", stream_error:"이미지 연결 오류",
    hidden:"탭 숨김", pagehide:"페이지 닫힘/이동", restart:"모드 변경/재연결", upstream_http:"MediaMTX HTTP 오류",
    upstream_unavailable:"MediaMTX 연결/응답 불가", invalid_response:"잘못된 협상 응답",
    viewer_limit:"동시 시청 한도 6명", stream_closed:"송신 연결 종료"};
  const scopes = {domain_url:"도메인 URL", private_ip_url:"사설 IP URL", public_ip_url:"공인 IP URL", loopback_url:"로컬 URL"};
  let records = [], lastKey = '';
  function render() {
    const rows = records.filter(row => !$('video-log-filter').checked || row.transport === 'mjpeg' || row.event === 'whep_failed');
    const key = rows.map(row => row.id).join(',');
    if (key === lastKey) return;
    lastKey = key;
    const container = $('video-log-events'); container.replaceChildren();
    if (!rows.length) { const p=document.createElement('p'); p.className='empty'; p.textContent='이 조건에 해당하는 연결 기록이 없습니다.'; container.append(p); }
    for (const row of rows) {
      const item=document.createElement('article'); item.className='connection-row';
      item.dataset.failed=String(['fallback','whep_failed','mjpeg_error','mjpeg_rejected'].includes(row.event) && row.reason!=='overlay_selected');
      const title=document.createElement('strong'); title.textContent=titles[row.event] || row.event;
      const meta=document.createElement('small'); meta.textContent=`${new Date(row.timestamp).toLocaleString('ko-KR',{hour12:false})} · ${row.source==='browser'?'브라우저 보고':'서버 관측'} · ${scopes[row.access] || '접속 범위 미상'} · #${row.connection_id.slice(0,8)}`;
      const parts=[reasons[row.reason] || '', row.overlay ? '오버레이 ON' : '오버레이 OFF'];
      if (row.width) parts.push(`${row.width}×${row.height}`);
      if (row.frames_decoded) parts.push(`복호화 ${row.frames_decoded}프레임`);
      if (row.remote_candidate) parts.push(`${row.local_candidate} ↔ ${row.remote_candidate} / ${row.protocol}`);
      if (row.ice_state) parts.push(`ICE ${row.ice_state}`);
      if (row.http_status) parts.push(`HTTP ${row.http_status}`);
      if (row.elapsed_ms !== undefined) parts.push(`연결 시도 후 ${(row.elapsed_ms/1000).toFixed(1)}초`);
      const detail=document.createElement('p'); detail.textContent=parts.filter(Boolean).join(' · ');
      item.append(title,meta,detail);container.append(item);
    }
  }
  $('video-log-filter').addEventListener('change', () => { lastKey='force';render(); });
  async function poll() {
    if (!document.hidden) {
      try {
        const response=await fetch('/api/video-connections',{cache:'no-store',signal:AbortSignal.timeout(4000)});
        if(!response.ok) throw new Error();
        const data=await response.json(); records=data.events;
        $('video-log-summary').textContent=`최근 ${records.length}/${data.limit}건`;
        $('video-log-health').textContent=data.storage_error ? '로그 저장 오류 · 디스크 상태 확인 필요' :
          `${data.persistent?'파일 저장 중':'메모리 기록만'}${data.dropped ? ` · 제한/오류로 제외 ${data.dropped}건` : ''}`;
        render();
      } catch (_) { $('video-log-health').textContent='연결 이력 조회 실패 · 자동 재시도'; }
    }
    setTimeout(poll,5000);
  }
  poll();
})();
