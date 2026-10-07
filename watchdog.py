"""Multi-camera prey watchdog (07.09, Sid).

Kittyhack itself only ever analyses ONE active camera source at a time
(CAMERA_SOURCE = internal | ip_camera) - there's no built-in way to fuse
several cameras into one prey decision. This script is a small, separate
process that does exactly that: it watches N independent RTSP cameras in
parallel (the ones covering the terrace/catio approach, not just the flap
itself), runs the same TFLite model Kittyhack uses, and - the instant ANY
of them sees a mouse/bird with high enough confidence - calls Kittyhack's
own REST API to lock the real (house-facing) flap in both directions for
LOCK_DURATION_AFTER_PREY_DETECTION seconds. Kittyhack keeps handling normal
entry/exit on its own primary camera; this only ever adds an extra,
more-cautious trigger to lock down early.

Run as a second process alongside app.py (see compose.yaml).
"""

import json
import logging
import os
import smtplib
import threading
import time
from email.mime.text import MIMEText

import cv2
import numpy as np
import paho.mqtt.client as mqtt
import requests
from tflite_runtime.interpreter import Interpreter

from src.api import create_token
from src.baseconfig import CONFIG, load_config
from src.mqtt import MQTTConfig
from src.paths import kittyhack_root
from src.model.yolo_model import YoloModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [WATCHDOG] %(message)s")
log = logging.getLogger("watchdog")

TOKEN_FILE = os.path.join(kittyhack_root(), "watchdog_token.json")
API_BASE = "http://127.0.0.1:80/api/v1"
RECONNECT_DELAY_S = 5.0

# 04.10, Sid: the watchdog + kittyhack-remote's own continuous chatiere
# analysis together were pegging the whole NAS (confirmed via docker stats:
# ~376% combined CPU on a 4-core box, same on both FUNMEDIA and OldMedia -
# moving host never was going to fix this, the two containers alone need
# nearly the whole machine). Prey pre-detection on the 3 peripheral cameras
# doesn't need sub-2s latency to still be useful, so they're cut way down.
# Chatiere stays closer to the original 1.5s cadence (a cat reaches the flap
# fast from right there) but gets a temporary speed-up on real PIR motion
# instead of running hot all the time - see CHATIERE_BOOST_* and the MQTT
# subscription in MqttStatus below.
CAMERA_POLL_INTERVAL_S = {
    "chatiere": 2.0,
    "terrasse": 10.0,
    "jardin_japonais": 10.0,
    "entree": 10.0,
}
DEFAULT_POLL_INTERVAL_S = 10.0
CHATIERE_BOOST_INTERVAL_S = 1.5
CHATIERE_BOOST_DURATION_S = 20.0
# Written by MqttStatus's on_message handler, read by chatiere's
# CameraWatcher thread - a single float assignment/read is atomic enough in
# CPython for this (no lock needed, worst case is one stale poll decision).
_chatiere_boost_until = [0.0]

# 09.09, Sid: on a flaky RTSP link (terrasse/entree, weak WiFi -> packet
# loss), ffmpeg's h264 decoder does error concealment on a corrupted frame
# instead of failing outright - cap.read() still returns ok=True with a
# real ndarray, just a near-flat gray field (measured std ~2 on a genuinely
# corrupted frame vs ~55-70 on any real night-vision frame, even an empty
# scene - there's always IR grain/texture). Those were slipping past the
# "any_detection" bar as spurious low-confidence noise and getting saved as
# training samples - a whole day of "Terrasse" cards that were just gray
# boxes with no way to tell why. Caught here before detection/save runs.
BLANK_FRAME_STD_THRESHOLD = 10.0

