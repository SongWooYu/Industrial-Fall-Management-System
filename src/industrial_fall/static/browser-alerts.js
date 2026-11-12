/* Browser-only natural speech alert: default enabled, SunHiNeural MP3 clip, client OS volume. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const naturalUrl = '/static/audio/fall-detected-ko-sunhi-v1.mp3';
  const enabled = true;
  let generation = 0, speechTimer, finishTimer;
  let context, oscillator, unlocked = Promise.resolve();
  let clipSource, clipPromise;
  const status = (text, ready = false) => {
    const el = $('audio-status');
    if (!el) return;
    el.textContent = text;
    el.dataset.ready = String(Boolean(ready));
  };

  function unlock() {
    const Audio = window.AudioContext || window.webkitAudioContext;
    try {
      if (!context && Audio) context = new Audio();
      if (context?.state === 'suspended') {
        unlocked = context.resume().then(() => {
          if (context?.state === 'running' && $('audio-status')?.textContent === '소리 확인 필요') {
            status('자동 음성 알림 활성', true);
          }
        }).catch(() => {});
      } else if (context?.state === 'running' && $('audio-status')?.textContent === '소리 확인 필요') {
        status('자동 음성 알림 활성', true);
      }
    } catch (_) {}
  }

  window.addEventListener('pointerdown', unlock, {passive: true});
  window.addEventListener('keydown', unlock, {passive: true});

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
      status('소리 확인 필요 · 소리 테스트를 눌러 주세요', false);
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
      status(`${reason} · ${replacement ? '경고음으로 대체' : '확인음'} 재생`, true);
    } catch (_) {
      status('소리 확인 필요 · 음소거 설정 확인', false);
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
        if (!clip.duration || !Number.isFinite(clip.duration) || clip.duration > 10) throw new Error('invalid clip duration');
        return clip;
      })().catch(error => { clipPromise = null; throw error; });
    }
    return clipPromise;
  }

  async function naturalSpeech(purpose, token) {
    const fallback = () => {
      if (token !== generation) return;
      stop();
      chime(generation, '안내형 음성 재생 불가', true);
    };
    speechTimer = setTimeout(fallback, 6000);
    try {
      await unlocked;
      if (token !== generation) return;
      if (!context || context.state !== 'running') {
        clearTimeout(speechTimer);
        status('소리 확인 필요 · 소리 테스트를 클릭해 주세요', false);
        return;
      }
      status(`${purpose} 준비 중`, true);
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
        status(`${purpose} 완료 · 자동 알림 활성`, true);
      };
      clearTimeout(speechTimer);
      finishTimer = setTimeout(() => {
        if (token === generation) { stop(); status('소리 확인 필요 · 소리 테스트로 확인', false); }
      }, 15000);
      source.start();
      status(`${purpose} 재생 중`, true);
    } catch (_) { fallback(); }
  }

  function speak(purpose) {
    stop();
    naturalSpeech(purpose, generation);
  }

  $('audio-test')?.addEventListener('click', () => {
    unlock();
    speak('소리 테스트');
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
