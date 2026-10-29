# Industrial Fall Management System

Read `docs/ARCHITECTURE.md` and `docs/MIGRATION_2026-10-29.md` before runtime changes.
This repository is standalone Pi5 fall detection, not a smart-home controller.
Do not add Pi4, MQTT, face recognition, TTS, GRU or vehicle control without a new request.
Never modify the collaborator's Pi4 workspace or old shared repository as part of this app.

Preserve Hailo YOLOv8s-Pose v2.15 HEF checksum, EdgeFallKit pinned commit and
units-separated-v6 input contract. Geometry hip-Y is normalized; frozen MLP
hip-Y uses 640-space pixels. Native tests enforce this. No model/threshold changes
as part of branding or UI work. Target 15 unique inference frames/s; latest-frame
queues only, no duplicate-frame time windows. Camera reconnect resets observation.

Runtime code: `src/industrial_fall`. No speculative compatibility layers.
Never commit `.env`, runtime artifacts, event images, face data or raw datasets.
Recent events 50, evidence 50 x 512 KB / 1 hour, diagnostics 20 MB x 3, dedup 256.
Evidence image and keypoints MUST come from the same inference frame.
Never claim model score equals accident probability or POC equals certified safety.

Tests: `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`.
Use read-only/fake evidence for tests; do not ask anyone to perform unsafe falls.
