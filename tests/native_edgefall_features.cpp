#include "tracker_hailo.h"
#include "temporal_model_weights_hailo.h"
#include <cassert>
#include <cmath>

int main() {
    using namespace rpi_hailo;
    using namespace jetson_fall;
    // Frozen artifact itself proves the feature units; no guessed scaling.
    static_assert(temporal_hailo_weights::kFrameDim == 56);
    static_assert(temporal_hailo_weights::kMean[51] > 100.0f);
    static_assert(temporal_hailo_weights::kMean[51] < 640.0f);
    Tracker tracker;
    TemporalClassifier reference{TemporalProfile::Hailo8YoloV8sPose};
    for (int frame = 0; frame < 75; ++frame) {
        Detection d;
        d.box = {0.5f, 0.5f, 0.3f, 0.8f, 0.9f};
        d.keypoints.assign(17, {0.5f, 0.5f, 0.9f});
        float hip_y = 0.4f + 0.2f * frame / 75.0f;
        d.keypoints[5] = {0.45f, hip_y - 0.2f, 0.9f};
        d.keypoints[6] = {0.55f, hip_y - 0.2f, 0.9f};
        d.keypoints[11] = {0.45f, hip_y, 0.9f};
        d.keypoints[12] = {0.55f, hip_y, 0.9f};
        auto tracks = tracker.update({d}, frame / 15.0);
        assert(tracks.size() == 1);
        auto& t = *tracks[0];
        assert(std::abs(t.observation.hip_y - hip_y) < 1e-6f);
        auto feature = temporalFrameFrom(t);
        assert(std::abs(feature[51] - hip_y * 640.0f) < 1e-3f);
        // End-to-end parity: tracker actually feeds the same pixel-valued
        // feature to the model; not merely an unused diagnostic helper.
        auto obs = t.observation;
        obs.hip_y = hip_y * 640.0f;
        auto expected = reference.update(makeTemporalFrame(&t.pose, obs, 640, 640), frame / 15.0);
        assert(std::abs(expected.probability - t.observation.temporal_probability) < 1e-5f);
        assert(expected.positive == t.observation.temporal_positive);
    }
}
