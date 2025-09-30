import subprocess, os, time, signal, glob
from shutil import which

# Paths
BUFFER_DIR = "/home/pi/rolling_buffer"
HIGHLIGHT_ROOT = "/home/pi/highlight"
os.makedirs(BUFFER_DIR, exist_ok=True)
os.makedirs(HIGHLIGHT_ROOT, exist_ok=True)

# Globals
rproc = None   # rpicam-vid process
fproc = None   # ffmpeg segment writer process
SESSION_ID = None   # id manual dari user (start <id>)
HIGHLIGHT_DIR = None
HIGHLIGHT_COUNT = 0  # urutan highlight per sesi

# Settings
WIDTH = 1536
HEIGHT = 864
FPS = 50
INTRA = 50

SEGMENT_LEN = 5         # 5 detik per segmen
SEGMENT_COUNT = 6       # target 30 detik highlight
SEGMENT_WRAP = SEGMENT_COUNT + 2  # total 7 file -> ada buffer ekstra

def check_binaries():
    for b in ("rpicam-vid", "ffmpeg"):
        if which(b) is None:
            raise SystemExit(f"Binary not found: {b} - install it first")

def start_recording(session_id):
    global rproc, fproc, SESSION_ID, HIGHLIGHT_DIR, HIGHLIGHT_COUNT
    if rproc is not None or fproc is not None:
        print("Recording already running")
        return

    check_binaries()

    # simpan session id
    SESSION_ID = session_id
    HIGHLIGHT_DIR = os.path.join(HIGHLIGHT_ROOT, SESSION_ID)
    os.makedirs(HIGHLIGHT_DIR, exist_ok=True)
    HIGHLIGHT_COUNT = 0  # reset counter setiap sesi baru

    # Bersihkan buffer lama
    for f in glob.glob(os.path.join(BUFFER_DIR, "seg_*.mp4")):
        try:
            os.remove(f)
        except:
            pass

    # rpicam command: output raw H264 ke stdout
    rpicam_cmd = [
        "rpicam-vid",
        "-t", "0", "--nopreview",
        "--width", str(WIDTH), "--height", str(HEIGHT), "--framerate", str(FPS),
        "--codec", "h264", "--inline", "--intra", str(INTRA),
        "-o", "-"   # output ke stdout
    ]

    # ffmpeg: tulis ke segmen rolling
    ffmpeg_cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts",
        "-use_wallclock_as_timestamps", "1",
        "-i", "-",                 # input dari rpicam stdout
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(SEGMENT_LEN),
        "-reset_timestamps", "1",
        "-segment_wrap", str(SEGMENT_WRAP),
        os.path.join(BUFFER_DIR, "seg_%03d.mp4")
    ]

    # Start processes
    rproc = subprocess.Popen(rpicam_cmd, stdout=subprocess.PIPE, preexec_fn=os.setsid)
    fproc = subprocess.Popen(ffmpeg_cmd, stdin=rproc.stdout,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             preexec_fn=os.setsid)
    rproc.stdout.close()

    print(f"Recording started with ID={SESSION_ID} -> buffer dir: {BUFFER_DIR}, highlight dir: {HIGHLIGHT_DIR}")

def stop_recording():
    global rproc, fproc, SESSION_ID
    if rproc is None and fproc is None:
        print("Recording not running")
        return

    print("Stopping recording...")
    try:
        if fproc:
            os.killpg(os.getpgid(fproc.pid), signal.SIGTERM)
            fproc.wait(timeout=5)
    except Exception:
        pass
    try:
        if rproc:
            os.killpg(os.getpgid(rproc.pid), signal.SIGTERM)
            rproc.wait(timeout=5)
    except Exception:
        pass

    rproc = None
    fproc = None
    print(f"Recording stopped (ID={SESSION_ID})")
    SESSION_ID = None

