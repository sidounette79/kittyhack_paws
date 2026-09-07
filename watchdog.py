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
from src.paths import kittyhack_root

logging.basicConfig(level=logging.INFO, format="%(asctime)s [WATCHDOG] %(message)s")
log = logging.getLogger("watchdog")

TOKEN_FILE = os.path.join(kittyhack_root(), "watchdog_token.json")
API_BASE = "http://127.0.0.1:80/api/v1"
POLL_INTERVAL_S = 1.5
RECONNECT_DELAY_S = 5.0

# Extra cameras only - the flap's own primary camera is already covered by
# Kittyhack itself. Fill in real RTSP URLs before running.
CAMERAS = {
    "chatiere": "rtsp://admin:Dorothee79%25@192.168.178.21:554/h264Preview_01_main",
    "terrasse": "rtsp://admin:Dorothee79%25@192.168.178.44:554/h264Preview_01_main",
    "jardin_japonais": "rtsp://admin:Dorothee79@192.168.178.138:554/h264Preview_01_main",
}

# Deliberately more cautious than Kittyhack's own default (70): a false
# lockdown just costs a cat a few extra minutes outside, a missed one costs
# a live mouse loose in the house.
WATCHDOG_MOUSE_THRESHOLD = 50.0

MODEL_PATH = os.path.join(kittyhack_root(), "tflite", "original_kittyflap_model_v2", "cv-lite-model.tflite")
LABELS_PATH = os.path.join(kittyhack_root(), "tflite", "original_kittyflap_model_v2", "labels.txt")

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


class MouseDetector:
    """Loads the TFLite model once; run() takes a BGR frame, returns mouse% (0-100)."""

    def __init__(self):
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

    def run(self, frame: np.ndarray) -> float:
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
        for i in range(len(scores)):
            object_name = self.labels[int(classes[i])]
            probability = float(scores[i] * 100)
            if object_name == "Maus" and probability > mouse_probability:
                mouse_probability = probability
        return mouse_probability


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

    def stop(self):
        self._stop.set()

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
                    if now - last_poll < POLL_INTERVAL_S:
                        continue
                    last_poll = now
                    try:
                        mouse_probability = self.detector.run(frame)
                    except Exception as e:
                        log.error("[%s] Inference error: %s", self.cam_name, e)
                        continue
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
