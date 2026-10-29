#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PIN=ef3bda2fd865fe6b80119fb1c896a63bfc4228ee
SOURCE=
HEF=
OFFLINE=0
ACCEPTED=0
MODEL_URL=https://hailo-model-zoo.s3.eu-west-2.amazonaws.com/ModelZoo/Compiled/v2.15.0/hailo8/yolov8s_pose.hef
MODEL_SHA256=e19856699ed47cf866d23265827f960b263f287dab5e54e82c7ce37e12525a2d
MODEL_OUT="$ROOT/runtime/models/yolov8s_pose_v2_15_h8.hef"

usage() {
    echo "Usage: $0 [--source DIR] [--hef FILE] [--offline] [--accept-upstream-license]" >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --source)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            SOURCE=$2
            shift
            ;;
        --hef)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            HEF=$2
            shift
            ;;
        --offline)
            OFFLINE=1
            ;;
        --accept-upstream-license)
            ACCEPTED=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage
            exit 2
            ;;
    esac
    shift
done

verify_hef() {
    [ -f "$1" ] || return 1
    ACTUAL_MODEL_SHA=$(sha256sum "$1" | awk '{print $1}')
    [ "$ACTUAL_MODEL_SHA" = "$MODEL_SHA256" ]
}

if ! verify_hef "$MODEL_OUT"; then
    [ "$ACCEPTED" -eq 1 ] || {
        echo "Exact YOLOv8s HEF is missing; review upstream terms and pass --accept-upstream-license" >&2
        exit 2
    }
    mkdir -p "$(dirname "$MODEL_OUT")"
    MODEL_TMP="$MODEL_OUT.tmp.$$"
    trap 'rm -f "$MODEL_TMP"' EXIT HUP INT TERM
    if [ -n "$HEF" ]; then
        verify_hef "$HEF" || { echo "HEF checksum mismatch: $HEF" >&2; exit 1; }
        cp "$HEF" "$MODEL_TMP"
    else
        [ "$OFFLINE" -eq 0 ] || {
            echo "Offline setup requires --hef FILE or verified $MODEL_OUT" >&2
            exit 1
        }
        curl --fail --location --retry 3 --connect-timeout 20 \
            --output "$MODEL_TMP" "$MODEL_URL"
    fi
    verify_hef "$MODEL_TMP" || { echo "Downloaded HEF checksum mismatch" >&2; exit 1; }
    chmod 0644 "$MODEL_TMP"
    mv "$MODEL_TMP" "$MODEL_OUT"
    trap - EXIT HUP INT TERM
fi
echo "Verified $MODEL_OUT"

if [ -z "$SOURCE" ]; then
    SOURCE="$ROOT/runtime/vendor/edgefallkit"
    if [ ! -d "$SOURCE/.git" ]; then
        [ "$OFFLINE" -eq 0 ] || { echo "Offline build requires --source DIR" >&2; exit 1; }
        [ "$ACCEPTED" -eq 1 ] || { echo "Review upstream terms and pass --accept-upstream-license" >&2; exit 2; }
        mkdir -p "$(dirname "$SOURCE")"
        git clone https://github.com/suharvest/edgefallkit.git "$SOURCE"
    fi
    [ -z "$(git -C "$SOURCE" status --porcelain)" ] || { echo "Refusing to change modified vendor checkout" >&2; exit 1; }
    if [ "$OFFLINE" -eq 0 ]; then
        git -C "$SOURCE" fetch --depth 1 origin "$PIN"
    fi
    git -C "$SOURCE" checkout --detach "$PIN"
fi

[ -d "$SOURCE/.git" ] || { echo "EdgeFallKit checkout not found: $SOURCE" >&2; exit 1; }
ACTUAL=$(git -C "$SOURCE" rev-parse HEAD)
[ "$ACTUAL" = "$PIN" ] || {
    echo "EdgeFallKit commit mismatch: expected $PIN, got $ACTUAL" >&2
    exit 1
}

OUT="$ROOT/runtime/native/libedgefall_shadow.so"
BUILD="$ROOT/runtime/build/edgefall_shadow"
mkdir -p "$(dirname "$OUT")"
mkdir -p "$BUILD"

# Build from a private, reproducible copy.  The upstream checkout stays clean
# and the validated deployment policy patch is always applied from Git.
cp "$SOURCE/platforms/rpi-hailo/src/tracker_hailo.h" "$BUILD/"
cp "$SOURCE/platforms/rpi-hailo/src/tracker_hailo.cpp" "$BUILD/"
cp "$SOURCE/platforms/jetson/main/pose.h" "$BUILD/"
cp "$SOURCE/platforms/jetson/main/pose.cpp" "$BUILD/"
cp "$SOURCE/platforms/jetson/main/fall_detector.h" "$BUILD/"
cp "$SOURCE/platforms/jetson/main/fall_detector.cpp" "$BUILD/"
cp "$SOURCE/platforms/jetson/main/temporal_classifier.h" "$BUILD/"
cp "$SOURCE/platforms/jetson/main/temporal_classifier.cpp" "$BUILD/"
cp "$SOURCE/platforms/jetson/main/temporal_model_weights.h" "$BUILD/"
cp "$SOURCE/platforms/jetson/main/temporal_model_weights_hailo.h" "$BUILD/"
cp "$SOURCE/platforms/jetson/main/temporal_model_weights_yolo11m.h" "$BUILD/"
cp "$SOURCE/platforms/jetson/main/temporal_model_weights_yolov8s_int8.h" "$BUILD/"
cp "$SOURCE/platforms/jetson/main/temporal_model_weights_yolov8m_int8.h" "$BUILD/"

patch --batch --forward -d "$BUILD" -p1 \
    < "$ROOT/native/edgefall_shadow/strict_confirmation.patch"

# Never truncate a shared library still mapped by the running service.
OUT_TMP=$(mktemp "$OUT.XXXXXX")
trap 'rm -f "$OUT_TMP"' EXIT HUP INT TERM
c++ -std=c++17 -O3 -DNDEBUG -fPIC -shared \
    -I"$BUILD" \
    "$ROOT/native/edgefall_shadow/edgefall_c_api.cpp" \
    "$BUILD/tracker_hailo.cpp" \
    "$BUILD/pose.cpp" \
    "$BUILD/fall_detector.cpp" \
    "$BUILD/temporal_classifier.cpp" \
    -o "$OUT_TMP"
chmod 0755 "$OUT_TMP"
mv "$OUT_TMP" "$OUT"
trap - EXIT HUP INT TERM

echo "Built $OUT"
sha256sum "$OUT"