# Extra cameras only - the flap's own primary camera is already covered by
# Kittyhack itself. Fill in real RTSP URLs before running.
# 10.09, Sid: terrasse/entree switched to the Reolink "_01_sub" substream
# (640x360 / 1536x576 H264) instead of "_01_main" (3840x2160 HEVC / 4608x1728
# HEVC). Root cause of their constant reconnects wasn't WiFi signal (terrasse
# measured -41dB, a strong signal) - it was streaming full 4K/8MP HEVC over
# WiFi at all: 2308/2342 "Lost frame" reconnects each in 48h, vs. 0 for
# chatiere and jardin_japonais, which are both plain H264 at a more modest
# resolution and never drop. HEVC is also less tolerant of a lost packet
# (corrupts a longer span before recovering) - matches the near-flat-gray
# frames from error concealment we were seeing on these two specifically.
CAMERAS = {
    "chatiere": "rtsp://admin:Dorothee79%25@192.168.178.21:554/h264Preview_01_main",
    "terrasse": "rtsp://admin:Dorothee79%25@192.168.178.44:554/h264Preview_01_sub",
    "jardin_japonais": "rtsp://admin:Dorothee79@192.168.178.138:554/h264Preview_01_main",
    "entree": "rtsp://admin:Dorothee79%25@192.168.178.108:554/h264Preview_01_sub",
}

# Deliberately more cautious than Kittyhack's own default (70): a false
# lockdown just costs a cat a few extra minutes outside, a missed one costs
# a live mouse loose in the house.
WATCHDOG_MOUSE_THRESHOLD = 50.0

MODEL_PATH = os.path.join(kittyhack_root(), "tflite", "original_kittyflap_model_v2", "cv-lite-model.tflite")
LABELS_PATH = os.path.join(kittyhack_root(), "tflite", "original_kittyflap_model_v2", "labels.txt")

# Latest-frame snapshots for the "Presence" page's camera preview in the main
# Kittyhack UI. Written here (shared /data volume) rather than served
# directly from this process, since this script has no HTTP server of its
# own - kittyhack-remote's own web server reads these same files.
FRAME_SNAPSHOT_DIR = "/data/watchdog_frames"

# Full-resolution samples (separate from the live preview above) - a growing
# archive of real terrace/catio conditions for Sid to pick from when
# labeling in Label Studio, so training data isn't limited to whatever she
# can stage on the kitchen table. She can prune the folder herself whenever,
# nothing here is read by Kittyhack automatically.
#
# 09.09, Sid: originally a blind timer (one frame every 10 minutes per
# camera) - most of those were just empty terrace/catio, wasting storage on
# frames with nothing to label. Now gated on the *same* model's own
# detection instead (see MouseDetector.run()'s any_detection return value):
# only save when it actually saw something. Deliberately a much lower
# confidence bar than WATCHDOG_MOUSE_THRESHOLD (which gates a real physical
# lockdown, so it stays conservative) - a borderline 20-30% guess is exactly
# the kind of uncertain case worth capturing and correcting through
# labeling, more useful for training than another confidently-empty frame.
# TRAINING_SAMPLE_COOLDOWN_S still caps the rate per camera so a cat sitting
# still in frame for minutes doesn't fill the folder with near-duplicates.
TRAINING_SAMPLES_DIR = "/data/watchdog_training_samples"
TRAINING_SAMPLE_MIN_CONFIDENCE = 20.0
TRAINING_SAMPLE_COOLDOWN_S = 30.0

SECRETS_FILE = os.path.join(kittyhack_root(), "watchdog_secrets.json")
MQTT_TOPIC_PREFIX = "kittyhack/watchdog"

# NEVER put real credentials here - this file is tracked by git. Real values
# live only in SECRETS_FILE (gitignored, created directly outside of source
# control). This is just the shape used if that file is somehow missing.
_PLACEHOLDER_SECRETS = {
    "mqtt": {"host": "", "port": 1883, "username": "", "password": ""},
    "smtp": {"host": "", "port": 465, "username": "", "password": "", "sender": "", "recipient": ""},
}


