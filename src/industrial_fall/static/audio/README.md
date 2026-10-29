# Fixed Korean alert asset

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
