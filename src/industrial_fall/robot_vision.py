"""Compatibility wrapper re-exporting the read-only WebSocket client from robot_ws.

Edge-side AI re-analysis of robot video frames has been retired; inspection
events are produced exclusively by the Windows exploration client (or robot).
"""
from __future__ import annotations

from .robot_ws import (
    ANOMALY_TARGETS,
    DEFECT_TARGETS,
    VALID_INSPECTION_ITEMS,
    SimpleWebSocket,
)

__all__ = [
    "ANOMALY_TARGETS",
    "DEFECT_TARGETS",
    "VALID_INSPECTION_ITEMS",
    "SimpleWebSocket",
]