def _get_or_create_secrets() -> dict:
    if os.path.exists(SECRETS_FILE):
        with open(SECRETS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    with open(SECRETS_FILE, "w", encoding="utf-8") as f:
        json.dump(_PLACEHOLDER_SECRETS, f, indent=2)
    os.chmod(SECRETS_FILE, 0o600)
    log.warning(
        "%s did not exist - created with empty placeholders. "
        "Fill in real MQTT/SMTP credentials there directly, then restart.",
        SECRETS_FILE,
    )
    return _PLACEHOLDER_SECRETS


def send_email_alert(smtp_cfg: dict, source: str, probability: float) -> None:
    if not smtp_cfg.get("host"):
        log.debug("SMTP not configured, skipping email alert.")
        return
    body = (
        f"La camera '{source}' a repere une proie possible (confiance {probability:.0f}%).\n"
        f"La chatiere est verrouillee dans les deux sens pendant quelques minutes par precaution."
    )
    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = "Kittyhack - proie suspectee, chatiere verrouillee"
    msg["From"] = smtp_cfg["sender"]
    msg["To"] = smtp_cfg["recipient"]
    try:
        with smtplib.SMTP_SSL(smtp_cfg["host"], int(smtp_cfg["port"]), timeout=10) as server:
            server.login(smtp_cfg["username"], smtp_cfg["password"])
            server.sendmail(smtp_cfg["username"], [smtp_cfg["recipient"]], msg.as_string())
        log.info("Sent email alert for '%s'.", source)
    except Exception as e:
        log.error("Failed to send email alert: %s", e)


class MqttStatus:
    """Thin wrapper around paho-mqtt: camera online/offline + lockdown events."""

    def __init__(self, mqtt_cfg: dict):
        self._connected = False
        if not mqtt_cfg.get("host"):
            log.debug("MQTT not configured, skipping.")
            self.client = None
            return
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="kittyhack-watchdog")
        self.client.username_pw_set(mqtt_cfg["username"], mqtt_cfg["password"])
        # 04.10, Sid: piggyback on kittyhack-remote's own existing PIR publish
        # (it already fires this on every real outside-motion edge) instead of
        # adding a new API endpoint - wakes the chatiere camera up to full
        # speed only when something is actually happening there.
        motion_topic = MQTTConfig.topics["motion_outside_state"]

        def _on_connect(client, userdata, flags, reason_code, properties=None):
            client.subscribe(motion_topic)

        def _on_message(client, userdata, msg):
            if msg.payload.decode(errors="ignore") == "detected":
                _chatiere_boost_until[0] = time.monotonic() + CHATIERE_BOOST_DURATION_S

        self.client.on_connect = _on_connect
        self.client.on_message = _on_message
        try:
            self.client.connect(mqtt_cfg["host"], int(mqtt_cfg["port"]), keepalive=30)
            self.client.loop_start()
            self._connected = True
            log.info("Connected to MQTT broker at %s:%s.", mqtt_cfg["host"], mqtt_cfg["port"])
        except Exception as e:
            log.error("Could not connect to MQTT broker: %s", e)

    def publish_camera_status(self, camera: str, status: str) -> None:
        if not self._connected:
            return
        self.client.publish(f"{MQTT_TOPIC_PREFIX}/{camera}/status", status, retain=True)

    def publish_lockdown(self, source: str, probability: float) -> None:
        if not self._connected:
            return
        payload = json.dumps({
            "source": source,
            "probability": probability,
            "timestamp": time.time(),
        })
        self.client.publish(f"{MQTT_TOPIC_PREFIX}/lockdown", payload, retain=True)


def _get_or_create_token() -> str:
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)["token"]
    raw, _record = create_token("multi-camera-watchdog")
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"token": raw}, f)
    os.chmod(TOKEN_FILE, 0o600)
    log.info("Created a new API token for the watchdog (%s)", TOKEN_FILE)
    return raw


