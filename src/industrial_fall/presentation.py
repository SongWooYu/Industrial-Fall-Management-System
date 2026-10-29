"""Display readiness separately from the detector's alarm state."""
from __future__ import annotations

from typing import Any


def analysis_readiness(status: dict[str, Any]) -> dict[str, Any]:
    tracks = [t for t in status.get("edgefall_tracks", []) if t.get("visible")]
    selected = next(
        (t for t in tracks if t["track_id"] == status.get("edgefall_best_track_id")),
        None,
    )
    frames = int(selected.get("age_frames", 0)) if selected else 0
    score = None
    if not status.get("pose_enabled"):
        code, label, detail = "disabled", "분석 비활성", "낙상 분석이 중지되어 있습니다."
    elif not status.get("online"):
        code, label, detail = "offline", "카메라 연결 끊김", "새 영상을 받지 못하고 있습니다."
    elif status.get("error") or status.get("edgefall_error"):
        code, label, detail = "error", "분석 오류", str(status.get("edgefall_error") or status.get("error"))
    elif not status.get("edgefall_enabled"):
        code, label, detail = "disabled", "분석 비활성", "EdgeFall 실행 상태를 확인해 주세요."
    elif not tracks:
        if status.get("person_count", 0):
            code, label, detail = "pose_needed", "관절 확인 필요", "사람 후보는 보이지만 분석 가능한 추적이 없습니다. 머리부터 발끝까지 보여 주세요."
        else:
            code, label, detail = "waiting", "사람을 기다리는 중", "카메라는 정상입니다. 사람이 들어오면 움직임 분석을 시작합니다."
    elif selected is None or not selected.get("valid"):
        code, label, detail = "pose_needed", "관절 확인 필요", "어깨·골반 관절이 가려져 현재 자세를 판단하기 어렵습니다. 전신이 보이도록 이동해 주세요."
    else:
        score = float(selected.get("temporal_probability", 0.0))
        if frames < 48:
            code, label, detail = "collecting", "초기 움직임 관측", "약 3.1초의 움직임을 분석합니다. 초기 점수는 관측이 쌓이며 바뀔 수 있습니다."
        else:
            code, label, detail = "analyzing", "움직임 분석 중", "움직임 점수와 하강·자세 변화를 함께 확인합니다. 점수만으로 경보를 확정하지 않습니다."
    reason = selected.get("decision_reason", "normal") if selected else "no_track"
    reasons = {
        "temporal_and_geometry": "모델·동작 조건 모두 충족",
        "alarm_latched": "확정된 경보 유지 중",
        "model_not_confirmed": "동작 후보 · 모델 미동의",
        "motion_candidate": "움직임 의심 · 확정 전",
        "motion_not_confirmed": "모델 양성 · 동작 조건 미충족",
        "insufficient_pose": "관절 부족 · 판정 보류",
        "pose_reacquired": "관절 다시 연결 · 속도 판정 보류",
        "normal": "낙상 확정 조건 미충족",
        "no_track": "판정 대상 없음",
    }
    return {"code": code, "label": label, "detail": detail,
            "motion_score": score, "observed_frames": frames,
            "decision_reason": reason, "decision_label": reasons.get(reason, reason),
            "temporal_positive": bool(selected and selected.get("temporal_positive")),
            "geometry_candidate": bool(selected and selected.get("geometry_candidate")),
            "temporal_threshold": 0.75}
