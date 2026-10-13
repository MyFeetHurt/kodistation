#!/bin/sh
# Demo video (backlog 021): lossless KMS capture of the dev Kodi console. Runs ON the Kodi VM (piped over ssh by
# `make demo-probe` / `make demo-capture`); needs sudo for kmsgrab (CAP_SYS_ADMIN). Changes no VM setting.
#
#   capture.sh probe          3 s capture of whatever is on screen; prints frame size, frame count and fps
#   capture.sh start CLIP [force]  background capture into ~/demo-021/CLIP.mkv (pid in ~/demo-021/CLIP.pid);
#                             an existing CLIP.mkv is refused unless "force" (a retake under the same name)
#   capture.sh stop CLIP      SIGINT (ffmpeg finishes the file) and wait; prints frames and duration
#
# The display must be the expected mode (debugfs: 1280x800, XR24, modifier 0x0), else it refuses.
# Fallbacks when kmsgrab cannot map the buffer: DEMO_VF='hwmap=mode=read,format=bgr0', then DEMO_FPS=15.
set -eu
DIR="$HOME/demo-021"
FPS="${DEMO_FPS:-30}"
VF="${DEMO_VF:-hwdownload,format=bgr0}"
DEVICE="${DEMO_DEVICE:-/dev/dri/card0}"

die() { echo "capture: $*" >&2; exit 1; }

check_mode() {
    state=$(sudo cat /sys/kernel/debug/dri/0/state) || die "cannot read the DRM state"
    plane=$(printf '%s\n' "$state" | awk '/^plane\[/{p=0} /allocated by = kodi.bin/{p=1} p' )
    printf '%s\n' "$plane" | grep -q 'format=XR24' || die "Kodi's plane is not XR24"
    printf '%s\n' "$plane" | grep -q 'modifier=0x0$' || die "Kodi's plane is not linear (modifier 0x0)"
    printf '%s\n' "$plane" | grep -q 'size=1280x800$' || die "Kodi's plane is not 1280x800"
}

grab() {  # grab OUT [extra ffmpeg args...]
    out=$1; shift
    sudo ffmpeg -hide_banner -loglevel error -nostdin -y -device "$DEVICE" -f kmsgrab -framerate "$FPS" -i - \
        -vf "$VF" -c:v libx264 -preset ultrafast -qp 0 "$@" "$out"
}

info() {  # info FILE: "WxH frames N duration S fps F"
    ffprobe -v error -count_frames -select_streams v:0 \
        -show_entries stream=width,height,nb_read_frames:format=duration -of default=nw=1 "$1" |
        awk -F= '{v[$1]=$2} END {d=v["duration"]+0; printf "%sx%s frames %s duration %.2f fps %.1f\n",
            v["width"], v["height"], v["nb_read_frames"], d, (d > 0 ? v["nb_read_frames"] / d : 0)}'
}

mkdir -p "$DIR"
chmod 700 "$DIR"
case "${1:-}" in
probe)
    check_mode
    grab "$DIR/probe.mkv" -t 3
    sudo chown "$(id -u):$(id -g)" "$DIR/probe.mkv"
    info "$DIR/probe.mkv"
    rm -f "$DIR/probe.mkv"
    ;;
start)
    clip=${2:?clip name}
    case "$clip" in *[!A-Za-z0-9_-]*) die "bad clip name";; esac
    [ ! -e "$DIR/$clip.pid" ] || die "$clip is already running"
    [ ! -e "$DIR/$clip.mkv" ] || [ "${3:-}" = force ] || die "$clip.mkv exists (retake: force, or a new name)"
    check_mode
    nohup sudo ffmpeg -hide_banner -loglevel error -nostdin -y -device "$DEVICE" -f kmsgrab -framerate "$FPS" \
        -i - -vf "$VF" -c:v libx264 -preset ultrafast -qp 0 -t 600 "$DIR/$clip.mkv" \
        >"$DIR/$clip.ffmpeg.log" 2>&1 &
    echo $! >"$DIR/$clip.pid"
    sleep 1
    kill -0 "$(cat "$DIR/$clip.pid")" 2>/dev/null || { cat "$DIR/$clip.ffmpeg.log" >&2; rm -f "$DIR/$clip.pid"; die "ffmpeg did not start"; }
    echo "capturing $clip"
    ;;
stop)
    clip=${2:?clip name}
    [ -e "$DIR/$clip.pid" ] || die "$clip is not running"
    pid=$(cat "$DIR/$clip.pid")
    # the pid is sudo's; signal its ffmpeg child (sudo relays too, but be explicit)
    sudo pkill -INT -P "$pid" ffmpeg 2>/dev/null || sudo kill -INT "$pid" 2>/dev/null || true
    for _ in $(seq 1 60); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$pid" 2>/dev/null && die "ffmpeg did not stop"
    rm -f "$DIR/$clip.pid"
    sudo chown "$(id -u):$(id -g)" "$DIR/$clip.mkv"
    info "$DIR/$clip.mkv"
    ;;
*)
    die "usage: capture.sh probe | start CLIP | stop CLIP"
    ;;
esac
