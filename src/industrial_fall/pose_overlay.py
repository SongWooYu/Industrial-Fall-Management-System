"""COCO pose drawing shared by live debug frames and matched-frame evidence."""
import math
import cv2

POSE_SKELETON = (
    (5, 7), (7, 9), (6, 8), (8, 10), (5, 6),
    (5, 11), (6, 12), (11, 12), (11, 13), (13, 15),
    (12, 14), (14, 16),
)


def draw_pose_shapes(frame, poses, source_shape=None, *, boxes=True):
    """Only render measured points >= .25; do not fill in missing joints."""
    source_h, source_w = (source_shape or frame.shape)[:2]
    sx, sy = frame.shape[1] / source_w, frame.shape[0] / source_h

    def point(x, y):
        return (round(x * sx), round(y * sy))

    drawn_boxes = drawn_joints = 0
    for pose in poses:
        keypoints = pose.get("keypoints", [])
        visible = {i: point(x, y) for i, (x, y, confidence) in enumerate(keypoints)
                   if confidence >= 0.25 and math.isfinite(x) and math.isfinite(y)
                   and 0 <= x < source_w and 0 <= y < source_h}
        for start, end in POSE_SKELETON:
            if start in visible and end in visible:
                cv2.line(frame, visible[start], visible[end], (0, 165, 255), 2)
        for coordinates in visible.values():
            cv2.circle(frame, coordinates, 3, (0, 220, 255), -1)
        drawn_joints += len(visible)
        if boxes:
            box = pose.get("box", [])
            if len(box) != 4 or not all(math.isfinite(v) for v in box):
                continue
            left, top = point(box[0], box[1])
            right, bottom = point(box[2], box[3])
            left, top = max(0, left), max(0, top)
            right, bottom = min(frame.shape[1] - 1, right), min(frame.shape[0] - 1, bottom)
            if right <= left or bottom <= top:
                continue
            cv2.rectangle(frame, (left, top), (right, bottom), (255, 0, 255), 2)
            cv2.putText(frame, f"HAILO PERSON {pose.get('confidence', 0):.2f}",
                        (left, max(20, top - 8)), cv2.FONT_HERSHEY_SIMPLEX,
                        0.55, (255, 0, 255), 2)
            drawn_boxes += 1
    return {"boxes": drawn_boxes, "visible_keypoints": drawn_joints}
