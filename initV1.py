import subprocess, os, time, signal, glob
import threading
import requests
import RPi.GPIO as GPIO
from datetime import datetime, timedelta
from google.cloud import storage
from shutil import which

PROJECT_ID = "padel-playground-472207"
BUCKET_NAME = "playground_padel"
KEY_FILE = "/home/pi/gcs/creds.json"

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
SEGMENT_WRAP = SEGMENT_COUNT + 2  # 8 -> buffer ekstra

# Remote Button Highlight
GPIO.setmode(GPIO.BCM)
PIN_RECORD = 17
PIN_SUCCESS = 27
PIN_HIGHLIGHT = 22
GPIO.setup(PIN_RECORD, GPIO.OUT)
GPIO.setup(PIN_SUCCESS, GPIO.OUT)
GPIO.setup(PIN_HIGHLIGHT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.output(PIN_RECORD, GPIO.LOW)
GPIO.output(PIN_SUCCESS, GPIO.LOW)

# Laravel
COURT_ID = "1"
API_URL = "https://padel.fgh-prd.com/api/"
API_KEY = "PL4YPADEL!"
POLL_INTERVAL = 5  # detik


def highlight_success():
    GPIO.output(PIN_SUCCESS, GPIO.HIGH)
    time.sleep(1)
    GPIO.output(PIN_SUCCESS, GPIO.LOW)

def monitor_highlight():
    last_state = GPIO.input(PIN_HIGHLIGHT)
    while True:
        state = GPIO.input(PIN_HIGHLIGHT)
        if last_state == GPIO.HIGH and state == GPIO.LOW:
            print("[GPIO] Highlight Button")
            save_highlight() # panggil fungsi highlight
        last_state = state
        time.sleep(0.05)

def set_record_led(on: bool):
    GPIO.output(PIN_RECORD, GPIO.HIGH if on else GPIO.LOW)


def upload_to_gcs(source_file_path, destination_blob_name=None):
    if destination_blob_name is None:
        destination_blob_name = os.path.basename(source_file_path)
    try:
        # buat client pakai service account json
        client = storage.Client.from_service_account_json(KEY_FILE)
        bucket = client.bucket(BUCKET_NAME)
        blob = bucket.blob(destination_blob_name)

        # upload dari file
        blob.upload_from_filename(source_file_path)
        print(f'[GCS] Uploaded {source_file_path} -> gs://{BUCKET_NAME}/{destination_blob_name}')

        url = blob.generate_signed_url(expiration=timedelta(minutes=60*24))
        send_video(url)
        return True
    except Exception as e:
        print(f'[GCS] Upload error: {e}')
        return False

def upload_in_background(source_file_path, destination_blob_name=None):
    t = threading.Thread(target=upload_to_gcs, args=(source_file_path, destination_blob_name))
    t.daemon = True
    t.start()

def start_recording(session_id):
    global rproc, fproc, SESSION_ID, HIGHLIGHT_DIR, HIGHLIGHT_COUNT
    if rproc is not None or fproc is not None:
        print("Recording already running")
        return

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
            
    rpicam_cmd = [
        "rpicam-vid",
        "-t", "0", "--nopreview", "--verbose", "0",
        "--width", str(WIDTH), "--height", str(HEIGHT), "--framerate", str(FPS),
        "--codec", "h264", "--inline", "--intra", str(INTRA),
        "-o", "-" 
    ]

    ffmpeg_cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts",
        "-use_wallclock_as_timestamps", "1",
        "-i", "-",
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
    print(f"Recording started with ID={SESSION_ID}, Highlight dir: {HIGHLIGHT_DIR}")
    set_record_led(True)

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
    set_record_led(False)

def save_highlight():
    global HIGHLIGHT_DIR, HIGHLIGHT_COUNT, SESSION_ID

    if SESSION_ID is None:
        print("Tidak ada sesi aktif!.")
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

    candidates = segs[1: SEGMENT_COUNT + 2]  # SEGMENT_COUNT + 2 ekstra

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

    chosen = valid[:SEGMENT_COUNT]
    chosen.sort(key=os.path.getmtime)

    if HIGHLIGHT_DIR is None:
        HIGHLIGHT_DIR = os.path.join(HIGHLIGHT_ROOT, SESSION_ID)
    os.makedirs(HIGHLIGHT_DIR, exist_ok=True)
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
        "-c", "copy", "-y",
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
        HIGHLIGHT_COUNT -= 1
        return None

    print("Highlight saved:", out_file)
    upload_in_background(out_file)
    highlight_success()
    return out_file

def send_video(signed_url):
    full_url = API_URL + "store-video"
    print(f"Sending POST to: {full_url}")

    response = requests.post(
        full_url,
        headers={"X-API-KEY": API_KEY},
        json={
            "activity_id": SESSION_ID,
            "url": signed_url
        }
    )
    print(f"[BACKEND] URL video dikirim: {response.status_code}")

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

def poll_server():
    while True:
        try:
            response = requests.get(
                API_URL + "check-activity?court_id=" + COURT_ID,
                headers={"X-API-KEY": API_KEY}
            )

            if response.status_code == 200:
                data = response.json()
                session_data = data.get("data", {})
                SESSION_ID = session_data.get("session_id")
                IS_STOPPED = session_data.get("is_stopped")

                if SESSION_ID:
                    start_recording(SESSION_ID)
                elif IS_STOPPED:
                    stop_recording()
                else:
                    print("[INFO] Standby...")
            else:
                print(f"[WARN] Respon server tidak OK: {response.status_code}")

        except Exception as e:
            print(f"[ERROR] Gagal polling: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    threading.Thread(target=monitor_highlight, daemon=True).start()
    poll_server()
    #main_loop()
