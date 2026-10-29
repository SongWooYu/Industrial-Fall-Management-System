# Fixed Korean alert assets

## Natural announcement

`fall-detected-ko-sunhi-v1.mp3`: “낙상이 감지되었습니다.”

- Reused the project's previously generated `fall-detected-ko-sunhi.wav` (2026-10-22).
- Generator recorded by the original project: edge-tts 7.3.1,
  `ko-KR-SunHiNeural`, rate `-5%`. Synthetic generic voice, no voice cloning.
- Source WAV SHA256: `bd9095ed428cc08387d1140ac8b34e27bf45e29eea5e5eb2cf7d24ff37a6560c`.
- Source WAV: mono, PCM16, 48 kHz, 2.88 seconds.
- Converted with ffmpeg/libmp3lame 64 kbit/s, mono, 24 kHz, without source metadata.
- MP3 SHA256: `5bcca06ac0fe68e45704cc1f1e6dc14c27763533accf4a393a5f5dc6944a3bfd`.
- MP3 size/duration: 23,660 bytes / 2.928 seconds including MP3 padding.
- No personal names, camera audio or credentials. Runtime only serves this static file;
  it does not contact the speech provider or depend on the old project directory.
- This is generated speech, not code licensed under EdgeFallKit's Apache-2.0 license.
  Service terms apply to the provider output. Recheck provider terms for commercial reuse.

Conversion (existing WAV must be supplied; never overwrite an existing asset):

```sh
ffmpeg -nostdin -n -i /path/to/fall-detected-ko-sunhi.wav -map_metadata -1 \
  -ac 1 -ar 24000 -c:a libmp3lame -b:a 64k fall-detected-ko-sunhi-v1.mp3
```

Reference: [edge-tts](https://github.com/rany2/edge-tts),
[Microsoft Korean voice list](https://learn.microsoft.com/azure/ai-services/speech-service/language-support?tabs=text-to-speech).

## Former Pi4 eSpeak NG voice

`fall-detected-ko-espeak-v1.mp3`: “낙상이 감지되었습니다.”

- Generated offline on 2026-10-29 using Debian `espeak-ng 1.52.0+dfsg-5`.
- Reproduces the defaults in the old Pi4 `audio_alarm.py`: Korean `ko`, speed `145`,
  amplitude `170`. This is a reconstruction, not the original Pi4 WAV recording;
  an older engine version or overridden Pi4 environment could sound different.
- MP3: libmp3lame, 64 kbit/s, mono, 24 kHz, 21,548 bytes, 2.664 seconds with padding.
- SHA256: `5e1cc9a4d108a3a4fe75b8ddaa053b550aa5ca23427e3460064d8eebf866d7c9`.
- Contains only the fixed synthesized sentence, no camera audio or voice cloning.
- The engine/packages are not distributed by this repository. Runtime serves only
  the generated file, requiring no Pi4 connection or eSpeak installation.

Reconstruction (use a new work directory; do not overwrite existing assets):

```sh
espeak-ng -v ko -s 145 -a 170 -w fall-espeak.wav '낙상이 감지되었습니다.'
ffmpeg -nostdin -n -i fall-espeak.wav -map_metadata -1 \
  -ac 1 -ar 24000 -c:a libmp3lame -b:a 64k fall-detected-ko-espeak-v1.mp3
```

Reference: [eSpeak NG command options](https://github.com/espeak-ng/espeak-ng/blob/master/src/espeak-ng.1.ronn).
