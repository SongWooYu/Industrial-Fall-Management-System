// Metadata-only bridge for EdgeFallKit's Hailo-specific temporal classifier.
//
// This file is original Home-Safety-System glue. It is linked at setup time
// with the pinned Apache-2.0 EdgeFallKit sources; no video decode or MQTT code
// is included in the shared library.

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <exception>
#include <string>
#include <vector>

#include "tracker_hailo.h"

namespace {

struct EdgeFallDetectionC {
    float box[5];       // centre x/y, width, height, confidence; normalized
    float keypoints[51]; // COCO-17 x/y/confidence; normalized
};

struct EdgeFallTrackC {
    std::uint64_t track_id;
    std::uint64_t event_id;
    std::int32_t state;
    std::int32_t age;
    std::int32_t missed;
    std::int32_t valid;
    std::int32_t fall_detected;
    std::int32_t fall_event;
    std::int32_t temporal_positive;
    float temporal_probability;
    float box[5];
    float hip_y;
    float torso_angle_deg;
    float bbox_aspect_ratio;
    float hip_drop_speed;
    float hip_drop_distance;
    std::int32_t geometry_candidate;
    std::int32_t motion_continuous;
};

struct Handle {
    explicit Handle(float keypoint_threshold) : tracker(keypoint_threshold) {}
    rpi_hailo::Tracker tracker;
    std::string error;
};

}  // namespace

extern "C" {

int edgefall_shadow_abi_version() noexcept { return 2; }

void* edgefall_shadow_create(float keypoint_threshold) noexcept {
    try {
        return new Handle(keypoint_threshold);
    } catch (...) {
        return nullptr;
    }
}

void edgefall_shadow_destroy(void* opaque) noexcept {
    delete static_cast<Handle*>(opaque);
}

const char* edgefall_shadow_profile() noexcept {
    return "edgefallkit-hailo8-yolov8s-pose-v1+units-separated-v6";
}

const char* edgefall_shadow_last_error(void* opaque) noexcept {
    const auto* handle = static_cast<Handle*>(opaque);
    return handle == nullptr ? "invalid EdgeFall handle" : handle->error.c_str();
}

std::size_t edgefall_shadow_update(
    void* opaque,
    const EdgeFallDetectionC* detections,
    std::size_t detection_count,
    double timestamp_sec,
    EdgeFallTrackC* output,
    std::size_t output_capacity) noexcept {
    auto* handle = static_cast<Handle*>(opaque);
    if (handle == nullptr) return 0;

    try {
        handle->error.clear();
        std::vector<rpi_hailo::Detection> converted;
        converted.reserve(detection_count);
        for (std::size_t i = 0; i < detection_count; ++i) {
            rpi_hailo::Detection detection;
            detection.box = {
                detections[i].box[0], detections[i].box[1],
                detections[i].box[2], detections[i].box[3],
                detections[i].box[4],
            };
            detection.keypoints.reserve(17);
            for (std::size_t joint = 0; joint < 17; ++joint) {
                const std::size_t offset = joint * 3;
                detection.keypoints.push_back({
                    detections[i].keypoints[offset],
                    detections[i].keypoints[offset + 1],
                    detections[i].keypoints[offset + 2],
                });
            }
            converted.push_back(std::move(detection));
        }

        handle->tracker.update(converted, timestamp_sec);
        const auto& tracks = handle->tracker.tracks();
        const std::size_t count = std::min(output_capacity, tracks.size());
        for (std::size_t i = 0; i < count; ++i) {
            const auto& track = *tracks[i];
            auto& item = output[i];
            item.track_id = track.id;
            item.event_id = track.output.event_id;
            item.state = static_cast<std::int32_t>(track.output.state);
            item.age = track.age;
            item.missed = track.missed;
            item.valid = track.observation.valid ? 1 : 0;
            item.fall_detected = track.output.fall_detected ? 1 : 0;
            item.fall_event = track.output.fall_event ? 1 : 0;
            item.temporal_positive = track.observation.temporal_positive ? 1 : 0;
            item.temporal_probability = track.observation.temporal_probability;
            item.box[0] = track.box.x;
            item.box[1] = track.box.y;
            item.box[2] = track.box.w;
            item.box[3] = track.box.h;
            item.box[4] = track.box.score;
            item.hip_y = track.observation.hip_y;
            item.torso_angle_deg = track.observation.torso_angle_deg;
            item.bbox_aspect_ratio = track.observation.bbox_aspect_ratio;
            item.hip_drop_speed = track.output.diagnostics.hip_drop_speed;
            item.hip_drop_distance = track.output.diagnostics.hip_drop_distance;
            item.geometry_candidate = track.output.diagnostics.geometry_candidate ? 1 : 0;
            item.motion_continuous = track.observation.motion_continuous ? 1 : 0;
        }
        return count;
    } catch (const std::exception& exc) {
        handle->error = exc.what();
    } catch (...) {
        handle->error = "unknown EdgeFall update failure";
    }
    return 0;
}

}  // extern "C"
