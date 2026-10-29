#include "fall_detector.h"
#include <cassert>

using namespace jetson_fall;

// Test policy independently of learned weights. No service/MQTT is involved.
int main() {
    for (bool model_positive : {false, true}) {
        FallDetector detector;
        FallObservation o;
        o.valid = o.temporal_available = true;
        o.person_score = 0.9f;
        o.hip_y = 0.5f; o.torso_angle_deg = 0; o.bbox_aspect_ratio = 0.5f;
        for (int i = 0; i < 20; ++i) {
            o.timestamp_sec = i / 15.0;
            assert(!detector.update(o).fall_event);
        }
        o.timestamp_sec = 20 / 15.0;
        o.hip_y = 0.65f; o.torso_angle_deg = 85; o.bbox_aspect_ratio = 2;
        o.temporal_positive = model_positive;
        auto result = detector.update(o);
        assert(result.diagnostics.geometry_candidate);
        assert(result.fall_event == model_positive);
        assert(result.fall_detected == model_positive);
        for (int i = 21; i < 50; ++i) {
            o.timestamp_sec = i / 15.0;
            result = detector.update(o);
            assert(!result.fall_event); // negative gate or deduplicated alarm
        }
    }
    // Windowed-impact and persistent-horizontal routes cannot bypass a
    // negative model either. Drive model evidence explicitly, independent
    // of how the frozen MLP classifies a synthetic skeleton.
    for (bool positive : {false, true}) {
        for (bool windowed_impact : {false, true}) {
            FallDetector candidate;
            FallObservation c;
            c.valid = c.temporal_available = true;
            c.hip_y = 0.5f; c.torso_angle_deg = 0; c.bbox_aspect_ratio = 0.5f;
            for (int i = 0; i < 20; ++i) {
                c.timestamp_sec = i / 15.0;
                assert(!candidate.update(c).fall_event);
            }
            c.timestamp_sec = 20 / 15.0;
            c.hip_y = windowed_impact ? 0.56f : 0.52f;
            c.torso_angle_deg = 85;
            c.bbox_aspect_ratio = windowed_impact ? 0.8f : 1.1f;
            assert(!candidate.update(c).fall_event);
            c.temporal_positive = positive;
            if (windowed_impact) c.bbox_aspect_ratio = 1.8f;
            int events = 0;
            bool saw_geometry = false;
            for (int i = 21; i < 36; ++i) {
                c.timestamp_sec = i / 15.0;
                auto result = candidate.update(c);
                events += result.fall_event;
                saw_geometry |= result.diagnostics.geometry_candidate;
                if (result.fall_event) assert(c.temporal_positive);
            }
            assert(saw_geometry);
            assert(events == (positive ? 1 : 0));
        }
    }
    // A positive MLP alone cannot originate an alarm from a static pose.
    FallDetector detector;
    FallObservation o;
    o.valid = o.temporal_available = o.temporal_positive = true;
    o.hip_y = 0.6f; o.torso_angle_deg = 85; o.bbox_aspect_ratio = 2;
    for (int i = 0; i < 60; ++i) {
        o.timestamp_sec = i / 15.0;
        assert(!detector.update(o).fall_event);
    }
}
