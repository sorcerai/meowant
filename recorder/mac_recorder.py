"""Mac-side NVR recorder for the cat cameras -> NAS, with a hard size cap.

Runs on the Mac Studio (which already reads the RTSP streams AND has the
Synology NAS mounted at /Volumes/Files), so it needs zero changes to the
Proxmox bridge or the NAS. One ffmpeg per camera does a plain stream COPY
(no re-encode -> negligible CPU) into timestamped segments on the NAS. A
pruner enforces a HARD size cap by deleting the oldest segments first.

Critical safety: if the NAS mount drops, macOS would let writes land on the
Mac's LOCAL disk under the empty mountpoint and silently fill it (the exact
failure that took the bridge cameras down). So recording ONLY runs while the
mount is verified live; if it drops, ffmpeg is stopped until it returns.

Config via env (all optional):
  REC_ROOT        default /Volumes/Files/meowant_recordings
  REC_MOUNT       default /Volumes/Files   (must be a live mountpoint to record)
  REC_CAMS        default meowcam1..6       (comma-separated)
  REC_RTSP_BASE   default rtsp://192.168.2.79:8554
  REC_SEGMENT_S   default 600               (10-min segments)
  REC_CAP_GB      default 1000              (hard ceiling across all cameras)
  REC_PRUNE_S     default 300               (size check cadence)
"""
import os
import subprocess
import sys
import time

REC_ROOT = os.environ.get("REC_ROOT", "/Volumes/Files/meowant_recordings")
REC_MOUNT = os.environ.get("REC_MOUNT", "/Volumes/Files")
REC_CAMS = os.environ.get("REC_CAMS", "meowcam1,meowcam2,meowcam3,meowcam4,meowcam5,meowcam6").split(",")
RTSP_BASE = os.environ.get("REC_RTSP_BASE", "rtsp://192.168.2.79:8554").rstrip("/")
SEGMENT_S = int(os.environ.get("REC_SEGMENT_S", "600"))
CAP_BYTES = int(float(os.environ.get("REC_CAP_GB", "1000")) * 1024**3)
PRUNE_S = int(os.environ.get("REC_PRUNE_S", "300"))


def mount_ok(path):
    """True only if `path` is a live mountpoint — never write recordings to a
    plain directory (that means the NAS is unmounted and writes hit local disk)."""
    try:
        return os.path.ismount(path)
    except OSError:
        return False


def _segments(root):
    """All recording segment files (*.mp4) under root, as (path, size, mtime)."""
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".mp4"):
                continue
            p = os.path.join(dirpath, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append((p, st.st_size, st.st_mtime))
    return out


def prune_to_cap(root, cap_bytes, now_fn=time.time, active_grace_s=30):
    """Delete oldest segments (globally, across cameras) until total <= cap_bytes.
    A segment whose mtime is within active_grace_s is being written now and is
    spared. Returns (files_deleted, bytes_freed). Never raises."""
    try:
        segs = _segments(root)
    except OSError:
        return (0, 0)
    total = sum(s for _p, s, _m in segs)
    if total <= cap_bytes:
        return (0, 0)
    now = now_fn()
    # oldest first; skip the actively-written tail segments
    prunable = sorted((s for s in segs if now - s[2] >= active_grace_s),
                      key=lambda s: s[2])
    deleted = freed = 0
    for p, size, _m in prunable:
        if total <= cap_bytes:
            break
        try:
            os.remove(p)
            total -= size
            freed += size
            deleted += 1
        except OSError as e:
            print(f"[recorder] prune remove failed {p}: {e}", file=sys.stderr)
    return (deleted, freed)


def _ffmpeg_cmd(cam):
    """Per-camera segmented recording, stream-copy, timestamped filenames.
    Video only (-an): the cams' pcm_mulaw audio muxes poorly into mp4 and adds
    nothing for a litterbox NVR. -reset_timestamps keeps each segment seekable."""
    out_dir = os.path.join(REC_ROOT, cam)
    os.makedirs(out_dir, exist_ok=True)
    pattern = os.path.join(out_dir, f"{cam}_%Y-%m-%d_%H-%M-%S.mp4")
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp", "-timeout", "10000000",
        "-i", f"{RTSP_BASE}/{cam}",
        "-an", "-c:v", "copy",
        "-f", "segment", "-segment_time", str(SEGMENT_S),
        "-segment_format", "mp4", "-reset_timestamps", "1", "-strftime", "1",
        pattern,
    ]


def run():
    procs = {}   # cam -> Popen
    last_prune = 0.0

    def stop_all():
        for cam, p in list(procs.items()):
            if p.poll() is None:
                p.terminate()
            procs.pop(cam, None)

    print(f"[recorder] cams={REC_CAMS} root={REC_ROOT} cap={CAP_BYTES//1024**3}GB "
          f"seg={SEGMENT_S}s", file=sys.stderr)
    while True:
        live = mount_ok(REC_MOUNT)
        if not live:
            if procs:
                print("[recorder] NAS mount gone -> pausing recording (won't write local)",
                      file=sys.stderr)
                stop_all()
            time.sleep(15)
            continue
        for cam in REC_CAMS:
            p = procs.get(cam)
            if p is None or p.poll() is not None:
                if p is not None:
                    print(f"[recorder] {cam} ffmpeg exited ({p.returncode}); restarting",
                          file=sys.stderr)
                try:
                    procs[cam] = subprocess.Popen(_ffmpeg_cmd(cam))
                except Exception as e:
                    print(f"[recorder] {cam} spawn failed: {e}", file=sys.stderr)
        now = time.time()
        if now - last_prune >= PRUNE_S:
            d, freed = prune_to_cap(REC_ROOT, CAP_BYTES)
            if d:
                print(f"[recorder] pruned {d} segments, freed {freed//1024**2}MB "
                      f"(cap {CAP_BYTES//1024**3}GB)", file=sys.stderr)
            last_prune = now
        time.sleep(10)


if __name__ == "__main__":
    run()