def _resolve_active_model() -> tuple[str, str, int]:
    """Return ("yolo", path, imgsz) for the custom model currently active in
    Kittyhack's own Configuration (Manage models), or ("tflite", path, 0) for
    the bundled default if none is active/resolvable. Re-checked on every
    MouseDetector() construction so a newly-activated custom model is picked
    up on the watchdog's next restart without editing this file.

    imgsz MUST match what the model was trained/exported at (Kittyhack shows
    this as "Taille d'image" per model) - an NCNN export has a fixed input
    shape baked in, and calling .predict() without matching imgsz silently
    produces garbage (hundreds of ~100%-confidence boxes covering the whole
    frame) instead of an error, discovered the hard way: 300 "detections" at
    99%+ confidence on pure random noise before this was added.
    """
    try:
        unique_id = (CONFIG.get("YOLO_MODEL") or "").strip()
        if unique_id:
            model_dir = YoloModel.get_model_path(unique_id)
            if model_dir:
                imgsz = YoloModel.get_model_image_size(unique_id) or 320
                ncnn_dir = os.path.join(model_dir, "best_ncnn_model")
                if os.path.isdir(ncnn_dir):
                    return "yolo", ncnn_dir, imgsz
                pt_path = os.path.join(model_dir, "model.pt")
                if os.path.exists(pt_path):
                    return "yolo", pt_path, imgsz
                log.warning("Active custom model '%s' has no usable export, falling back to default.", unique_id)
    except Exception as e:
        log.warning("Could not resolve active custom model, falling back to default: %s", e)
    return "tflite", MODEL_PATH, 0


# Same literal check as Kittyhack's own src/model/detection.py, so a custom
# model trained through Kittyhack's own Label Studio pipeline (labels are
# whatever the user typed - cat names, "Prey", "Beute", ...) is read the
# same way here as it is for the flap's own camera.
_PREY_LABELS = ("prey", "beute")


class MouseDetector:
    """Loads the active model once; run() takes a BGR frame, returns prey% (0-100).

    Uses Kittyhack's own custom-trained YOLO model when one is active in
    Configuration (same model the flap's own camera uses), falling back to
    the bundled default TFLite model otherwise.
    """

    def __init__(self):
        self.kind, self.model_path, self.imgsz = _resolve_active_model()
        if self.kind == "yolo":
            from ultralytics import YOLO
            self.yolo = YOLO(self.model_path, task="detect", verbose=False)
            log.info("Loaded custom YOLO model for prey detection: %s (imgsz=%d)", self.model_path, self.imgsz)
        else:
            with open(LABELS_PATH, "r", encoding="utf-8") as f:
                self.labels = [line.strip() for line in f if line.strip()]
            self.interpreter = Interpreter(model_path=MODEL_PATH, num_threads=2)
            self.interpreter.allocate_tensors()
            self.input_details = self.interpreter.get_input_details()
            self.output_details = self.interpreter.get_output_details()
            self.height = self.input_details[0]["shape"][1]
            self.width = self.input_details[0]["shape"][2]
            self.floating_model = self.input_details[0]["dtype"] == np.float32
            out_name = self.output_details[0]["name"]
            if "StatefulPartitionedCall" in out_name:
                self.boxes_idx, self.classes_idx, self.scores_idx = 1, 3, 0
            elif "detected_scores:0" in out_name:
                self.boxes_idx, self.classes_idx, self.scores_idx = 1, 2, 0
            else:
                self.boxes_idx, self.classes_idx, self.scores_idx = 0, 1, 2
            log.info("Loaded default TFLite model for prey detection: %s", self.model_path)

    def run(self, frame: np.ndarray) -> tuple[float, bool]:
        """Returns (prey_probability, any_detection) - the latter true if
        *any* label (cat identity or prey) cleared TRAINING_SAMPLE_MIN_CONFIDENCE,
        used to gate training-sample capture regardless of whether it was
        specifically prey."""
        if self.kind == "yolo":
            return self._run_yolo(frame)
        return self._run_tflite(frame)

    def _run_yolo(self, frame: np.ndarray) -> tuple[float, bool]:
        results = self.yolo.predict(frame, imgsz=self.imgsz, verbose=False)
        prey_probability = 0.0
        any_detection = False
        for r in results:
            if r.boxes is None:
                continue
            names = r.names if hasattr(r, "names") else {}
            for cls_idx, conf in zip(r.boxes.cls.tolist(), r.boxes.conf.tolist()):
                label = str(names.get(int(cls_idx), "")).lower()
                probability = float(conf * 100)
                if label in _PREY_LABELS and probability > prey_probability:
                    prey_probability = probability
                if probability >= TRAINING_SAMPLE_MIN_CONFIDENCE:
                    any_detection = True
        return prey_probability, any_detection

    def _run_tflite(self, frame: np.ndarray) -> tuple[float, bool]:
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        frame_resized = cv2.resize(frame_rgb, (self.width, self.height))
        input_data = np.expand_dims(frame_resized, axis=0)
        if self.floating_model:
            input_data = (np.float32(input_data) - 127.5) / 127.5

        self.interpreter.set_tensor(self.input_details[0]["index"], input_data)
        self.interpreter.invoke()

        classes = self.interpreter.get_tensor(self.output_details[self.classes_idx]["index"])[0]
        scores = self.interpreter.get_tensor(self.output_details[self.scores_idx]["index"])[0]
        if np.isscalar(scores):
            scores = np.array([scores])
        if np.isscalar(classes):
            classes = np.array([classes])

        mouse_probability = 0.0
        any_detection = False
        for i in range(len(scores)):
            object_name = self.labels[int(classes[i])]
            probability = float(scores[i] * 100)
            if object_name == "Maus" and probability > mouse_probability:
                mouse_probability = probability
            if probability >= TRAINING_SAMPLE_MIN_CONFIDENCE:
                any_detection = True
        return mouse_probability, any_detection


