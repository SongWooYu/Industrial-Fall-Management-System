(() => {
  const $ = id => document.getElementById(id);
  const theme = $('theme');
  try { theme.value = localStorage.getItem('industrial-theme') || 'system'; } catch (_) {}
  function applyTheme() {
    if (theme.value === 'system') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme.value;
    try { localStorage.setItem('industrial-theme', theme.value); } catch (_) {}
  }
  theme.addEventListener('change', applyTheme); applyTheme();

  let activeTab = 'fall';
  function refreshRobotStreamSrc() {
    const img = $('robot-live-stream');
    if (!img) return;
    const overlay = $('robot-overlay-mode')?.value ?? '1';
    img.src = `/stream/robot?overlay=${overlay}&t=${Date.now()}`;
    if ($('robot-stream-label')) {
      $('robot-stream-label').textContent = overlay === '1' ? '실시간 AI 분석 스트림' : '원본 카메라 스트림';
    }
  }

  function switchTab(tab) {
    if (activeTab === tab) return;
    activeTab = tab;
    const isFall = tab === 'fall';
    if ($('tab-fall')) $('tab-fall').hidden = !isFall;
    if ($('tab-robot')) $('tab-robot').hidden = isFall;
    if ($('tab-btn-fall')) {
      $('tab-btn-fall').classList.toggle('active', isFall);
      $('tab-btn-fall').setAttribute('aria-selected', String(isFall));
    }
    if ($('tab-btn-robot')) {
      $('tab-btn-robot').classList.toggle('active', !isFall);
      $('tab-btn-robot').setAttribute('aria-selected', String(!isFall));
    }
    window.dispatchEvent(new Event('industrial-tab-changed'));
    if (!isFall) {
      refreshRobotStreamSrc();
      fetchRobot();
    }
  }
  $('tab-btn-fall')?.addEventListener('click', () => switchTab('fall'));
  $('tab-btn-robot')?.addEventListener('click', () => switchTab('robot'));

  const titles = {normal:'확정 경보 없음', suspected:'낙상 의심 · 확인 중', fallen:'낙상 감지 · 현장 확인 필요', recovering:'낙상 회복 동작 관측 중', unobserved:'판정 대상 없음'};
  const badges = {suspected:'낙상 의심', fallen:'낙상 감지', recovering:'낙상 회복 중'};
  const eventTitles = {fall_confirmed:'낙상 감지',fall_suspected:'낙상 의심',fall_recovering:'회복 동작 관측',fall_cleared:'감지 상태 해제',tracking_lost:'추적 종료 · 회복 여부 미확인'};
  const date = value => new Date(value).toLocaleString('ko-KR', {hour12:false});
  let pendingFall = null, acknowledged = [];
  try { acknowledged = JSON.parse(sessionStorage.getItem('industrial-ack-falls') || '[]'); } catch (_) {}
  try {
    const saved = JSON.parse(sessionStorage.getItem('industrial-pending-fall') || 'null');
    if (saved && typeof saved.id === 'string' && /^[a-f0-9]{32}$/.test(saved.id)
        && typeof saved.timestamp === 'string' && Number.isFinite(Date.parse(saved.timestamp))) pendingFall = saved;
  } catch (_) {}
  if (!Array.isArray(acknowledged)) acknowledged = [];
  acknowledged = acknowledged.filter(id => typeof id === 'string').slice(-50);
  $('acknowledge-fall').addEventListener('click', () => {
    if (!pendingFall) return;
    acknowledged.push(pendingFall.id); acknowledged = acknowledged.slice(-50);
    try {
      sessionStorage.setItem('industrial-ack-falls', JSON.stringify(acknowledged));
      sessionStorage.removeItem('industrial-pending-fall');
    } catch (_) {}
    pendingFall = null; $('fall-notice').hidden = true;
    $('events').scrollIntoView({behavior:'smooth', block:'nearest'});
  });
  function showBadge(state) {
    const display = badges[state] || '';
    $('video-alarm').dataset.state = state;
    $('video-alarm-title').textContent = display;
    $('video-alarm').hidden = !display;
    document.querySelectorAll('[data-stage]').forEach(el => {
      el.classList.toggle('active', el.dataset.stage === state);
      if (el.dataset.stage === state) el.setAttribute('aria-current', 'step');
      else el.removeAttribute('aria-current');
    });
  }
  function openEvidenceModal(titleText, imgUrl, detailText, downloadName) {
    $('evidence-title').textContent = titleText;
    $('evidence-image').src = imgUrl;
    if ($('download-evidence')) {
      $('download-evidence').href = imgUrl;
      $('download-evidence').download = downloadName || 'capture.jpg';
    }
    $('evidence-detail').textContent = detailText;
    $('evidence').showModal();
  }
  let eventKey = null;
  function showEvents(events) {
    document.dispatchEvent(new CustomEvent('fall-events-updated', {detail:events}));
    const visibleEvents = events.filter(event => event.type === 'fall_confirmed');
    const diagEvents = events.filter(event => event.type !== 'fall_confirmed');
    const key = events.map(e => `${e.id}:${e.snapshot_url || ''}`).join(',');
    $('event-count').textContent = visibleEvents.length;
    if ($('diag-event-count')) $('diag-event-count').textContent = diagEvents.length;
    if (eventKey === key) return;
    eventKey = key;
    const confirmed = visibleEvents[0];
    if (confirmed && !acknowledged.includes(confirmed.id)) {
      pendingFall = {id:confirmed.id, timestamp:confirmed.timestamp};
      try { sessionStorage.setItem('industrial-pending-fall', JSON.stringify(pendingFall)); } catch (_) {}
    }
    if (pendingFall) {
      $('fall-notice').hidden = false;
      $('fall-notice-detail').textContent = `${date(pendingFall.timestamp)} · Dahua · 기록 확인 전까지 표시`;
    }
    if (events.length) $('last-transition').textContent = `최근 기록: ${date(events[0].timestamp)} · ${eventTitles[events[0].type] || events[0].type} (현재 판정과 별개)`;
    $('events').replaceChildren();
    if (!visibleEvents.length) { const p = document.createElement('p'); p.className='empty'; p.textContent='아직 확정된 낙상 사고 기록이 없습니다.'; $('events').append(p); }
    for (const event of visibleEvents) {
      const item = document.createElement('article'); item.className='event'; item.dataset.type=event.type;
      const title = document.createElement('strong'); title.textContent=eventTitles[event.type] || event.type;
      const time = document.createElement('time'); time.dateTime=event.timestamp; time.textContent=`${date(event.timestamp)} · ${event.camera_id || 'Dahua'}`;
      item.append(title,time);
      if (event.track) { const p = document.createElement('p'); p.textContent=`추적 #${event.track.track_id} · 모델 점수 ${(event.track.temporal_probability*100).toFixed(1)}%`; item.append(p); }
      if (event.snapshot_url) {
        const modalTitle = `${eventTitles[event.type] || event.type} · ${date(event.timestamp)}`;
        const modalDetail = '감지한 프레임과 같은 시점의 박스·관절 캡처입니다. 클릭하여 저장하거나 나중에 다시 확인할 수 있습니다.';
        const dlName = `fall-capture-${event.id.slice(0, 8)}.jpg`;
        const thumb = document.createElement('img');
        thumb.className = 'event-thumb';
        thumb.src = event.snapshot_url;
        thumb.alt = `${eventTitles[event.type] || event.type} 캡처 미리보기`;
        thumb.loading = 'lazy';
        thumb.addEventListener('click', () => openEvidenceModal(modalTitle, event.snapshot_url, modalDetail, dlName));
        const button = document.createElement('button'); button.type='button'; button.textContent='캡처 확인 ↗';
        button.addEventListener('click', () => openEvidenceModal(modalTitle, event.snapshot_url, modalDetail, dlName));
        item.append(thumb, button);
      }
      $('events').append(item);
    }
    const diagList = $('diag-events');
    if (diagList) {
      diagList.replaceChildren();
      if (!diagEvents.length) {
        const p = document.createElement('p'); p.className='empty'; p.textContent='상세 진단 이력이 없습니다.'; diagList.append(p);
      } else {
        for (const event of diagEvents) {
          const item = document.createElement('article'); item.className='event'; item.dataset.type=event.type;
          const title = document.createElement('strong'); title.textContent=eventTitles[event.type] || event.type;
          const time = document.createElement('time'); time.dateTime=event.timestamp; time.textContent=date(event.timestamp);
          item.append(title,time);
          if (event.track) { const p = document.createElement('p'); p.textContent=`추적 #${event.track.track_id}`; item.append(p); }
          diagList.append(item);
        }
      }
    }
  }
  $('evidence-image').addEventListener('error',()=>{$('evidence-detail').textContent='보관 시간이 지났거나 캡처 파일을 불러올 수 없습니다.';});
  $('close-evidence').addEventListener('click',()=>$('evidence').close());
  $('evidence').addEventListener('click',e=>{if(e.target===$('evidence'))$('evidence').close();});
  function showStatus(s) {
    const ready=s.analysis;
    showBadge(s.analysis_online ? s.fall_state : 'offline');
    $('alarm').dataset.state=s.analysis_online ? s.fall_state : 'unobserved';
    $('alarm-title').textContent=s.analysis_online ? titles[s.fall_state] || '관측 중' : '분석 연결 확인 필요';
    $('alarm-detail').textContent=s.error || (s.fall_state==='fallen' ? '실제 상황을 확인해 주세요. 영상 AI만으로 사고·부상 여부를 확정할 수 없습니다.' : ready.detail);
    $('connection').textContent=s.analysis_online ? '카메라 연결 · AI 분석 중' : '분석 비정상 / 대기';
    $('updated').textContent=`갱신 ${new Date().toLocaleTimeString('ko-KR',{hour12:false})}`;
    $('people').textContent=`${s.person_count}명`;
    $('fps').textContent=s.analysis_fps.toFixed(1);
    $('inference').textContent=s.inference_ms===null ? '—' : `${s.inference_ms} ms`;
    $('age').textContent=s.frame_age_ms===null ? '—' : `${s.frame_age_ms} ms`;
    $('readiness').textContent=ready.label;
    $('score').textContent=ready.motion_score===null ? '모델 점수 —' : `모델 점수 ${(ready.motion_score*100).toFixed(1)}%`;
    $('analysis-detail').textContent=ready.detail;
    $('decision').textContent=`${ready.decision_label} · 모델 점수는 사고 발생 확률이 아닙니다.`;
  }

  let selectedRobotEventId = null;
  let robotHistoryKey = null;
  let latestRobotAlertRecord = null;
  let dismissedRobotAlertIds = [];
  try { dismissedRobotAlertIds = JSON.parse(sessionStorage.getItem('industrial-dismissed-robot-alerts') || '[]'); } catch (_) {}
  if (!Array.isArray(dismissedRobotAlertIds)) dismissedRobotAlertIds = [];

  function selectRobotRecord(rec, opts = {}) {
    if (!rec) return;
    selectedRobotEventId = rec.event_id;
    document.querySelectorAll('#robot-records .event').forEach(el => {
      el.classList.toggle('selected', el.dataset.eventId === rec.event_id);
    });
    if ($('robot-selected-title')) {
      $('robot-selected-title').textContent = `[${rec.abnormal}] ${rec.item} 캡처 사진`;
    }
    if ($('robot-selected-meta')) {
      $('robot-selected-meta').textContent = `${rec.timestamp} · ${rec.item} · 일치/신뢰도 ${(rec.match_score * 100).toFixed(1)}% (좌측 실시간 영상과 동시 비교 중)`;
    }
    if ($('robot-selected-badge')) {
      $('robot-selected-badge').textContent = `${rec.item} (${rec.abnormal})`;
      $('robot-selected-badge').dataset.abnormal = rec.abnormal;
    }
    const overImg = $('robot-overview-img'), overEmpty = $('robot-overview-empty');
    const detImg = $('robot-detail-img'), detEmpty = $('robot-detail-empty');
    if (overImg && overEmpty) {
      overImg.src = rec.overview_url;
      overImg.hidden = false;
      overEmpty.hidden = true;
      overImg.onclick = () => openEvidenceModal(
        `[${rec.abnormal}] ${rec.item} — 발견 당시 전체 영상 (${rec.timestamp})`,
        rec.overview_url,
        `탐지 일치/신뢰도 ${(rec.match_score * 100).toFixed(1)}% · 현장 전체 캡처`,
        `robot-overview-${rec.event_id.slice(0, 8)}.jpg`
      );
    }
    if (detImg && detEmpty) {
      detImg.src = rec.detail_url;
      detImg.hidden = false;
      detEmpty.hidden = true;
      detImg.onclick = () => openEvidenceModal(
        `[${rec.abnormal}] ${rec.item} — 검출 대상 확대 (${rec.timestamp})`,
        rec.detail_url,
        `탐지 일치/신뢰도 ${(rec.match_score * 100).toFixed(1)}% · 부분 확대 캡처`,
        `robot-detail-${rec.event_id.slice(0, 8)}.jpg`
      );
    }
    if (opts.focusLive) {
      if (activeTab !== 'robot') switchTab('robot');
      else refreshRobotStreamSrc();
      const focusEl = $('robot-live-focus');
      if (focusEl) {
        focusEl.scrollIntoView({behavior: 'smooth', block: 'start'});
        focusEl.classList.remove('pulse-focus');
        void focusEl.offsetWidth;
        focusEl.classList.add('pulse-focus');
      }
    }
  }

  $('open-robot-live')?.addEventListener('click', () => {
    if (latestRobotAlertRecord) selectRobotRecord(latestRobotAlertRecord, {focusLive: true});
    else switchTab('robot');
  });
  $('robot-alert-thumb')?.addEventListener('click', () => {
    if (latestRobotAlertRecord) selectRobotRecord(latestRobotAlertRecord, {focusLive: true});
  });
  $('dismiss-robot-alert')?.addEventListener('click', () => {
    if (latestRobotAlertRecord) {
      dismissedRobotAlertIds.push(latestRobotAlertRecord.event_id);
      dismissedRobotAlertIds = dismissedRobotAlertIds.slice(-50);
      try { sessionStorage.setItem('industrial-dismissed-robot-alerts', JSON.stringify(dismissedRobotAlertIds)); } catch (_) {}
    }
    if ($('robot-alert-notice')) $('robot-alert-notice').hidden = true;
  });

  async function updateRobotConfig(payload) {
    try {
      const response = await fetch('/api/robot/config', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      if (response.ok) {
        showRobot(await response.json());
        refreshRobotStreamSrc();
      }
    } catch (_) {}
  }

  $('robot-vision-mode')?.addEventListener('change', e => {
    updateRobotConfig({mode: e.target.value});
  });
  $('robot-overlay-mode')?.addEventListener('change', () => {
    refreshRobotStreamSrc();
  });
  $('robot-stream-toggle')?.addEventListener('click', () => {
    const btn = $('robot-stream-toggle');
    const currentlyEnabled = btn?.getAttribute('aria-pressed') !== 'false';
    updateRobotConfig({stream_enabled: !currentlyEnabled});
  });

  function showRobot(data) {
    if (!data || !$('tab-robot')) return;
    const r = data.robot || {};
    const v = data.vision || {};
    const ins = data.inspection || {};
    const records = ins.records || [];

    // Update top alert banner if there is an unacknowledged abnormal (or latest) robot detection
    const alertCandidate = records.find(x => x.abnormal === '이상') || v.latest_alert || records[0];
    if (alertCandidate && !dismissedRobotAlertIds.includes(alertCandidate.event_id)) {
      latestRobotAlertRecord = alertCandidate;
      if ($('robot-alert-notice')) $('robot-alert-notice').hidden = false;
      if ($('robot-alert-thumb')) $('robot-alert-thumb').src = alertCandidate.overview_url || alertCandidate.detail_url;
      if ($('robot-alert-title')) {
        $('robot-alert-title').textContent = `안전탐색 로봇 [${alertCandidate.abnormal}] ${alertCandidate.item} 감지 알림`;
      }
      if ($('robot-alert-detail')) {
        $('robot-alert-detail').textContent = `${alertCandidate.timestamp} · 탐지 신뢰도 ${(alertCandidate.match_score * 100).toFixed(1)}% · 클릭하면 현재 로봇 실시간 영상과 함께 확인합니다.`;
      }
    }

    const banner = $('robot-banner');
    if (banner) {
      banner.dataset.state = !r.online ? 'unobserved' : (r.obstacle_warning || r.motor_error || r.camera_error ? 'suspected' : 'normal');
    }
    if ($('robot-status-title')) {
      $('robot-status-title').textContent = r.online
        ? `탐사로봇 연결됨 · ${r.reason_label || r.reason}`
        : `탐사로봇 오프라인 (${r.host || '192.168.1.206'})`;
    }
    if ($('robot-status-detail')) {
      const err = r.motor_error || r.camera_error;
      $('robot-status-detail').textContent = err
        ? `하드웨어 점검 필요: ${err}`
        : (r.video_message || `로봇 주소 ${r.host || '192.168.1.206'}:${r.port || 5000} · 실시간 영상 수신 및 AI 분석 중`);
    }
    if ($('robot-connection')) {
      $('robot-connection').textContent = r.video_online ? '실시간 영상 · AI 활성' : (r.online ? '텔레메트리 ONLINE' : '연결 대기');
    }
    if ($('robot-updated')) {
      $('robot-updated').textContent = `갱신 ${new Date().toLocaleTimeString('ko-KR', {hour12:false})}`;
    }
    if ($('robot-pc-badge')) {
      if (r.video_online && r.pc_connected) {
        $('robot-pc-badge').textContent = '조종 PC 주행 중 + Edge 관제 동시 수신';
      } else if (r.video_online) {
        $('robot-pc-badge').textContent = 'Edge 관제 영상 수신 중 (/monitor/video)';
      } else if (r.pc_connected) {
        $('robot-pc-badge').textContent = '외부 Windows 조종 PC 연결됨';
      } else {
        $('robot-pc-badge').textContent = '대기 중';
      }
    }
    if ($('robot-stream-toggle')) {
      const enabled = r.stream_enabled !== false;
      $('robot-stream-toggle').setAttribute('aria-pressed', String(enabled));
      $('robot-stream-toggle').textContent = enabled
        ? '실시간 관제 영상 수신 중 (일시정지)'
        : '관제 영상 일시정지됨 (다시 연결)';
    }
    if ($('robot-live-pill')) {
      $('robot-live-pill').textContent = r.video_online ? 'LIVE 수신 중' : (r.video_state === 'conflict_pc_active' ? '외부 PC 조종 중' : '연결 대기');
      $('robot-live-pill').dataset.live = String(Boolean(r.video_online));
    }
    if ($('robot-vision-mode') && v.mode && document.activeElement !== $('robot-vision-mode')) {
      $('robot-vision-mode').value = v.mode;
    }

    if ($('robot-direction')) {
      $('robot-direction').textContent = r.online ? `${r.direction_label || r.direction} (${r.pwm ?? 0}%)` : '—';
    }
    if ($('robot-distance')) {
      $('robot-distance').textContent = r.distance_cm != null
        ? `${r.distance_cm.toFixed(1)} cm${r.obstacle_warning ? ' (장애물 정지)' : ''}`
        : '—';
    }
    if ($('robot-climate')) {
      const dht = r.dht22;
      $('robot-climate').textContent = (dht && dht.connected && dht.temperature_c != null)
        ? `${dht.temperature_c.toFixed(1)}°C / ${dht.humidity_pct.toFixed(1)}%`
        : '측정 대기';
    }
    if ($('robot-gas')) {
      const mq = r.mq135;
      $('robot-gas').textContent = (mq && mq.connected && mq.voltage_v != null)
        ? `${mq.voltage_v.toFixed(3)} V ${mq.warming_up ? '(예열중)' : '(정상)'}`
        : '측정 대기';
    }
    if ($('robot-vision-summary')) {
      $('robot-vision-summary').textContent = v.summary || '탐지 대기';
    }
    if ($('robot-vision-perf')) {
      $('robot-vision-perf').textContent = v.inference_ms != null
        ? `${v.inference_ms} ms (${v.fps || 0} FPS)`
        : (v.mode_label || '대기');
    }

    if ($('robot-record-count')) $('robot-record-count').textContent = records.length;
    if ($('robot-stats-text')) {
      $('robot-stats-text').textContent = `총 ${ins.total_count || 0}건 · 이상 ${ins.abnormal_count || 0}건 · 정상 ${ins.normal_count || 0}건 (클릭 시 실시간 영상과 동시 확인)`;
    }
    if ($('robot-item-breakdown')) {
      const box = $('robot-item-breakdown');
      box.replaceChildren();
      const byItem = ins.by_item || {};
      for (const [name, count] of Object.entries(byItem)) {
        const badge = document.createElement('span');
        badge.className = 'item-pill';
        badge.dataset.warn = String(['안전모 미 착용', '벽면 균열', '노출 철근', '누수 흔적'].includes(name));
        badge.textContent = `${name} ${count}건`;
        box.append(badge);
      }
    }
    const key = records.map(x => x.event_id).join(',');
    if (robotHistoryKey !== key && $('robot-records')) {
      robotHistoryKey = key;
      const list = $('robot-records');
      list.replaceChildren();
      if (!records.length) {
        const p = document.createElement('p');
        p.className = 'empty';
        p.textContent = '저장된 탐사 분석 기록이 없습니다.';
        list.append(p);
      } else {
        for (const rec of records) {
          const item = document.createElement('article');
          item.className = 'event robot-event';
          item.dataset.type = rec.abnormal === '이상' ? 'fall_confirmed' : 'normal';
          item.dataset.eventId = rec.event_id;
          const title = document.createElement('strong');
          title.textContent = `[${rec.abnormal}] ${rec.item}`;
          const timeEl = document.createElement('time');
          timeEl.textContent = rec.timestamp;
          const scoreEl = document.createElement('p');
          scoreEl.textContent = `일치/신뢰도 ${(rec.match_score * 100).toFixed(1)}%`;
          const thumb = document.createElement('img');
          thumb.className = 'event-thumb';
          thumb.src = rec.overview_url;
          thumb.alt = `${rec.item} 캡처 미리보기`;
          thumb.loading = 'lazy';
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.textContent = '현재 실시간 영상 + 캡처 사진 함께 보기 ↗';
          item.addEventListener('click', () => selectRobotRecord(rec, {focusLive: true}));
          item.append(title, timeEl, scoreEl, thumb, btn);
          list.append(item);
        }
        const initial = records.find(x => x.event_id === selectedRobotEventId) || records[0];
        if (initial) selectRobotRecord(initial, {focusLive: false});
      }
    }
  }

  async function fetchRobot() {
    try {
      const response = await fetch('/api/robot/status', {cache:'no-store', signal:AbortSignal.timeout(4000)});
      if (!response.ok) return;
      showRobot(await response.json());
    } catch (_) {}
  }

  let tickCount = 0;
  async function poll() {
    if(!document.hidden) {
      try {
        const [status,events]=await Promise.all(['/api/status','/api/events'].map(async path=>{
          const response=await fetch(path,{cache:'no-store',signal:AbortSignal.timeout(4000)});
          if(!response.ok) throw new Error(); return response.json();
        })); showStatus(status);showEvents(events);
      } catch(_) {
        showBadge('offline');
        $('alarm').dataset.state='unobserved'; $('alarm-title').textContent='서버 연결 끊김';
        $('alarm-detail').textContent='최신 분석 상태를 확인할 수 없습니다. 자동으로 다시 연결합니다.';
        $('connection').textContent='응답 없음';
      }
      tickCount++;
      if (activeTab === 'robot' || tickCount % 2 === 1) fetchRobot();
    }
    setTimeout(poll,1000);
  }
  poll();
})();