def save_highlight():
    """
    Ambil SEGMENT_COUNT segmen terakhir yang valid (skip segmen terbaru yang masih ditulis).
    Validasi segmen berdasarkan ukuran (>1 MB) dan modifikasi >1 detik lalu.
    Merge ke mp4 playable dengan concat (copy).
    """
    global HIGHLIGHT_DIR, HIGHLIGHT_COUNT, SESSION_ID

    if SESSION_ID is None:
        print("Tidak ada sesi aktif! Jalankan 'start <id>' dulu.")
        return None

    now = time.time()

    # list segmen, urut berdasarkan waktu modifikasi (terbaru dulu)
    segs = sorted(
        glob.glob(os.path.join(BUFFER_DIR, "seg_*.mp4")),
        key=os.path.getmtime,
        reverse=True
    )

    if len(segs) < SEGMENT_COUNT + 1:
        print("Buffer belum cukup segmen untuk 30 detik highlight")
        return None

    # ambil kandidat: skip segmen paling terbaru (saat ini kemungkinan sedang ditulis)
    # ambil ekstra sebagai cadangan supaya bisa melewati segmen corrupt/incomplete
    candidates = segs[1: SEGMENT_COUNT + 2]  # SEGMENT_COUNT + 2 ekstra, ambil lebih aman

    valid = []
    for s in candidates:
        try:
            st = os.stat(s)
        except FileNotFoundError:
            continue

        # valid jika ukuran > 1.5 MB dan file tidak diubah dalam 1 detik terakhir
        if st.st_size > 1_500_000 and (now - st.st_mtime) > 1.0:
            valid.append(s)

        if len(valid) >= SEGMENT_COUNT:
            break

    if len(valid) < SEGMENT_COUNT:
        print(f"Highlight gagal: hanya {len(valid)} segmen valid, butuh {SEGMENT_COUNT}")
        return None

    # ambil tepat SEGMENT_COUNT segmen terbaru dari valid, urutkan ascending berdasarkan waktu
    chosen = valid[:SEGMENT_COUNT]
    chosen.sort(key=os.path.getmtime)

    # pastikan folder highlight ada
    if HIGHLIGHT_DIR is None:
        HIGHLIGHT_DIR = os.path.join(HIGHLIGHT_ROOT, SESSION_ID)
    os.makedirs(HIGHLIGHT_DIR, exist_ok=True)

    # increment counter dan buat nama file
    HIGHLIGHT_COUNT += 1
    out_file = os.path.join(HIGHLIGHT_DIR, f"highlight_{SESSION_ID}_{HIGHLIGHT_COUNT}.mp4")
    concat_list = os.path.join(BUFFER_DIR, f"concat_{SESSION_ID}_{HIGHLIGHT_COUNT}.txt")

    # buat file concat
    with open(concat_list, "w") as f:
        for s in chosen:
            f.write(f"file '{os.path.abspath(s)}'\n")

    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0",
        "-i", concat_list,
        "-c", "copy",
        out_file
    ]

    print(f"Creating highlight {HIGHLIGHT_COUNT} for ID={SESSION_ID} (last {SEGMENT_COUNT * SEGMENT_LEN}s)...")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    try:
        os.remove(concat_list)
    except Exception:
        pass

    if res.returncode != 0:
        print("Highlight failed:", res.stderr.decode(errors="ignore")[:1000])
        # turunkan counter karena gagal
        HIGHLIGHT_COUNT -= 1
        return None

    print("Highlight saved:", out_file)
    return out_file


def main_loop():
    print("Commands: start <id> / stop / highlight / exit")
    try:
        while True:
            cmd = input("> ").strip().split()
            if not cmd:
                continue
            if cmd[0] == "start":
                if len(cmd) < 2:
                    print("Usage: start <id>")
                else:
                    start_recording(cmd[1])
            elif cmd[0] == "stop":
                stop_recording()
            elif cmd[0] == "highlight":
                save_highlight()
            elif cmd[0] == "exit":
                stop_recording()
                break
            else:
                print("Unknown command")
    except KeyboardInterrupt:
        stop_recording()
        print("\nExiting")

if __name__ == "__main__":
    main_loop()
