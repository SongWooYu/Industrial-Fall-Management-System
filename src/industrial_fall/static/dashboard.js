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
  const titles = {normal:'확정 경보 없음', suspected:'낙상 의심 · 확인 중', fallen:'낙상 감지 · 현장 확인 필요', recovering:'회복 동작 관측 중', unobserved:'판정 대상 없음'};
  const eventTitles = {fall_confirmed:'낙상 감지',fall_suspected:'낙상 의심',fall_recovering:'회복 동작 관측',fall_cleared:'감지 상태 해제',tracking_lost:'추적 종료 · 회복 여부 미확인'};
  const date = value => new Date(value).toLocaleString('ko-KR', {hour12:false});
  let eventKey = '';
  function showEvents(events) {
    const key = events.map(e => e.id).join(',');
    $('event-count').textContent = events.length;
    if (eventKey === key) return;
    eventKey = key;
    $('events').replaceChildren();
    if (!events.length) { const p = document.createElement('p'); p.className='empty'; p.textContent='아직 감지 기록이 없습니다.'; $('events').append(p); }
    for (const event of events) {
      const item = document.createElement('article'); item.className='event'; item.dataset.type=event.type;
      const title = document.createElement('strong'); title.textContent=eventTitles[event.type] || event.type;
      const time = document.createElement('time'); time.dateTime=event.timestamp; time.textContent=date(event.timestamp);
      item.append(title,time);
      if (event.track) { const p = document.createElement('p'); p.textContent=`추적 #${event.track.track_id} · 모델 점수 ${(event.track.temporal_probability*100).toFixed(1)}%`; item.append(p); }
      if (event.snapshot_url) {
        const button = document.createElement('button'); button.type='button'; button.textContent='낙상 순간 캡처 확인 ↗';
        button.addEventListener('click', () => {
          $('evidence-title').textContent=`${eventTitles[event.type]} · ${date(event.timestamp)}`;
          $('evidence-image').src=event.snapshot_url;
          $('evidence-detail').textContent='감지한 프레임과 같은 시점의 박스·관절입니다. 현재 실시간 영상이 아닙니다.';
          $('evidence').showModal();
        }); item.append(button);
      }
      $('events').append(item);
    }
  }
  $('evidence-image').addEventListener('error',()=>{$('evidence-detail').textContent='보관 시간이 지났거나 서비스가 재시작되어 캡처를 사용할 수 없습니다.';});
  $('close-evidence').addEventListener('click',()=>$('evidence').close());
  $('evidence').addEventListener('click',e=>{if(e.target===$('evidence'))$('evidence').close();});
  function showStatus(s) {
    const ready=s.analysis;
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
  async function poll() {
    if(!document.hidden) {
      try {
        const [status,events]=await Promise.all(['/api/status','/api/events'].map(async path=>{
          const response=await fetch(path,{cache:'no-store',signal:AbortSignal.timeout(4000)});
          if(!response.ok) throw new Error(); return response.json();
        })); showStatus(status);showEvents(events);
      } catch(_) {
        $('alarm').dataset.state='unobserved'; $('alarm-title').textContent='서버 연결 끊김';
        $('alarm-detail').textContent='최신 분석 상태를 확인할 수 없습니다. 자동으로 다시 연결합니다.';
        $('connection').textContent='응답 없음';
      }
    }
    setTimeout(poll,1000);
  }
  poll();
})();
