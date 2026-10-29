/* Browser-only speech: no server commands, event injection or audio uploads. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const synth = window.speechSynthesis;
  let enabled = false, generation = 0, speechTimer, finishTimer, utterance;
  let context, oscillator, voice, unlocked = Promise.resolve();
  const volume = () => Number($('audio-volume').value) / 100;
  const status = text => { $('audio-status').textContent = text; };
  function loadVoice() {
    const voices = synth?.getVoices() || [];
    voice = voices.find(v => /^ko([_-]|$)/i.test(v.lang) && v.localService)
      || voices.find(v => /^ko([_-]|$)/i.test(v.lang));
  }
  loadVoice();
  synth?.addEventListener('voiceschanged', loadVoice);
  // Create/resume only in a click handler. Reuse one context; never queue tones.
  function unlock() {
    const Audio = window.AudioContext || window.webkitAudioContext;
    try {
      if (!context && Audio) context = new Audio();
      if (context?.state === 'suspended') unlocked = context.resume().catch(() => {});
    } catch (_) {}
  }
  function stop() {
    generation++;
    clearTimeout(speechTimer); clearTimeout(finishTimer);
    utterance = null;
    synth?.cancel();
    if (oscillator) { try { oscillator.stop(); } catch (_) {} oscillator = null; }
  }
  async function chime(token, reason) {
    await unlocked;
    if (token !== generation) return;
    if (!context || context.state !== 'running') {
      status(`${reason} · 소리만 테스트를 다시 눌러 브라우저 소리 허용 여부를 확인해 주세요.`);
      return;
    }
    try {
      const now = context.currentTime, gain = context.createGain();
      const tone = oscillator = context.createOscillator();
      tone.frequency.setValueAtTime(740, now);
      tone.frequency.setValueAtTime(550, now + .24);
      gain.gain.setValueAtTime(0, now);
      gain.gain.linearRampToValueAtTime(volume() * .18, now + .02);
      gain.gain.setValueAtTime(volume() * .18, now + .48);
      gain.gain.linearRampToValueAtTime(0, now + .55);
      tone.connect(gain); gain.connect(context.destination);
      tone.onended = () => { tone.disconnect(); gain.disconnect(); if (oscillator === tone) oscillator = null; };
      tone.start(now); tone.stop(now + .56);
      status(`${reason} · 대신 경고음을 재생합니다. 자동 알림 ${enabled ? '켜짐' : '꺼짐'}.`);
    } catch (_) { status('소리를 재생하지 못했습니다. 브라우저·기기의 음소거 설정을 확인해 주세요.'); }
  }
  function speak(text, purpose) {
    stop();
    const token = generation;
    if (volume() === 0) { status('음량 0% · 소리가 꺼져 있습니다.'); return; }
    loadVoice();
    if (!synth || !window.SpeechSynthesisUtterance || !voice) {
      chime(token, '한국어 음성을 사용할 수 없음'); return;
    }
    try {
      const speech = utterance = new SpeechSynthesisUtterance(text);
      speech.voice = voice; speech.lang = 'ko-KR'; speech.rate = 1; speech.volume = volume();
      speech.onstart = () => {
        if (token !== generation) return;
        clearTimeout(speechTimer);
        status(`${purpose} · ${voice.name} · 기기 음량도 확인해 주세요.`);
      };
      speech.onend = () => {
        if (token !== generation) return;
        clearTimeout(speechTimer); clearTimeout(finishTimer); utterance = null;
        status(`${purpose} 완료 · 자동 알림 ${enabled ? '켜짐' : '꺼짐'}.`);
      };
      speech.onerror = event => {
        if (token !== generation || ['canceled', 'interrupted'].includes(event.error)) return;
        stop(); chime(generation, '음성 재생 실패');
      };
      // Engines can silently fail or hang. One active request, bounded lifetime.
      speechTimer = setTimeout(() => { if (token === generation) { stop(); chime(generation, '음성 시작 응답 없음'); } }, 4000);
      finishTimer = setTimeout(() => { if (token === generation) { stop(); status('음성 재생 시간 초과 · 소리만 테스트로 확인해 주세요.'); } }, 15000);
      synth.speak(speech);
    } catch (_) { stop(); chime(generation, '음성 기능 사용 불가'); }
  }
  $('audio-enable').addEventListener('click', () => {
    enabled = !enabled;
    $('audio-enable').setAttribute('aria-pressed', String(enabled));
    $('audio-enable').textContent = enabled ? '음성 알림 끄기' : '음성 알림 켜기';
    if (enabled) { unlock(); speak('낙상 음성 알림을 켰습니다.', '음성 알림 활성화'); }
    else { stop(); status('꺼짐 · 자동 음성 알림을 중지했습니다.'); }
  });
  $('audio-test').addEventListener('click', () => {
    unlock(); speak('낙상이 감지되었습니다.', '소리만 테스트');
  });
  $('audio-volume').addEventListener('input', () => {
    $('audio-volume-value').textContent = `${$('audio-volume').value}%`;
    if (!volume()) stop();
    status(`다음 재생 음량 ${$('audio-volume').value}% · 자동 알림 ${enabled ? '켜짐' : '꺼짐'}.`);
  });
  // Consume every event even while muted. Initial history / long disconnections
  // are a baseline, not a new accident; never replay a backlog on page return.
  const seen = new Set();
  let sampledAt = null;
  document.addEventListener('fall-events-updated', event => {
    const now = performance.now();
    const live = sampledAt !== null && now - sampledAt < 15000;
    sampledAt = now;
    const fresh = event.detail.filter(e => !seen.has(e.id));
    for (const e of event.detail) seen.add(e.id);
    while (seen.size > 200) seen.delete(seen.values().next().value);
    if (enabled && live && !document.hidden && fresh.some(e => e.type === 'fall_confirmed')) {
      speak('낙상이 감지되었습니다. 현장을 확인해 주세요.', '낙상 감지 알림');
    }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { sampledAt = null; stop(); }
  });
  window.addEventListener('pagehide', stop);
})();
