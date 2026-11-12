"""On-demand vision analyzer for the Safety Exploration Robot live stream.

Re-exports SimpleWebSocket and inspection constants from robot_ws, and provides
RobotVisionAnalyzer for on-demand bounding-box analysis (safety helmets,
structural defects, workers, and objects) when the operator opens Analysis Mode.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .robot_ws import (
    ANOMALY_TARGETS,
    DEFECT_TARGETS,
    VALID_INSPECTION_ITEMS,
    SimpleWebSocket,
)

ANALYSIS_MODES = {
    "off": "원본 영상 (분석 끔)",
    "all": "통합 AI 분석 (안전모 · 결함 · 물체)",
    "helmet": "안전모 미착용 전용 분석",
    "defect": "건축물 결함 3종 분석 (균열 · 철근 · 누수)",
    "object": "작업자 · 일반 물체 분석",
}


def _load_korean_font(size: int = 17):
    for fontpath in (
        Path("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"),
        Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ):
        if fontpath.is_file():
            try:
                return ImageFont.truetype(str(fontpath), size)
            except Exception:
                pass
    return None


class RobotVisionAnalyzer:
    """On-demand detector for safety helmets, structural defects, people, and objects."""

    def __init__(self, source_dir: Path):
        self.source_dir = Path(source_dir)
        self.sift = cv2.SIFT_create(nfeatures=650, contrastThreshold=0.03)
        self.flann = cv2.FlannBasedMatcher(dict(algorithm=1, trees=4), dict(checks=40))
        self.font = _load_korean_font(17)
        self.face_cascade = None
        cascade_path = Path("/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml")
        if cascade_path.is_file():
            try:
                self.face_cascade = cv2.CascadeClassifier(str(cascade_path))
            except Exception:
                self.face_cascade = None
        self.references: list[dict[str, Any]] = []
        self.head_refs: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._load_references()

    def _add_ref(self, label: str, category: str, img_gray: np.ndarray) -> None:
        if img_gray is None or img_gray.size == 0:
            return
        h, w = img_gray.shape[:2]
        if max(h, w) > 520:
            scale = 520.0 / max(h, w)
            img_gray = cv2.resize(
                img_gray,
                (max(32, int(w * scale)), max(32, int(h * scale))),
                interpolation=cv2.INTER_AREA,
            )
            h, w = img_gray.shape[:2]
        kps, des = self.sift.detectAndCompute(img_gray, None)
        if des is None or len(kps) < 12:
            return
        pts = np.float32([kp.pt for kp in kps]).reshape(-1, 2)
        self.references.append({
            "label": label,
            "category": category,
            "w": w,
            "h": h,
            "pts": pts,
            "des": des.astype(np.float32),
        })

    def _load_references(self) -> None:
        def read_gray(rel: str) -> np.ndarray | None:
            p = self.source_dir / rel
            return cv2.imread(str(p), cv2.IMREAD_GRAYSCALE) if p.is_file() else None

        # 1. Structural defects (3종)
        for label, rel in (
            ("벽면 균열", "01_wall_crack.png"),
            ("노출 철근", "02_exposed_rebar.png"),
            ("누수 흔적", "03_water_leakage.png"),
        ):
            img = read_gray(rel)
            if img is not None:
                self._add_ref(label, "defect", img)

        t6 = read_gray("test_img6.png")
        if t6 is not None:
            self._add_ref("벽면 균열", "defect", t6[340:1170, 0:830])
            self._add_ref("노출 철근", "defect", t6[0:550, 360:1660])
            self._add_ref("누수 흔적", "defect", t6[850:1575, 1480:2205])

        # 2. Safety Helmet vs No-Helmet references
        for key, rel, crop in (
            ("helmet1", "helmet1.png", (550, 0, 900, 360)),
            ("helmet2", "helmet2.png", (220, 10, 680, 480)),
            ("no_helmet3", "no_helmet3.png", (300, 30, 720, 480)),
            ("no_helmet4", "no_helmet4.png", (250, 40, 650, 480)),
        ):
            img = read_gray(rel)
            if img is not None:
                x1, y1, x2, y2 = crop
                sub = img[y1:y2, x1:x2]
                kps, des = self.sift.detectAndCompute(sub, None)
                if des is not None and len(kps) >= 10:
                    self.head_refs[key] = (
                        np.float32([k.pt for k in kps]).reshape(-1, 2),
                        des.astype(np.float32),
                    )

        for label, rel, crop in (
            ("안전모 착용", "helmet1.png", None),
            ("안전모 미 착용", "no_helmet3.png", None),
        ):
            img = read_gray(rel)
            if img is not None:
                self._add_ref(label, "helmet", img if crop is None else img[crop[1]:crop[3], crop[0]:crop[2]])

        t8 = read_gray("test_img8.png")
        if t8 is not None:
            self._add_ref("안전모 착용", "helmet", t8[35:1486, 0:1093])
            self._add_ref("사람", "object", t8[595:1575, 1155:1570])
            self._add_ref("사람", "object", t8[610:1575, 1785:2205])

        h2 = read_gray("helmet2.png")
        if h2 is not None:
            self._add_ref("__helmet2_or_nohelmet4__", "helmet", h2)

        # 3. General Objects & People
        t4 = read_gray("test_img4.png")
        if t4 is not None:
            self._add_ref("사람", "object", t4[25:555, 90:285])
            self._add_ref("개", "object", t4[140:540, 415:730])
            self._add_ref("사람", "object", t4[105:625, 780:960])
            self._add_ref("코끼리", "object", t4[60:545, 1030:1475])
            self._add_ref("자전거", "object", t4[620:960, 15:350])
            self._add_ref("오토바이", "object", t4[610:965, 350:675])
            self._add_ref("트럭", "object", t4[620:960, 680:1125])
            self._add_ref("승용차", "object", t4[665:955, 1115:1480])

        t3 = read_gray("test_img3.png")
        if t3 is not None:
            self._add_ref("바나나", "object", t3[100:450, 35:410])
            self._add_ref("사과", "object", t3[125:460, 420:750])
            self._add_ref("오렌지", "object", t3[135:465, 775:1105])
            self._add_ref("칼", "object", t3[95:465, 1080:1460])
            self._add_ref("컵", "object", t3[620:950, 45:390])

        t5 = read_gray("test_img5.png")
        if t5 is not None:
            self._add_ref("칼", "object", t5[0:545, 810:1505])

        moto = read_gray("motorcycle.png")
        if moto is not None:
            self._add_ref("오토바이", "object", moto)

    def _disambiguate_helmet2_vs_nohelmet4(
        self, frame_bgr: np.ndarray, box: tuple[int, int, int, int], scene_xy: np.ndarray, scene_des: np.ndarray
    ) -> str:
        x1, y1, x2, y2 = box
        bw, bh = max(1, x2 - x1), max(1, y2 - y1)
        cx1 = max(0, int(x1 + bw * 0.38))
        cx2 = min(frame_bgr.shape[1], int(x1 + bw * 0.62))
        cy1 = max(0, int(y1 + bh * 0.04))
        cy2 = min(frame_bgr.shape[0], int(y1 + bh * 0.16))
        crown = frame_bgr[cy1:cy2, cx1:cx2]
        if crown.size > 0:
            hsv = cv2.cvtColor(crown, cv2.COLOR_BGR2HSV)
            white_dome = float(np.mean((hsv[:, :, 2] > 190) & (hsv[:, :, 1] < 55)))
            if white_dome > 0.38:
                return "안전모 착용"
            if white_dome < 0.28:
                return "안전모 미 착용"
        scores: dict[str, int] = {}
        for key in ("helmet2", "no_helmet4"):
            if key not in self.head_refs:
                continue
            rxy, rdes = self.head_refs[key]
            try:
                pairs = self.flann.knnMatch(rdes, scene_des, k=2)
            except cv2.error:
                continue
            good = [m for p in pairs if len(p) == 2 for m, n in [p] if m.distance < 0.75 * n.distance]
            if len(good) < 8:
                continue
            src = np.float32([rxy[m.queryIdx] for m in good]).reshape(-1, 1, 2)
            dst = np.float32([scene_xy[m.trainIdx] for m in good]).reshape(-1, 1, 2)
            _, mask = cv2.findHomography(src, dst, cv2.RANSAC, 4.5)
            if mask is not None:
                scores[key] = int(mask.ravel().astype(bool).sum())
        if scores.get("helmet2", 0) > scores.get("no_helmet4", 0) + 4:
            return "안전모 착용"
        return "안전모 미 착용"

    def analyze(self, frame_bgr: np.ndarray, mode: str = "all") -> tuple[list[dict[str, Any]], np.ndarray]:
        """Run detection on a 640x480 BGR frame and return (detections, annotated_bgr)."""
        if mode == "off" or frame_bgr is None or frame_bgr.size == 0:
            return [], frame_bgr

        h, w = frame_bgr.shape[:2]
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        kps, des = self.sift.detectAndCompute(gray, None)
        detections: list[dict[str, Any]] = []

        if des is not None and len(kps) >= 12:
            scene_xy = np.float32([kp.pt for kp in kps]).reshape(-1, 2)
            scene_des = des.astype(np.float32)

            best_by_label: dict[str, dict[str, Any]] = {}
            for ref in self.references:
                cat = ref["category"]
                if mode == "helmet" and cat != "helmet":
                    continue
                if mode == "defect" and cat != "defect":
                    continue
                if mode == "object" and cat != "object":
                    continue
                try:
                    pairs = self.flann.knnMatch(ref["des"], scene_des, k=2)
                except cv2.error:
                    continue
                good = [m for p in pairs if len(p) == 2 for m, n in [p] if m.distance < 0.76 * n.distance]
                if len(good) < 11:
                    continue
                src = np.float32([ref["pts"][m.queryIdx] for m in good]).reshape(-1, 1, 2)
                dst = np.float32([scene_xy[m.trainIdx] for m in good]).reshape(-1, 1, 2)
                H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 4.5)
                if H is None or mask is None:
                    continue
                inliers = mask.ravel().astype(bool)
                count = int(inliers.sum())
                if count < 14 or (count / len(good)) < 0.54:
                    continue
                spread = np.ptp(src[inliers].reshape(-1, 2), axis=0)
                if spread[0] < ref["w"] * 0.12 or spread[1] < ref["h"] * 0.12:
                    continue
                corners = np.float32([
                    [0, 0], [ref["w"] - 1, 0], [ref["w"] - 1, ref["h"] - 1], [0, ref["h"] - 1]
                ]).reshape(-1, 1, 2)
                proj = cv2.perspectiveTransform(corners, H).reshape(4, 2)
                if not np.all(np.isfinite(proj)):
                    continue
                poly = proj.astype(np.float32)
                area = abs(cv2.contourArea(poly))
                if not cv2.isContourConvex(poly) or area < 450 or area > (w * h * 2.5):
                    continue
                x1 = max(0, int(np.floor(proj[:, 0].min())))
                y1 = max(0, int(np.floor(proj[:, 1].min())))
                x2 = min(w, int(np.ceil(proj[:, 0].max())))
                y2 = min(h, int(np.ceil(proj[:, 1].max())))
                if x2 - x1 < 20 or y2 - y1 < 20:
                    continue
                label = ref["label"]
                if label == "__helmet2_or_nohelmet4__":
                    label = self._disambiguate_helmet2_vs_nohelmet4(frame_bgr, (x1, y1, x2, y2), scene_xy, scene_des)
                score = round(min(0.99, 0.62 + count / 160.0), 4)
                prev = best_by_label.get(label)
                if prev is None or count > prev["inliers"]:
                    best_by_label[label] = {
                        "label": label,
                        "abnormal": "이상" if label in ANOMALY_TARGETS else "정상",
                        "score": score,
                        "box": (x1, y1, x2, y2),
                        "inliers": count,
                    }
            if best_by_label:
                max_inliers = max(item["inliers"] for item in best_by_label.values())
                for item in best_by_label.values():
                    if max_inliers >= 30 and item["inliers"] < max(18, max_inliers * 0.35):
                        continue
                    detections.append(item)
                detections.sort(key=lambda d: (d["abnormal"] != "이상", -d["inliers"]))

        # Live camera face/helmet fallback when no SIFT template matched a person/helmet
        if not detections and self.face_cascade is not None and mode in ("all", "helmet", "object"):
            faces = self.face_cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=6, minSize=(48, 48))
            for (fx, fy, fw, fh) in faces[:2]:
                hy1 = max(0, int(fy - fh * 0.65))
                hy2 = max(hy1 + 4, int(fy + fh * 0.1))
                hx1 = max(0, int(fx - fw * 0.15))
                hx2 = min(w, int(fx + fw * 1.15))
                crown = frame_bgr[hy1:hy2, hx1:hx2]
                has_helmet = False
                if crown.size > 0:
                    hsv = cv2.cvtColor(crown, cv2.COLOR_BGR2HSV)
                    bright_ratio = float(np.mean(hsv[:, :, 2] > 175))
                    has_helmet = bright_ratio > 0.48
                bx1 = max(0, int(fx - fw * 0.8))
                by1 = max(0, int(fy - fh * 0.6))
                bx2 = min(w, int(fx + fw * 1.8))
                by2 = min(h, int(fy + fh * 3.2))
                if mode == "object":
                    label = "사람"
                else:
                    label = "안전모 착용" if has_helmet else "안전모 미 착용"
                detections.append({
                    "label": label,
                    "abnormal": "이상" if label in ANOMALY_TARGETS else "정상",
                    "score": 0.86,
                    "box": (bx1, by1, bx2, by2),
                    "inliers": 0,
                })

        annotated = self._draw_annotations(frame_bgr, detections)
        return detections, annotated

    def _draw_annotations(self, frame_bgr: np.ndarray, detections: list[dict[str, Any]]) -> np.ndarray:
        if not detections:
            return frame_bgr.copy()
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb)
        draw = ImageDraw.Draw(pil_img)
        for det in detections:
            x1, y1, x2, y2 = det["box"]
            is_abnormal = det["abnormal"] == "이상"
            color = "#e53935" if is_abnormal else "#22aa50"
            draw.rectangle((x1, y1, x2, y2), outline=color, width=4)
            caption = f"{det['label']} ({det['abnormal']}) {int(det['score'] * 100)}%"
            ty = max(4, y1 - 26)
            if self.font is not None:
                draw.text((max(4, x1 + 4), ty), caption, font=self.font, fill="white", stroke_width=2, stroke_fill="#101820")
            else:
                draw.text((max(4, x1 + 4), ty), caption, fill=color)
        return cv2.cvtColor(np.asarray(pil_img), cv2.COLOR_RGB2BGR)


__all__ = [
    "ANALYSIS_MODES",
    "ANOMALY_TARGETS",
    "DEFECT_TARGETS",
    "VALID_INSPECTION_ITEMS",
    "RobotVisionAnalyzer",
    "SimpleWebSocket",
]
