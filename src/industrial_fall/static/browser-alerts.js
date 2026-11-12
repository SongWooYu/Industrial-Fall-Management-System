/* Browser-only natural speech alert: fixed SunHiNeural MP3 clip, client OS volume. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const naturalUrl = '/static/audio/fall-detected-ko-sunhi-v1.mp3';
  let enabled = false, generation = 0, speechTimer, finishTimer;
  let context, oscillator, unlocked = Promise.resolve();
  let clipSource, clipPromise;
  const status = text => { $('audio-status').textContent = text; };

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
    if (oscillator) { try { oscillator.stop(); } catch (_) {} oscillator = null; }
    if (clipSource) { try { clipSource.stop(); } catch (_) {} clipSource = null; }
  }

  async function chime(token, reason, replacement = true) {
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
      gain.gain.linearRampToValueAtTime(.18, now + .02);
      gain.gain.setValueAtTime(.18, now + .48);
      gain.gain.linearRampToValueAtTime(0, now + .55);
      tone.connect(gain); gain.connect(context.destination);
      tone.onended = () => { tone.disconnect(); gain.disconnect(); if (oscillator === tone) oscillator = null; };
      tone.start(now); tone.stop(now + .56);
      status(`${reason} · ${replacement ? '대신 경고음' : '확인음'}을 재생합니다. 자동 알림 ${enabled ? '켜짐' : '꺼짐'}.`);
    } catch (_) {
      status('소리를 재생하지 못했습니다. 브라우저·기기의 음소거 설정을 확인해 주세요.');
    }
  }

  function loadClip() {
    if (!clipPromise) {
      clipPromise = (async () => {
        const response = await fetch(naturalUrl, {signal: AbortSignal.timeout(4000)});
        if (!response.ok) throw new Error('audio unavailable');
        const data = await response.arrayBuffer();
        if (!data.byteLength || data.byteLength > 256 * 1024) throw new Error('invalid clip size');
        const clip = await context.decodeAudioData(data);
        if (!clip.duration || clip.duration > 10) throw new Error('invalid clip duration');
        return clip;
      })().catch(error => { clipPromise = null; throw error; });
    }
    return clipPromise;
  }

  async function naturalSpeech(purpose, token) {
    const fallback = () => {
      if (token !== generation) return;
      stop();
      chime(generation, '안내형 음성 재생 불가');
    };
    speechTimer = setTimeout(fallback, 6000);
    try {
      await unlocked;
      if (token !== generation) return;
      if (!context || context.state !== 'running') throw new Error('audio context unavailable');
      status(`${purpose} · 자연스러운 음성 준비 중`);
      const buffer = await loadClip();
      if (token !== generation) return;
      const source = clipSource = context.createBufferSource();
      const gain = context.createGain();
      source.buffer = buffer; gain.gain.value = 1;
      source.connect(gain); gain.connect(context.destination);
      source.onended = () => {
        source.disconnect(); gain.disconnect();
        if (clipSource === source) clipSource = null;
        if (token !== generation) return;
        clearTimeout(finishTimer);
        status(`${purpose} 완료 · 안내형 · 자동 알림 ${enabled ? '켜짐' : '꺼짐'}.`);
      };
      clearTimeout(speechTimer);
      finishTimer = setTimeout(() => {
        if (token === generation) { stop(); status('안내형 재생 시간 초과 · 소리만 테스트로 확인해 주세요.'); }
      }, 15000);
      source.start();
      status(`${purpose} · 자연스러운 안내형 음성 재생 중`);
    } catch (_) { fallback(); }
  }

  function speak(purpose) {
    stop();
    naturalSpeech(purpose, generation);
  }

  $('audio-enable').addEventListener('click', () => {
    enabled = !enabled;
    $('audio-enable').setAttribute('aria-pressed', String(enabled));
    $('audio-enable').textContent = enabled ? '음성 알림 끄기' : '음성 알림 켜기';
    if (enabled) {
      unlock();
      stop();
      chime(generation, '자연스러운 음성 알림 켜짐', false);
    } else {
      stop();
      status('꺼짐 · 자동 음성 알림을 중지했습니다.');
    }
  });

  $('audio-test').addEventListener('click', () => {
    unlock();
    speak('소리만 테스트');
  });

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
      speak('낙상 감지 알림');
    }
  });

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { sampledAt = null; stop(); }
  });
  window.addEventListener('pagehide', stop);
})();
