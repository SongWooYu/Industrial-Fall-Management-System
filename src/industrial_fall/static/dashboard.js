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
  function switchTab(tab) {
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
    window.dispatchEvent(new Event('online'));
    if (!isFall) fetchRobot();
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
  function openEvidenceModal(event) {
    $('evidence-title').textContent = `${eventTitles[event.type] || event.type} · ${date(event.timestamp)}`;
    $('evidence-image').src = event.snapshot_url;
    if ($('download-evidence')) {
      $('download-evidence').href = event.snapshot_url;
      $('download-evidence').download = `fall-capture-${event.id.slice(0, 8)}.jpg`;
    }
    $('evidence-detail').textContent = '감지한 프레임과 같은 시점의 박스·관절 캡처입니다. 클릭하여 저장하거나 나중에 다시 확인할 수 있습니다.';
    $('evidence').showModal();
  }
  let eventKey = null;
  function showEvents(events) {
    document.dispatchEvent(new CustomEvent('fall-events-updated', {detail:events}));
    const key = events.map(e => `${e.id}:${e.snapshot_url || ''}`).join(',');
    $('event-count').textContent = events.length;
    if (eventKey === key) return;
    eventKey = key;
    const confirmed = events.find(e => e.type === 'fall_confirmed');
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
    if (!events.length) { const p = document.createElement('p'); p.className='empty'; p.textContent='아직 감지 기록이 없습니다.'; $('events').append(p); }
    for (const event of events) {
      const item = document.createElement('article'); item.className='event'; item.dataset.type=event.type;
      const title = document.createElement('strong'); title.textContent=eventTitles[event.type] || event.type;
      const time = document.createElement('time'); time.dateTime=event.timestamp; time.textContent=date(event.timestamp);
      item.append(title,time);
      if (event.track) { const p = document.createElement('p'); p.textContent=`추적 #${event.track.track_id} · 모델 점수 ${(event.track.temporal_probability*100).toFixed(1)}%`; item.append(p); }
      if (event.snapshot_url) {
        const thumb = document.createElement('img');
        thumb.className = 'event-thumb';
        thumb.src = event.snapshot_url;
        thumb.alt = `${eventTitles[event.type] || event.type} 캡처 미리보기`;
        thumb.loading = 'lazy';
        thumb.addEventListener('click', () => openEvidenceModal(event));
        const button = document.createElement('button'); button.type='button'; button.textContent='낙상 순간 캡처 크게 보기 ↗';
        button.addEventListener('click', () => openEvidenceModal(event));
        item.append(thumb, button);
      }
      $('events').append(item);
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
  function selectRobotRecord(rec) {
    selectedRobotEventId = rec.event_id;
    document.querySelectorAll('#robot-records .event').forEach(el => {
      el.classList.toggle('selected', el.dataset.eventId === rec.event_id);
    });
    if ($('robot-selected-meta')) {
      $('robot-selected-meta').textContent = `${rec.timestamp} · ${rec.item} · 일치/신뢰도 ${(rec.match_score * 100).toFixed(1)}%`;
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
    }
    if (detImg && detEmpty) {
      detImg.src = rec.detail_url;
      detImg.hidden = false;
      detEmpty.hidden = true;
    }
  }

  function showRobot(data) {
    if (!data || !$('tab-robot')) return;
    const r = data.robot || {};
    const ins = data.inspection || {};
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
        : `로봇 주소 ${r.host || '192.168.1.206'}:${r.port || 5000} · 조종 간섭 없는 읽기 전용(Read-Only) 상태 수집 및 현장 탐사 DB 분석 화면입니다.`;
    }
    if ($('robot-connection')) {
      $('robot-connection').textContent = r.online ? '로봇 서버 ONLINE' : '연결 대기';
    }
    if ($('robot-updated')) {
      $('robot-updated').textContent = `갱신 ${new Date().toLocaleTimeString('ko-KR', {hour12:false})}`;
    }
    if ($('robot-pc-badge')) {
      $('robot-pc-badge').textContent = r.pc_connected ? '원격 조종 PC 연결됨 (주행 활성)' : '조종 PC 대기 중 (스탠바이)';
    }
    if ($('robot-direction')) {
      $('robot-direction').textContent = r.online ? `${r.direction_label || r.direction}` : '—';
    }
    if ($('robot-pwm')) {
      $('robot-pwm').textContent = r.online ? `${r.pwm ?? 0} %` : '—';
    }
    if ($('robot-distance')) {
      $('robot-distance').textContent = r.distance_cm != null
        ? `${r.distance_cm.toFixed(1)} cm${r.obstacle_warning ? ' (장애물 주의)' : ''}`
        : '—';
    }
    if ($('robot-health')) {
      const d = r.diagnostics || {};
      $('robot-health').textContent = r.online
        ? (r.motor_error || r.camera_error ? '오류 감지' : `모터 ${d.motor_age_sec ?? 0}s / 카메라 ${d.camera_age_sec ?? 0}s`)
        : '—';
    }

    const records = ins.records || [];
    if ($('robot-record-count')) $('robot-record-count').textContent = records.length;
    if ($('robot-stats-text')) {
      $('robot-stats-text').textContent = `총 ${ins.total_count || 0}건 · 이상 감지 ${ins.abnormal_count || 0}건 · 정상 확인 ${ins.normal_count || 0}건`;
    }
    if ($('robot-item-breakdown')) {
      const box = $('robot-item-breakdown');
      box.replaceChildren();
      const byItem = ins.by_item || {};
      for (const [name, count] of Object.entries(byItem)) {
        const badge = document.createElement('span');
        badge.className = 'item-pill';
        badge.dataset.warn = String(name !== '사람');
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
          scoreEl.textContent = `분석 일치/신뢰도 ${(rec.match_score * 100).toFixed(1)}%`;
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.textContent = '전체·부분 캡처 분석 보기';
          btn.addEventListener('click', () => selectRobotRecord(rec));
          item.addEventListener('click', () => selectRobotRecord(rec));
          item.append(title, timeEl, scoreEl, btn);
          list.append(item);
        }
        const initial = records.find(x => x.event_id === selectedRobotEventId) || records[0];
        if (initial) selectRobotRecord(initial);
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
      if (activeTab === 'robot') fetchRobot();
    }
    setTimeout(poll,1000);
  }
  poll();
})();
