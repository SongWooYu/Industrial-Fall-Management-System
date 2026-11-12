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

  const BLANK_GIF = 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=';
  let activeTab = 'fall';

  function startRobotStreamIfNeeded(force = false) {
    const img = $('robot-live-stream');
    if (!img) return;
    if (activeTab !== 'robot' || document.hidden) return;
    if (!force && img.dataset.streaming === 'true') return;
    const mode = $('robot-analysis-mode')?.value || 'off';
    const analysisParam = mode !== 'off' ? '1' : '0';
    img.dataset.streaming = 'true';
    img.dataset.streamError = 'false';
    img.src = `/stream/robot?live=1&analysis=${analysisParam}&mode=${encodeURIComponent(mode)}&t=${Date.now()}`;
    if ($('robot-stream-label')) {
      $('robot-stream-label').textContent = mode !== 'off'
        ? `실시간 AI 분석 오버레이 (${mode})`
        : '원본 MJPEG 릴레이 (/monitor/video)';
    }
    updateRobotVideoOverlay();
  }

  function stopRobotStream() {
    const img = $('robot-live-stream');
    if (!img) return;
    if (img.dataset.streaming === 'false' && img.src === BLANK_GIF) return;
    img.dataset.streaming = 'false';
    img.src = BLANK_GIF;
    updateRobotVideoOverlay();
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
    if (isFall) {
      stopRobotStream();
    } else {
      startRobotStreamIfNeeded(true);
      fetchRobot();
    }
  }
  $('tab-btn-fall')?.addEventListener('click', () => switchTab('fall'));
  $('tab-btn-robot')?.addEventListener('click', () => switchTab('robot'));
  $('robot-video-retry')?.addEventListener('click', () => {
    stopRobotStream();
    startRobotStreamIfNeeded(true);
    fetchRobot();
  });
  async function updateRobotAnalysisConfig() {
    const mode = $('robot-analysis-mode')?.value || 'off';
    const autoRecord = Boolean($('robot-auto-record')?.checked);
    try {
      const resp = await fetch('/api/robot/analysis', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mode, auto_record: autoRecord}),
      });
      if (resp.ok) showRobot(await resp.json());
    } catch (_) {}
  }
  $('robot-analysis-mode')?.addEventListener('change', () => {
    updateRobotAnalysisConfig();
    if (activeTab === 'robot') {
      startRobotStreamIfNeeded(true);
    }
  });
  $('robot-auto-record')?.addEventListener('change', () => {
    updateRobotAnalysisConfig();
  });
  $('robot-capture-now')?.addEventListener('click', async () => {
    const btn = $('robot-capture-now');
    if (btn) btn.disabled = true;
    try {
      const resp = await fetch('/api/robot/capture-now', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({}),
      });
      if (resp.ok) {
        const data = await resp.json();
        if (data.status) showRobot(data.status);
        if (data.capture?.event_id) {
          const rec = robotRecordsCache.find(x => x.event_id === data.capture.event_id);
          if (rec) selectRobotRecord(rec);
        }
      }
    } catch (_) {
    } finally {
      if (btn) btn.disabled = false;
    }
  });
  $('robot-live-stream')?.addEventListener('error', () => {
    const img = $('robot-live-stream');
    if (!img || img.dataset.streaming !== 'true') return;
    img.dataset.streamError = 'true';
    updateRobotVideoOverlay();
  });
  $('robot-live-stream')?.addEventListener('load', () => {
    const img = $('robot-live-stream');
    if (!img || img.dataset.streaming !== 'true') return;
    img.dataset.streamError = 'false';
    updateRobotVideoOverlay();
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      stopRobotStream();
    } else if (activeTab === 'robot') {
      startRobotStreamIfNeeded(true);
      fetchRobot();
    }
  });
  window.addEventListener('pagehide', stopRobotStream);

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

  const ROBOT_PAGE_SIZE = 15;
  let selectedRobotEventId = null;
  let robotHistoryKey = null;
  let robotRecordsCache = [];
  let robotCurrentPage = 1;
  let robotInitialRecordsLoaded = false;
  const seenRobotEventIds = new Set();
  let lastRobotState = {};

  function setTextIfChanged(el, text) {
    if (!el) return;
    const next = String(text ?? '');
    if (el.textContent !== next) el.textContent = next;
  }

  function shortSourceLabel(source, fallback) {
    if (source === 'windows_client') return 'Windows PC';
    if (source === 'edge_analysis') return 'Edge 분석';
    if (source === 'robot_event') return '로봇 자체';
    if (source === 'legacy_import') return '기존 보관본';
    return fallback || '탐사 기록';
  }

  function updateRobotVideoOverlay() {
    const overlay = $('robot-video-overlay');
    const titleEl = $('robot-video-overlay-title');
    const detailEl = $('robot-video-overlay-detail');
    const pill = $('robot-live-pill');
    const img = $('robot-live-stream');
    if (!overlay || !titleEl || !detailEl) return;

    const r = lastRobotState || {};
    const streamingRequested = img?.dataset.streaming === 'true';
    const streamError = img?.dataset.streamError === 'true';

    if (activeTab !== 'robot') {
      overlay.hidden = false;
      overlay.dataset.state = 'idle';
      setTextIfChanged(titleEl, '실시간 영상 대기');
      setTextIfChanged(detailEl, '로봇 관제 탭이 열리면 /monitor/video 스트림을 수신합니다.');
      if (pill) {
        setTextIfChanged(pill, '대기');
        pill.dataset.live = 'false';
        pill.dataset.error = 'false';
      }
      return;
    }

    if (r.video_online && streamingRequested && !streamError) {
      overlay.hidden = true;
      overlay.dataset.state = 'streaming';
      if (pill) {
        setTextIfChanged(pill, 'LIVE 수신 중');
        pill.dataset.live = 'true';
        pill.dataset.error = 'false';
      }
      return;
    }

    overlay.hidden = false;
    let state = r.video_state || 'connecting';
    let title = '실시간 영상 연결 중';
    let detail = r.video_message || '로봇 /monitor/video 스트림 연결을 준비하고 있습니다.';
    let pillText = '연결 중';
    let isError = false;

    if (r.online === false || state === 'robot_offline') {
      state = 'robot_offline';
      title = '로봇 오프라인';
      detail = r.video_message || `로봇 서버(${r.host || '192.168.1.206'}:${r.port || 5000})에 연결할 수 없습니다.`;
      pillText = '로봇 오프라인';
      isError = true;
    } else if (r.camera_error || state === 'camera_error') {
      state = 'camera_error';
      title = '카메라 오류';
      detail = r.camera_error ? `로봇 카메라 오류: ${r.camera_error}` : (r.video_message || '로봇 카메라 프레임을 획득할 수 없습니다.');
      pillText = '카메라 오류';
      isError = true;
    } else if (state === 'video_failed' || streamError) {
      state = 'video_failed';
      title = '영상 연결 실패';
      detail = r.video_message || '로봇 /monitor/video 스트림 연결이 중단되었습니다. [영상 재연결]을 눌러 다시 시도할 수 있습니다.';
      pillText = '영상 연결 실패';
      isError = true;
    }

    overlay.dataset.state = state;
    setTextIfChanged(titleEl, title);
    setTextIfChanged(detailEl, detail);
    if (pill) {
      setTextIfChanged(pill, pillText);
      pill.dataset.live = 'false';
      pill.dataset.error = String(isError);
    }
  }

  function selectRobotRecord(rec) {
    if (!rec) return;
    selectedRobotEventId = rec.event_id;
    document.querySelectorAll('#robot-records .robot-history-row').forEach(el => {
      el.classList.toggle('selected', el.dataset.eventId === rec.event_id);
    });
    const badge = $('robot-selected-badge');
    if (badge) {
      setTextIfChanged(badge, `${rec.item} (${rec.abnormal})`);
      badge.dataset.abnormal = rec.abnormal;
    }
    setTextIfChanged($('robot-sel-item'), rec.item || '—');
    setTextIfChanged($('robot-sel-time'), rec.timestamp || '—');
    const abnEl = $('robot-sel-abnormal');
    if (abnEl) {
      setTextIfChanged(abnEl, rec.abnormal || '—');
      abnEl.dataset.abnormal = rec.abnormal || '';
    }
    setTextIfChanged($('robot-sel-source'), rec.source_label || shortSourceLabel(rec.source));
    const scorePct = ((Number(rec.match_score) || 0) * 100).toFixed(1);
    const metricLabel = rec.score_label || '일치/신뢰도 점수';
    const scoreText = `${metricLabel}: ${scorePct}% (${rec.score_note || '안전/위험 확률 아님'})`;
    const scoreEl = $('robot-sel-score');
    if (scoreEl) {
      setTextIfChanged(scoreEl, scoreText);
      scoreEl.title = scoreText;
    }

    const overImg = $('robot-overview-img'), overEmpty = $('robot-overview-empty');
    const detImg = $('robot-detail-img'), detEmpty = $('robot-detail-empty');
    if (overImg && overEmpty) {
      if (overImg.getAttribute('src') !== rec.overview_url) {
        overImg.src = rec.overview_url;
      }
      overImg.hidden = false;
      overEmpty.hidden = true;
      overImg.onclick = () => openEvidenceModal(
        `[${rec.abnormal}] ${rec.item} — 발견 당시 전체 영상 (${rec.timestamp})`,
        rec.overview_url,
        `${rec.source_label || shortSourceLabel(rec.source)} · ${metricLabel} ${scorePct}% (사고 확률 아님)`,
        `robot-overview-${rec.event_id.slice(0, 8)}.jpg`
      );
    }
    if (detImg && detEmpty) {
      if (detImg.getAttribute('src') !== rec.detail_url) {
        detImg.src = rec.detail_url;
      }
      detImg.hidden = false;
      detEmpty.hidden = true;
      detImg.onclick = () => openEvidenceModal(
        `[${rec.abnormal}] ${rec.item} — 검출 대상 확대 (${rec.timestamp})`,
        rec.detail_url,
        `${rec.source_label || shortSourceLabel(rec.source)} · ${metricLabel} ${scorePct}% (사고 확률 아님)`,
        `robot-detail-${rec.event_id.slice(0, 8)}.jpg`
      );
    }
  }

  function renderRobotHistoryPage() {
    const list = $('robot-records');
    if (!list) return;
    const total = robotRecordsCache.length;
    const totalPages = Math.max(1, Math.ceil(total / ROBOT_PAGE_SIZE));
    if (robotCurrentPage > totalPages) robotCurrentPage = totalPages;
    if (robotCurrentPage < 1) robotCurrentPage = 1;

    setTextIfChanged($('robot-page-info'), `${robotCurrentPage} / ${totalPages} 페이지 (총 ${total}건)`);
    if ($('robot-page-prev')) $('robot-page-prev').disabled = robotCurrentPage <= 1;
    if ($('robot-page-next')) $('robot-page-next').disabled = robotCurrentPage >= totalPages;

    list.replaceChildren();
    if (!total) {
      const p = document.createElement('p');
      p.className = 'empty';
      p.textContent = '저장된 탐사 분석 기록이 없습니다.';
      list.append(p);
      return;
    }

    const startIdx = (robotCurrentPage - 1) * ROBOT_PAGE_SIZE;
    const pageItems = robotRecordsCache.slice(startIdx, startIdx + ROBOT_PAGE_SIZE);
    for (const rec of pageItems) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'robot-history-row';
      if (rec.event_id === selectedRobotEventId) row.classList.add('selected');
      row.dataset.eventId = rec.event_id;

      const timeSpan = document.createElement('span');
      timeSpan.textContent = rec.timestamp;
      timeSpan.title = rec.timestamp;

      const itemSpan = document.createElement('span');
      itemSpan.textContent = rec.item;
      itemSpan.title = rec.item;

      const abnSpan = document.createElement('span');
      abnSpan.className = 'robot-row-badge';
      abnSpan.dataset.abnormal = rec.abnormal;
      abnSpan.textContent = rec.abnormal;

      const srcSpan = document.createElement('span');
      srcSpan.className = 'robot-row-source';
      srcSpan.textContent = shortSourceLabel(rec.source, rec.source_label);
      srcSpan.title = rec.source_label || '';

      row.append(timeSpan, itemSpan, abnSpan, srcSpan);
      row.addEventListener('click', () => selectRobotRecord(rec));
      list.append(row);
    }
  }

  $('robot-page-prev')?.addEventListener('click', () => {
    if (robotCurrentPage > 1) {
      robotCurrentPage -= 1;
      renderRobotHistoryPage();
    }
  });
  $('robot-page-next')?.addEventListener('click', () => {
    const totalPages = Math.max(1, Math.ceil(robotRecordsCache.length / ROBOT_PAGE_SIZE));
    if (robotCurrentPage < totalPages) {
      robotCurrentPage += 1;
      renderRobotHistoryPage();
    }
  });

  function showRobotToast(rec) {
    const container = $('robot-toast-container');
    if (!container || !rec) return;
    const toast = document.createElement('div');
    toast.className = 'robot-toast';
    toast.dataset.eventId = rec.event_id;

    const body = document.createElement('div');
    body.className = 'robot-toast-body';
    const title = document.createElement('strong');
    title.textContent = `탐사로봇 [이상] ${rec.item} 감지`;
    const detail = document.createElement('p');
    detail.textContent = `${rec.timestamp} · ${rec.source_label || shortSourceLabel(rec.source)}`;
    const hint = document.createElement('small');
    hint.textContent = '클릭하면 안전탐색 로봇 관제 탭에서 캡처와 실시간 영상을 확인합니다.';
    body.append(title, detail, hint);

    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'robot-toast-close';
    closeBtn.setAttribute('aria-label', '알림 닫기');
    closeBtn.textContent = '닫기 ×';

    body.addEventListener('click', () => {
      if (activeTab !== 'robot') switchTab('robot');
      const idx = robotRecordsCache.findIndex(x => x.event_id === rec.event_id);
      if (idx >= 0) {
        robotCurrentPage = Math.floor(idx / ROBOT_PAGE_SIZE) + 1;
        renderRobotHistoryPage();
      }
      selectRobotRecord(rec);
      toast.remove();
    });
    closeBtn.addEventListener('click', e => {
      e.stopPropagation();
      toast.remove();
    });

    toast.append(body, closeBtn);
    container.prepend(toast);
    while (container.children.length > 3) {
      container.lastElementChild?.remove();
    }
  }

  function showRobot(data) {
    if (!data || !$('tab-robot')) return;
    const r = data.robot || {};
    const ins = data.inspection || {};
    const records = ins.records || [];
    lastRobotState = r;

    if (!robotInitialRecordsLoaded) {
      for (const rec of records) seenRobotEventIds.add(rec.event_id);
      robotInitialRecordsLoaded = true;
    } else {
      let newestArrived = null;
      for (const rec of records) {
        if (!seenRobotEventIds.has(rec.event_id)) {
          seenRobotEventIds.add(rec.event_id);
          if (!newestArrived) newestArrived = rec;
          if (rec.abnormal === '이상' && rec.source !== 'legacy_import') {
            showRobotToast(rec);
          }
        }
      }
      if (newestArrived && robotPage === 1) {
        selectedRobotEventId = newestArrived.event_id;
      }
    }

    const hostPort = `${r.host || '192.168.1.206'}:${r.port || 5000}`;
    setTextIfChanged($('robot-video-host-label'), `${hostPort} · /monitor/video (640×480)`);
    if ($('robot-analysis-mode') && r.analysis_mode && document.activeElement !== $('robot-analysis-mode')) {
      $('robot-analysis-mode').value = r.analysis_mode;
    }
    if ($('robot-auto-record') && typeof r.auto_record === 'boolean' && document.activeElement !== $('robot-auto-record')) {
      $('robot-auto-record').checked = r.auto_record;
    }
    if ($('robot-vision-summary')) {
      const infTxt = r.vision_inference_ms != null ? ` · ${r.vision_inference_ms}ms` : '';
      setTextIfChanged($('robot-vision-summary'), `${r.vision_summary || '원본 영상 모드 (분석 끔)'}${infTxt}`);
    }

    // 1. Robot connection cell
    const connCell = $('robot-cell-conn');
    if (connCell) {
      connCell.dataset.status = r.online ? (r.motor_error || r.camera_error ? 'warn' : 'ok') : 'error';
      connCell.title = r.error || r.video_message || `${hostPort} 상태 정상`;
    }
    setTextIfChanged($('robot-conn-state'), r.online ? 'ONLINE (정상 연결)' : 'OFFLINE (연결 끊김)');
    setTextIfChanged(
      $('robot-conn-sub'),
      r.motor_error ? `모터 오류: ${r.motor_error}` : (r.camera_error ? `카메라 오류: ${r.camera_error}` : hostPort)
    );

    // 2. Controller & video cell
    const pcCell = $('robot-cell-pc');
    if (pcCell) {
      pcCell.dataset.status = r.video_online ? 'ok' : (r.pc_connected ? 'ok' : '');
      pcCell.title = r.video_message || '';
    }
    setTextIfChanged($('robot-pc-badge'), r.pc_connected ? 'Windows 조종 PC 연결됨' : '조종 PC 미연결 (대기)');
    setTextIfChanged(
      $('robot-video-sub'),
      r.video_online
        ? `/monitor/video 수신 중 (관제 ${r.viewer_count || 1}명)`
        : (activeTab === 'robot' ? (r.video_message || '영상 연결 준비 중') : '탭 비활성 · 영상 수신 대기')
    );

    // 3. Driving direction & PWM cell
    const driveCell = $('robot-cell-drive');
    if (driveCell) {
      driveCell.dataset.status = r.motor_error ? 'error' : '';
      driveCell.title = r.motor_error ? `모터 오류: ${r.motor_error}` : `${r.reason_label || r.reason || ''}`;
    }
    setTextIfChanged(
      $('robot-direction'),
      r.online ? `${r.direction_label || r.direction || '정지'} · PWM ${r.pwm ?? 0}%` : '오프라인 (—)'
    );
    setTextIfChanged($('robot-reason-sub'), r.online ? (r.reason_label || r.reason || '대기') : '주행 상태 확인 불가');

    // 4. Ultrasonic distance cell
    const distCell = $('robot-cell-dist');
    if (distCell) {
      distCell.dataset.status = r.obstacle_warning ? 'warn' : (r.online && r.distance_cm != null ? 'ok' : '');
      distCell.title = r.obstacle_warning ? '전방 20cm 이내 장애물 감지 정지' : '초음파 거리 센서';
    }
    setTextIfChanged(
      $('robot-distance'),
      r.online && r.distance_cm != null ? `${Number(r.distance_cm).toFixed(1)} cm` : (r.online ? '측정 대기' : '오프라인 (—)')
    );
    setTextIfChanged(
      $('robot-obstacle-sub'),
      !r.online ? '측정 중단' : (r.obstacle_warning ? '전방 장애물 근접 경고' : '전방 안전 거리 확보')
    );

    // 5. DHT22 cell
    const dhtEval = r.dht22_eval || {};
    const dhtCell = $('robot-cell-dht');
    if (dhtCell) {
      dhtCell.dataset.status = dhtEval.state === 'ok' ? 'ok' : (dhtEval.state === 'error' || dhtEval.state === 'offline' ? 'error' : 'warn');
      dhtCell.title = dhtEval.detail || dhtEval.label || 'DHT22 센서';
    }
    setTextIfChanged($('robot-climate'), dhtEval.label || '측정 대기');
    setTextIfChanged($('robot-climate-sub'), dhtEval.detail || (r.online ? 'DHT22 센서' : '로봇 오프라인'));

    // 6. MQ-135 cell (never display uncalibrated voltage as '정상')
    const mqEval = r.mq135_eval || {};
    const mqCell = $('robot-cell-mq');
    if (mqCell) {
      mqCell.dataset.status = mqEval.state === 'error' || mqEval.state === 'offline' ? 'error' : 'warn';
      mqCell.title = mqEval.detail || 'MQ-135 미보정 원시 전압';
    }
    setTextIfChanged($('robot-gas'), mqEval.label || '미보정 · 판정 불가');
    setTextIfChanged($('robot-gas-sub'), mqEval.detail || '보정 전 정상/위험 판정 불가');

    updateRobotVideoOverlay();

    setTextIfChanged($('robot-record-count'), records.length);
    setTextIfChanged(
      $('robot-stats-text'),
      `총 ${ins.total_count || 0}건 · 이상 ${ins.abnormal_count || 0}건 · 정상 ${ins.normal_count || 0}건`
    );

    const key = records.map(x => `${x.event_id}:${x.abnormal}:${x.source}`).join(',');
    if (robotHistoryKey !== key) {
      robotHistoryKey = key;
      robotRecordsCache = records;
      if (!selectedRobotEventId && records.length) {
        selectedRobotEventId = records[0].event_id;
      }
      renderRobotHistoryPage();
      const selected = records.find(x => x.event_id === selectedRobotEventId) || records[0];
      if (selected) selectRobotRecord(selected);
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