def _is_frame_usable(frame: np.ndarray) -> bool:
    """Reject a decoded-but-corrupted frame (near-flat gray field from h264
    error concealment) before it reaches detection/saving."""
    try:
        return float(frame.std()) >= BLANK_FRAME_STD_THRESHOLD
    except Exception:
        return True


def _save_training_sample(cam_name: str, frame: np.ndarray) -> None:
    """Append a full-resolution timestamped JPEG for later labeling."""
    try:
        os.makedirs(TRAINING_SAMPLES_DIR, exist_ok=True)
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        if not ok:
            return
        filename = f"{cam_name}_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
        with open(os.path.join(TRAINING_SAMPLES_DIR, filename), "wb") as f:
            f.write(buf.tobytes())
    except Exception as e:
        log.warning("[%s] Could not save training sample: %s", cam_name, e)


def _save_frame_snapshot(cam_name: str, frame: np.ndarray) -> None:
    """Atomically write the latest frame as a small JPEG for the UI preview."""
    try:
        os.makedirs(FRAME_SNAPSHOT_DIR, exist_ok=True)
        # Downscale - this is a preview thumbnail, not a detection input.
        preview = cv2.resize(frame, (480, 270)) if frame.shape[1] > 480 else frame
        ok, buf = cv2.imencode(".jpg", preview, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        if not ok:
            return
        final_path = os.path.join(FRAME_SNAPSHOT_DIR, f"{cam_name}.jpg")
        tmp_path = final_path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(buf.tobytes())
        os.replace(tmp_path, final_path)
    except Exception as e:
        log.warning("[%s] Could not save frame snapshot: %s", cam_name, e)


class CameraWatcher(threading.Thread):
    """One thread per camera: keeps a VideoCapture alive, polls for prey."""

    def __init__(self, name: str, rtsp_url: str, detector: MouseDetector, on_prey_detected, mqtt_status: "MqttStatus"):
        super().__init__(daemon=True, name=f"cam-{name}")
        self.cam_name = name
        self.rtsp_url = rtsp_url
        self.detector = detector
        self.on_prey_detected = on_prey_detected
        self.mqtt_status = mqtt_status
        self._stop = threading.Event()
        # Instance attribute (not a run()-local) so a stream reconnect - common
        # on a weak-signal camera - doesn't reset the sampling cadence.
        self._last_sample_at = 0.0
        self._base_poll_interval = CAMERA_POLL_INTERVAL_S.get(name, DEFAULT_POLL_INTERVAL_S)

    def stop(self):
        self._stop.set()

    def _poll_interval(self) -> float:
        if self.cam_name == "chatiere" and time.monotonic() < _chatiere_boost_until[0]:
            return CHATIERE_BOOST_INTERVAL_S
        return self._base_poll_interval

    def run(self):
        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                log.warning("[%s] Could not open stream, retrying in %ss", self.cam_name, RECONNECT_DELAY_S)
                self.mqtt_status.publish_camera_status(self.cam_name, "offline")
                time.sleep(RECONNECT_DELAY_S)
                continue
            log.info("[%s] Stream opened.", self.cam_name)
            self.mqtt_status.publish_camera_status(self.cam_name, "online")
            last_poll = 0.0
            try:
                while not self._stop.is_set():
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        log.warning("[%s] Lost frame, reconnecting.", self.cam_name)
                        self.mqtt_status.publish_camera_status(self.cam_name, "offline")
                        break
                    now = time.monotonic()
                    if now - last_poll < self._poll_interval():
                        continue
                    last_poll = now
                    if not _is_frame_usable(frame):
                        log.warning("[%s] Corrupted/blank frame (decode error concealment), skipping.", self.cam_name)
                        continue
                    _save_frame_snapshot(self.cam_name, frame)
                    try:
                        mouse_probability, any_detection = self.detector.run(frame)
                    except Exception as e:
                        log.error("[%s] Inference error: %s", self.cam_name, e)
                        continue
                    if any_detection and now - self._last_sample_at >= TRAINING_SAMPLE_COOLDOWN_S:
                        self._last_sample_at = now
                        _save_training_sample(self.cam_name, frame)
                    if mouse_probability >= WATCHDOG_MOUSE_THRESHOLD:
                        log.warning("[%s] Prey suspected (%.0f%%).", self.cam_name, mouse_probability)
                        self.on_prey_detected(self.cam_name, mouse_probability)
            finally:
                cap.release()
            if not self._stop.is_set():
                time.sleep(RECONNECT_DELAY_S)


class LockdownController:
    """Calls Kittyhack's own REST API to lock both directions, with a cooldown
    so repeated detections during the same window don't spam the API."""

    def __init__(self, token: str, lock_duration_s: float, mqtt_status: "MqttStatus", smtp_cfg: dict):
        self.token = token
        self.lock_duration_s = lock_duration_s
        self.mqtt_status = mqtt_status
        self.smtp_cfg = smtp_cfg
        self._lock = threading.Lock()
        self._locked_until = 0.0

    def _call(self, endpoint: str) -> None:
        try:
            r = requests.post(
                f"{API_BASE}/door/{endpoint}",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=5,
            )
            if not r.ok:
                log.error("API call %s failed: %s %s", endpoint, r.status_code, r.text)
        except Exception as e:
            log.error("API call %s failed: %s", endpoint, e)

    def trigger(self, source: str, probability: float) -> None:
        with self._lock:
            now = time.monotonic()
            already_locked = now < self._locked_until
            self._locked_until = now + self.lock_duration_s
            if already_locked:
                # Already in a lockdown window - just extend it, no need to
                # re-issue the API calls.
                return
        log.warning(
            "Locking the flap for %ss - prey suspected on '%s' (%.0f%%).",
            self.lock_duration_s, source, probability,
        )
        self._call("lock_inside")
        self._call("lock_outside")
        self.mqtt_status.publish_lockdown(source, probability)
        send_email_alert(self.smtp_cfg, source, probability)


def main():
    load_config()
    lock_duration = float(CONFIG.get("LOCK_DURATION_AFTER_PREY_DETECTION", 180) or 180)
    log.info("Starting multi-camera watchdog (%d cameras, threshold %.0f%%, lock %ss)",
              len(CAMERAS), WATCHDOG_MOUSE_THRESHOLD, lock_duration)

    token = _get_or_create_token()
    secrets = _get_or_create_secrets()
    mqtt_status = MqttStatus(secrets["mqtt"])
    controller = LockdownController(token, lock_duration, mqtt_status, secrets["smtp"])

    # Each thread gets its own Interpreter instance - a TFLite interpreter is
    # not safe to call concurrently from multiple threads.
    watchers = [
        CameraWatcher(name, url, MouseDetector(), controller.trigger, mqtt_status)
        for name, url in CAMERAS.items()
    ]
    for w in watchers:
        w.start()
    for w in watchers:
        w.join()


if __name__ == "__main__":
    main()
