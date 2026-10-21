"""Multi-camera watchdog (07.09, Sid).

Kittyhack itself only ever analyses ONE active camera source at a time
(CAMERA_SOURCE = internal | ip_camera) - there's no built-in way to fuse
several cameras into one prey decision. This script is a small, separate
process that watches N independent RTSP cameras in parallel (the ones
covering the terrace/catio approach, not just the flap itself) and captures
frames for Kittyhack's own UI (Presence/Live preview) and training-sample
archive (Parcours correlation, Label Studio).

08.10, Sid ("désactive le blocage via watchdog" / "c'est clairement pas
fiable"): this used to ALSO run its own independent prey model per camera
and lock the real flap + send an email the instant any one of them saw a
mouse/bird with high enough confidence - a second, less reliable layer on
top of Kittyhack's own already-trusted prey detection on its primary
camera (single low camera-distance frame, no corroboration). That trigger
is now fully disabled (see CameraWatcher.run()'s comment) - kept in the
code, not deleted, in case it's ever wanted back. Local YOLO/TFLite
inference itself now only runs for "chatiere" (close enough to the action
to be reliable); the 3 peripheral cameras instead poll their own onboard
Reolink AI (GetAiState) to know when to save a training-sample frame - see
REOLINK_HTTP below.

Run as a second process alongside app.py (see compose.yaml).
"""

import json
import logging
from logging.handlers import TimedRotatingFileHandler
import os
import select
import smtplib
import subprocess
import threading
import time
from email.mime.text import MIMEText

import cv2
import numpy as np
import paho.mqtt.client as mqtt
import requests
import urllib3
from tflite_runtime.interpreter import Interpreter

# terrasse's Reolink HTTPS endpoint uses a self-signed cert (verify=False by
# design, same LAN, same credentials already trusted via RTSP) - silence the
# resulting per-request warning instead of letting it spam the log on every
# poll.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from src.api import create_token
from src.baseconfig import CONFIG, load_config
from src.mqtt import MQTTConfig
from src.paths import kittyhack_root
from src.model.yolo_model import YoloModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [WATCHDOG] %(message)s")
log = logging.getLogger("watchdog")

# 11.10, Sid ("on peut garder en stock les logs... pendant 7 jours ?"):
# same fix as kittyhack-remote's own configure_logging() - `docker logs` is
# tied to the current container id, lost on any --force-recreate. Separate
# file from kittyhack-remote's own (different container, same /data bind
# mount), daily rotation, 7 days kept.
try:
    _log_dir = "/data/logs"
    os.makedirs(_log_dir, exist_ok=True)
    _file_handler = TimedRotatingFileHandler(
        os.path.join(_log_dir, "kittyhack-watchdog.log"),
        when="midnight", backupCount=7, encoding="utf-8",
    )
    _file_handler.setFormatter(logging.Formatter("%(asctime)s [WATCHDOG] %(message)s"))
    logging.getLogger().addHandler(_file_handler)
except Exception as e:
    log.warning("[LOGGING] Failed to set up persistent file log: %s", e)

TOKEN_FILE = os.path.join(kittyhack_root(), "watchdog_token.json")
# 11.10, Sid: real crash-notification gap found - this container used to
# reach kittyhack-remote via network_mode: "service:kittyhack-remote"
# (shares its network namespace, so 127.0.0.1 worked), which breaks every
# time kittyhack-remote restarts for ANY reason, not just a deliberate
# --force-recreate (confirmed tonight: a plain restart, same container id,
# still tears down the shared netns - every camera went "Network is
# unreachable" simultaneously, and the watchdog stayed blind to the Pi
# crash it was supposed to be watching for, so its own "disconnected 2min"
# notification never fired). Both containers already sit on the same
# Compose default bridge network regardless (see compose.yaml) - reaching
# kittyhack-remote by its service name instead of 127.0.0.1 and joining
# that normal network (compose.yaml change alongside this) removes the
# fragile coupling entirely instead of needing a manual recreate forever.
API_BASE = "http://kittyhack-remote:80/api/v1"
RECONNECT_DELAY_S = 5.0

# 04.10, Sid: the watchdog + kittyhack-remote's own continuous chatiere
# analysis together were pegging the whole NAS (confirmed via docker stats:
# ~376% combined CPU on a 4-core box, same on both FUNMEDIA and OldMedia -
# moving host never was going to fix this, the two containers alone need
# nearly the whole machine). Prey pre-detection on the 3 peripheral cameras
# doesn't need sub-2s latency to still be useful, so they're cut way down.
# 08.10, Sid ("réduire sa cadence (2s -> plus lent, sauf pendant le boost
# PIR)"): cutting local detection on the 3 peripheral cameras barely moved
# watchdog's own CPU (still ~119%, confirmed via `ps` right after deploy) -
# chatiere's own 2s-cadence inference was already the dominant cost on its
# own, independent of the other 3. Idle base interval slowed down to 8s -
# the PIR-triggered boost (CHATIERE_BOOST_*, MQTT subscription in
# MqttStatus below) already covers "a cat is actually approaching" with a
# real 1.5s cadence for 20s, so the idle rate doesn't need to be fast too.
CAMERA_POLL_INTERVAL_S = {
    "chatiere": 8.0,
    "terrasse": 10.0,
    "jardin_japonais": 10.0,
    "entree": 10.0,
}
DEFAULT_POLL_INTERVAL_S = 10.0
# 11.10, Sid ("le réglage ne devrait être actif que quand il y a une
# détection... CPU ça fait quoi en plus ?"): tightened from 1.5s - the RTSP
# decode itself (cap.read(), _run_continuous() below) already runs flat-out
# continuously regardless of this value, so the only real marginal cost is
# more frequent local YOLO inference calls (self.detector.run()), and only
# within an active CHATIERE_BOOST_DURATION_S trigger window, never while
# idle - confirmed, not just assumed, by reading _poll_interval()'s own
# gating logic. First real-world step towards CAM PI's own 10fps
# (REMOTE_INFERENCE_MAX_FPS) cadence - re-measure actual CPU% during a real
# trigger before tightening further.
CHATIERE_BOOST_INTERVAL_S = 0.3
CHATIERE_BOOST_DURATION_S = 20.0
# 08.10, Sid ("elle s'active au PIR et elle s'active si une des Reolink
# détecte quelque chose - pendant 3 minutes? comme ça elle est prête en cas
# de défaillance du PIR"): a second, longer trigger source backing up the
# PIR one - a peripheral camera's own Reolink AI seeing an animal is a
# less immediate/precise signal than the PIR (it could be any of the 3
# cameras, not necessarily right at the flap), so it gets a longer window
# to still catch the cat by the time it actually reaches the flap.
CHATIERE_REOLINK_TRIGGER_DURATION_S = 180.0
# 21.10, Sid: cadence du poll Reolink lui-même (voir le commentaire sur
# CameraWatcher._last_reolink_poll) - 2s plutôt que l'ancien 8s hérité du
# cycle idle, sans toucher à ce dernier (toujours utile pour
# snapshot/inférence locale, plus coûteux).
CHATIERE_REOLINK_POLL_INTERVAL_S = 2.0
# Written by MqttStatus's on_message handler AND by a peripheral camera's
# own thread (via _extend_chatiere_trigger below), read by chatiere's
# CameraWatcher thread - a single float assignment/read is atomic enough in
# CPython for this (no lock needed, worst case is one stale poll decision).
_chatiere_boost_until = [0.0]


def _extend_chatiere_trigger(duration_s: float) -> None:
    """Push chatiere's trigger window forward, never backward - a shorter
    new trigger (e.g. PIR) must never cut a longer one already in progress
    (e.g. a Reolink detection) short."""
    candidate = time.monotonic() + duration_s
    if candidate > _chatiere_boost_until[0]:
        _chatiere_boost_until[0] = candidate

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
# 08.10, Sid: physically swapped the "Chambre boubous" camera (the only one
# of the 4 with an HTTP/HTTPS toggle in its Reolink server settings) into
# the chatiere position - IPs are DHCP-reserved/fixed, so .121 follows the
# camera to its new physical spot rather than staying tied to "chatiere".
# 09.10, Sid ("il faut rajouter l'ancienne chatière... celle qui était
# chatière avant et qui est devenue chatière Int"): the original chatiere
# camera (RTSP+ONVIF only, no HTTP/HTTPS toggle - see REOLINK_HTTP's own
# comment below, confirmed again live via a real cv2.VideoCapture test on
# .21) - physically repositioned indoors, facing the flap from the inside.
# Kept out of REOLINK_HTTP/CAMERAS_WITH_DETECTION: no onboard AI capability
# on this camera, same reason the original chatiere position never had it
# either before the swap.
CAMERAS = {
    # 11.10 (round 2), Sid ("passer en basse résolution... beaucoup plus
    # de frames"): was _01_main (2560x1440) - under real CPU contention
    # during an actual passage (confirmed live, up to ~500% combined
    # demand on this 4-core box), decoding that high-res stream was the
    # real bottleneck, not the CHATIERE_BOOST_INTERVAL_S poll setting -
    # confirmed via the real API response for a live exit: only 8 frames
    # in the whole event window, with a 31s capture gap right before the
    # cat even left. Switched to _01_sub (640x360, same resolution as the
    # other cameras below) - much cheaper to decode, so the boost interval
    # can actually be reached in practice. Trade-off: slightly less sharp
    # per-frame identification, accepted knowingly over density.
    "chatiere": "rtsp://admin:Dorothee79%25@192.168.178.121:554/h264Preview_01_sub",
    # 09.10, Sid ("elle était toujours en flux de qualité... tu peux la
    # descendre en basse qualité"): _01_sub (640x360), not _01_main
    # (2560x1440) - same reasoning as terrasse/entree's own sub-stream
    # switch above, just missed it when adding this one.
    "chatiere_int": "rtsp://admin:Dorothee79%25@192.168.178.21:554/h264Preview_01_sub",
    "terrasse": "rtsp://admin:Dorothee79%25@192.168.178.44:554/h264Preview_01_sub",
    "jardin_japonais": "rtsp://admin:Dorothee79@192.168.178.138:554/h264Preview_01_main",
    "entree": "rtsp://admin:Dorothee79%25@192.168.178.108:554/h264Preview_01_sub",
}

# 08.10, Sid ("peut-on se brancher au détecteur de mouvement de présence de
# reolink pour donner de meilleures images dans parcours"): each peripheral
# camera's own onboard AI (GetAiState, api.cgi) replaces this script's local
# model for them (see CAMERAS_WITH_DETECTION below) - both as the CPU-saving
# move already agreed, AND to drive TRAINING_SAMPLES_DIR captures (what
# journeys.py's Parcours correlation actually reads) off a real "there's a
# cat in frame" signal instead of local inference. Port/protocol verified
# directly against each real camera today (curl), not assumed - they differ
# per camera's own HTTP/HTTPS toggle in its Reolink app settings:
#   - jardin_japonais, entree: plain HTTP on port 80.
#   - terrasse, chatiere: HTTPS only (HTTP disabled), self-signed cert.
# Password taken from the same RTSP credentials above (one real % in
# "Dorothee79%25" decodes to a literal "%" in the password - matches what's
# actually configured on the camera, confirmed working).
#
# 08.10, Sid: the original chatiere camera had no HTTP/HTTPS toggle at all
# (RTSP+ONVIF only, confirmed by exhaustive port/protocol testing earlier
# today) - she swapped it with "Chambre boubous" (which does have one,
# verified live via curl: GetAiState responds with dog_cat.support=1) so
# chatiere now ALSO gets this as a third trigger source, backing up both
# the PIR and the 3 peripheral cameras' own Reolink AI - see
# CameraWatcher._run_continuous()'s own poll below. Unlike the 3 peripheral
# entries, chatiere keeps self.detector (CAMERAS_WITH_DETECTION) and never
# switches to _run_peripheral() - see CameraWatcher.run()'s dispatch.
REOLINK_HTTP = {
    "chatiere": {"host": "192.168.178.121", "https": True, "user": "admin", "password": "Dorothee79%"},
    "terrasse": {"host": "192.168.178.44", "https": True, "user": "admin", "password": "Dorothee79%"},
    "jardin_japonais": {"host": "192.168.178.138", "https": False, "user": "admin", "password": "Dorothee79"},
    "entree": {"host": "192.168.178.108", "https": False, "user": "admin", "password": "Dorothee79%"},
}


def _reolink_dog_cat_detected(cfg: dict) -> bool | None:
    """Poll this camera's own onboard AI state directly - True/False if we
    got a real answer, None on any error (treated as "no signal, don't
    capture" by the caller, same as a transient network issue shouldn't
    spam the log or fabricate a detection)."""
    scheme = "https" if cfg["https"] else "http"
    url = f"{scheme}://{cfg['host']}/api.cgi"
    try:
        r = requests.get(
            url,
            params={"cmd": "GetAiState", "channel": 0, "user": cfg["user"], "password": cfg["password"]},
            timeout=5,
            verify=False,
        )
        r.raise_for_status()
        data = r.json()
        return bool(data[0]["value"]["dog_cat"]["alarm_state"])
    except Exception as e:
        log.debug("Reolink GetAiState failed for %s: %s", cfg["host"], e)
        return None


def _wake_kittyflap_camera(token: str, duration_s: float) -> None:
    """08.10, Sid ("la détection Reolink active potentiellement la caméra
    KF pour les cas où PIR a un angle mort"): a Reolink camera's own AI
    seeing an animal is relayed to kittyhack-remote's own internal flap
    camera (PAUSE_CAMERA_WHEN_IDLE) via this small authenticated API call -
    src/api.py's POST /camera/wake. Best-effort: a failure here (network
    hiccup, API down) just means this one backup path didn't fire - PIR and
    the Reolink-side detection/boost in this process are unaffected."""
    try:
        r = requests.post(
            f"{API_BASE}/camera/wake",
            headers={"Authorization": f"Bearer {token}"},
            json={"duration_s": duration_s},
            timeout=5,
        )
        if not r.ok:
            log.warning("camera/wake API call failed: %s %s", r.status_code, r.text)
    except Exception as e:
        log.debug("camera/wake API call failed: %s", e)


def _grab_single_frame(rtsp_url: str, discard_reads: int = 2) -> np.ndarray | None:
    """Open a short-lived RTSP connection, read (and discard) a couple of
    initial frames - an RTSP decoder needs a moment to sync to a real
    I-frame, the very first read(s) right after opening can be garbage/stale
    - then return one real frame and close immediately. Used by
    CameraWatcher._run_peripheral() instead of a persistent VideoCapture,
    so a camera with its own Reolink AI never pays for continuous H264
    decode when nothing's happening (08.10, Sid)."""
    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    try:
        if not cap.isOpened():
            return None
        frame = None
        for _ in range(discard_reads + 1):
            ok, frame = cap.read()
            if not ok:
                return None
        return frame
    finally:
        cap.release()

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
                _extend_chatiere_trigger(CHATIERE_BOOST_DURATION_S)

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


# 21.10, Sid ("on teste ça" - partager le GPU entre KH et watchdog, sans
# garde-fou, et mesurer si la contention reste acceptable) - avant ça,
# watchdog chargeait TOUJOURS l'export NCNN du modèle actif quand il
# existait (ci-dessous), un format CPU-only (pas de device= qui y change
# quoi que ce soit, NCNN n'a aucun backend Intel GPU) - ce n'était donc pas
# juste "pas de device=intel:gpu passé", le format de modèle chargé lui-
# même excluait le GPU. Même bascule OpenVINO que src/model/model_handler.py
# (kittyhack-remote) et model_review.py (Paws Review) - export à la demande
# vers model_openvino_model/, chargé sans device= explicite (AUTO
# sélectionne le GPU tout seul, voir le commentaire déjà existant côté
# model_handler.py sur ce point).
def _uses_openvino_backend() -> bool:
    device = str(CONFIG.get("INFERENCE_DEVICE", "cpu") or "cpu").strip().lower()
    return device in {"gpu", "intel:gpu", "intel:cpu", "intel:npu"}


def _ensure_openvino_export(model_dir: str) -> str | None:
    """Export model_dir/model.pt to OpenVINO IR on demand; returns the IR
    directory path, or None if no model.pt is available to export from."""
    ov_dir = os.path.join(model_dir, "model_openvino_model")
    if os.path.isdir(ov_dir) and any(f.endswith(".xml") for f in os.listdir(ov_dir)):
        return ov_dir
    pt_path = os.path.join(model_dir, "model.pt")
    if not os.path.exists(pt_path):
        return None
    try:
        from ultralytics import YOLO as _YOLO
        log.info("OpenVINO export missing for '%s'. Exporting now...", pt_path)
        export_model = _YOLO(pt_path, task="detect", verbose=False)
        export_result = export_model.export(format="openvino")
        result_dir = export_result if isinstance(export_result, str) and export_result.strip() else ov_dir
        if os.path.isdir(result_dir) and any(f.endswith(".xml") for f in os.listdir(result_dir)):
            return result_dir
    except Exception as e:
        log.warning("OpenVINO export failed for '%s': %s", pt_path, e)
    return None


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
                if _uses_openvino_backend():
                    ov_dir = _ensure_openvino_export(model_dir)
                    if ov_dir:
                        return "yolo", ov_dir, imgsz
                    log.warning("GPU inference requested but OpenVINO export unavailable for '%s', falling back to NCNN/CPU.", unique_id)
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

    def run(self, frame: np.ndarray) -> tuple[float, bool, list[dict]]:
        """Returns (prey_probability, any_detection, detections) - the
        middle value true if *any* label (cat identity or prey) cleared
        TRAINING_SAMPLE_MIN_CONFIDENCE, used to gate training-sample capture
        regardless of whether it was specifically prey. ``detections`` is
        every box found (name/probability/x/y/width/height, percent,
        top-left-anchored - same convention as DetectedObjectSchema in
        src/database.py), regardless of confidence - 10.10, Sid ("avec les
        incrustations aussi"): used to draw boxes on the dense event-frame
        sequence (see _save_event_frame below), not just gate a save."""
        if self.kind == "yolo":
            return self._run_yolo(frame)
        return self._run_tflite(frame)

    def _run_yolo(self, frame: np.ndarray) -> tuple[float, bool, list[dict]]:
        results = self.yolo.predict(frame, imgsz=self.imgsz, verbose=False)
        prey_probability = 0.0
        any_detection = False
        detections: list[dict] = []
        frame_h, frame_w = frame.shape[0], frame.shape[1]
        for r in results:
            if r.boxes is None:
                continue
            names = r.names if hasattr(r, "names") else {}
            for cls_idx, conf, xyxy in zip(
                r.boxes.cls.tolist(), r.boxes.conf.tolist(), r.boxes.xyxy.tolist()
            ):
                label = str(names.get(int(cls_idx), "")).lower()
                probability = float(conf * 100)
                if label in _PREY_LABELS and probability > prey_probability:
                    prey_probability = probability
                if probability >= TRAINING_SAMPLE_MIN_CONFIDENCE:
                    any_detection = True
                x1, y1, x2, y2 = xyxy
                detections.append(
                    {
                        "name": label,
                        "probability": round(probability, 1),
                        "x": round(100.0 * x1 / frame_w, 2),
                        "y": round(100.0 * y1 / frame_h, 2),
                        "width": round(100.0 * (x2 - x1) / frame_w, 2),
                        "height": round(100.0 * (y2 - y1) / frame_h, 2),
                    }
                )
        return prey_probability, any_detection, detections

    def _run_tflite(self, frame: np.ndarray) -> tuple[float, bool, list[dict]]:
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
        # 10.10, Sid: this fallback TFLite model isn't the one active on her
        # "chatiere" camera today (custom YOLO is), and its own output
        # tensors don't carry box coordinates in the same layout handled
        # above - no box overlay for this path, just the existing gate.
        for i in range(len(scores)):
            object_name = self.labels[int(classes[i])]
            probability = float(scores[i] * 100)
            if object_name == "Maus" and probability > mouse_probability:
                mouse_probability = probability
            if probability >= TRAINING_SAMPLE_MIN_CONFIDENCE:
                any_detection = True
        return mouse_probability, any_detection, []


def _drain_stale_frames(cap: "cv2.VideoCapture", max_grabs: int = 15) -> None:
    """13.10, Sid (root-caused via live evidence - a Reolink clip saved for
    a real event showed a frozen scene ~18s before the cat actually
    entered, while the Kittyflap panel tracked real time correctly):
    cv2.VideoCapture with the FFmpeg backend buffers decoded frames
    internally, and this loop spends up to 5s blocked on the Reolink HTTP
    poll (_reolink_dog_cat_detected) plus variable YOLO inference time
    between reads - the stream keeps arriving and queues up during that
    block. The next cap.read() then returns the OLDEST queued frame
    (FIFO), not the live one: captured_at (11.10's fix, stamped right
    after read()) correctly reflects wall-clock read time, but the
    frame's actual visual content (and Reolink's own burned-in overlay
    timestamp) stays stale behind it. CAP_PROP_BUFFERSIZE=1 alone isn't
    reliable (some FFmpeg backends ignore it) - grab() without retrieve()
    skips the decode step, so this is cheap, and running it right before
    every read() fast-forwards through whatever queued up since the last
    one. Bounded so a genuinely dead/slow stream can't hang the loop; the
    throttled fast path (many quick reads between heavy polls, see the
    poll-interval check right after this) means any backlog too big to
    clear in one pass still gets caught up within a few iterations.
    """
    for _ in range(max_grabs):
        if not cap.grab():
            break


def _is_frame_usable(frame: np.ndarray) -> bool:
    """Reject a decoded-but-corrupted frame (near-flat gray field from h264
    error concealment) before it reaches detection/saving."""
    try:
        return float(frame.std()) >= BLANK_FRAME_STD_THRESHOLD
    except Exception:
        return True


def _save_training_sample(cam_name: str, frame: np.ndarray, detections: list[dict] | None = None) -> None:
    """Append a full-resolution timestamped JPEG for later labeling.

    21.10, Sid ("dans paws review n'arrivent que les boîtes de reo chatière
    [...] mais sans les boîtes [...] j'ai des tags dans 'video parcours' et
    celles-là je les ai pas") - chatière a déjà un modèle local qui tourne
    sur ce même frame (self.detector.run(), voir CameraWatcher) et dont le
    résultat s'affiche bien dans Parcours ("boubou 70%") via
    _save_event_frame's sidecar - mais ce résultat-ci n'était jamais
    sauvegardé, donc la même image arrivait vierge dans Paws Review. Même
    convention de sidecar que _save_event_frame (json brut, clé "name" -
    la traduction vers le schéma de Paws Review, clé "label", se fait côté
    journeys.py au moment de l'upload, pas ici)."""
    try:
        os.makedirs(TRAINING_SAMPLES_DIR, exist_ok=True)
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        if not ok:
            return
        filename = f"{cam_name}_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
        with open(os.path.join(TRAINING_SAMPLES_DIR, filename), "wb") as f:
            f.write(buf.tobytes())
        if detections:
            with open(os.path.join(TRAINING_SAMPLES_DIR, filename[:-4] + ".json"), "w", encoding="utf-8") as f:
                json.dump(detections, f)
    except Exception as e:
        log.warning("[%s] Could not save training sample: %s", cam_name, e)


# 10.10, Sid ("deux vidéos côte à côte... avec les incrustations aussi"):
# a dense, unconditional frame+box sequence captured only while chatiere's
# own trigger window is active (PIR or a Reolink detection elsewhere - see
# _chatiere_boost_until), so it's naturally bounded to real passage events
# rather than growing forever. Separate from TRAINING_SAMPLES_DIR on
# purpose - that one stays sparse/gated for Label Studio, this one is dense
# for the event-comparison viewer and keeps every frame regardless of
# confidence. Storage isn't a concern ("j'ai 2 to de libre") - no retention
# logic here, same as the existing training-sample archive.
EVENT_FRAMES_DIR = "/data/watchdog_event_frames"


def _save_event_frame(cam_name: str, frame: np.ndarray, detections: list[dict], captured_at: float | None = None) -> None:
    """Save one frame of the dense per-event sequence, plus a JSON sidecar
    with its detection boxes (same shape the frontend already renders for
    the flap's own camera - see EventModal.tsx's DetectedObject).

    11.10 (round 2), Sid ("pour illustrer le decalage temporel" - a real
    screenshot showing the Reolink panel's own on-screen camera overlay
    18s earlier than the timestamp the UI was syncing against): confirmed
    real cause - this used to stamp `time.time()` fresh right here, i.e.
    AFTER the caller's Reolink AI HTTP poll (_reolink_dog_cat_detected,
    its own up-to-5s timeout) and the full YOLO inference pass
    (self.detector.run()) had already run on this same frame. Under real
    CPU contention (confirmed elsewhere this session: up to ~500%
    combined demand during an actual passage) that gap is not
    negligible, and it's cumulative tick over tick - exactly the kind of
    drift that would make the EventModal's new real-time sync (which
    trusts this filename timestamp) show a stale-looking Reolink frame
    next to a timely Pi one. `captured_at` is now stamped by the caller
    immediately after the frame is actually grabbed (cap.read() /
    _read_hw_frame()), before any of that slow processing - falls back to
    "now" only if a caller doesn't pass one (keeps this function safe to
    call from anywhere without breaking)."""
    try:
        os.makedirs(EVENT_FRAMES_DIR, exist_ok=True)
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        if not ok:
            return
        ts = captured_at if captured_at is not None else time.time()
        # Milliseconds in the filename - at a 1.5s boost cadence two frames
        # landing in the same second is unlikely but not impossible, and
        # the sort order (filename = timestamp) must stay unambiguous.
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(ts)) + f"_{int(ts * 1000) % 1000:03d}"
        filename = f"{cam_name}_{stamp}.jpg"
        with open(os.path.join(EVENT_FRAMES_DIR, filename), "wb") as f:
            f.write(buf.tobytes())
        with open(os.path.join(EVENT_FRAMES_DIR, filename[:-4] + ".json"), "w", encoding="utf-8") as f:
            json.dump(detections, f)
    except Exception as e:
        log.warning("[%s] Could not save event frame: %s", cam_name, e)


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


# 11.10 (round 2), Sid ("meilleures photos avec moins de charge CPU et
# plus de photos aussi"): OLDMEDIA's Intel iGPU (J4125, Quick Sync -
# confirmed present via /dev/dri, passed through in compose.yaml) peut
# décoder chatière en matériel - tenté une première fois ce soir-là via
# ffmpeg -hwaccel vaapi sur le flux COMPLET (2560x1920), désactivé le
# lendemain (voir plus bas) : désync RTSP ("CSeq 6 expected, 0 received")
# qui faisait planter ffmpeg toutes les 12-17s, cause racine jamais
# élucidée malgré 3 pistes ffmpeg épuisées (19.10).
#
# 21.10, Sid ("on va l'ouvrir maintenant" / "c'est en forgeant..."):
# reprise avec GStreamer plutôt que ffmpeg - testé en isolé d'abord
# (hors service réel) avant de toucher ce fichier :
#   - `vaapih264dec` (l'ancien plugin gstreamer-vaapi) plante
#     instantanément (SIGSEGV) dans CE conteneur précis, reproduit 2x.
#   - `vah264dec` (le plugin VA plus récent) tourne proprement : 2m25
#     sans la moindre erreur ni désync CSeq, arrêt propre sur EOS (la
#     caméra qui ferme la session, pas un plantage) - bien au-delà des
#     12-17s où ffmpeg s'effondrait.
#   - Le flux COMPLET (2560x1920) reste inexploitable même en matériel :
#     ~0.5 image/s mesuré (décodage+réduction logicielle après, trop
#     lourd). Le flux `_01_sub` (déjà utilisé aujourd'hui, 640x360) donne
#     ~8.7 images/s avec exactement le même pipeline matériel - largement
#     au-dessus du rythme actuel. D'où HW_DECODE_WIDTH/HEIGHT alignés sur
#     cette résolution native, pas de redimensionnement nécessaire.
#
# D'ABORD DÉSACTIVÉ LE SOIR MÊME, cause racine trouvée et vérifiée EN
# DIRECT (pas une supposition) : `vah264dec` plantait en vrai déploiement
# avec "undefined symbol: GmmGetAIL" - le pilote intel-media-va-driver
# (25.2.3) a besoin de libigdgmm12 >= 22.7.2+ds1, alors que le GPU
# compute (OpenVINO) est épinglé sur 22.3.0 EXACT - "la dernière
# combinaison confirmée fonctionner sur ce matériel" (Gemini Lake/UHD
# 600, trop vieux pour les compute-runtime récents). Testé les DEUX
# versions système en direct, une par une : 22.7.2+ds1 fait marcher le
# décodage mais fait retomber OpenVINO sur CPU ; 22.3.0 restaure
# OpenVINO mais recasse le décodage.
#
# 21.10, Sid ("Y avait pas d'autres options que ça ?" / "Tu peux
# reproduire le souci de base dans ton container éphémère ?") - RÉACTIVÉ
# le même soir avec la vraie solution : isoler les deux versions au lieu
# de choisir. `vah264dec` tourne dans un sous-processus séparé
# (gst-launch-1.0, pas le process Python principal) - lui seul reçoit
# `LD_LIBRARY_PATH` pointant vers une copie de la version récente
# (voir le Dockerfile, /opt/gmm-new) ; le process watchdog principal
# (et donc OpenVINO) ne voit jamais cette variable et continue d'utiliser
# la version système 22.3.0. Testé dans un conteneur jetable avant
# d'écrire ça ici : décodage matériel (~8.8 img/s, confirmé) ET
# inférence GPU OpenVINO fonctionnent en même temps, chacun avec sa
# propre version de GmmLib. Voir KITTYHACK_ROADMAP.md pour le détail
# complet de l'exploration (les deux tentatives, celle qui a échoué et
# celle-ci).
#
# Toujours un filet de sécurité : repli sur le chemin logiciel existant
# (cv2.VideoCapture sur self.rtsp_url) si le décodeur matériel ne
# démarre pas ou ne produit rien dans HW_DECODE_STARTUP_TIMEOUT_S -
# jamais bloqué sans caméra fonctionnelle.
_GST_PRIVATE_GMM_DIR = "/opt/gmm-new"
HW_DECODE_DEVICE = "/dev/dri/renderD128"
# 21.10, Sid ("ça défile verticalement" - capture réelle montrant l'image
# qui glisse progressivement d'une frame à l'autre) : la vraie résolution
# négociée par vah264dec/videoconvert est 640x480, pas 640x360 comme
# supposé au départ (je m'étais basé sur un commentaire décrivant
# l'ancienne résolution du chemin LOGICIEL, jamais vérifié pour le
# matériel) - confirmé via `gst-launch -v` (caps réelles sur le pad src
# de videoconvert0). Chaque frame lue était donc plus grande que prévu,
# les octets en trop contaminant le début de la frame suivante -
# glissement progressif, pas une corruption aléatoire.
HW_DECODE_WIDTH = 640
HW_DECODE_HEIGHT = 480
# 21.10, Sid ("tourne encore verticalement ?") : les frames fraîches sont
# en fait nettes (le bug de glissement ci-dessus est bien réglé) mais le
# retard par rapport au temps réel grossit sans fin - confirmé via
# `gst-launch -v` : la caméra envoie réellement du 15 img/s
# (framerate=15/1 dans les caps négociées), alors que `videoconvert`
# (conversion YUV→BGR, LOGICIELLE - aucun équivalent GPU disponible sur
# ce système, `vapostproc`/`vaapipostproc` absents des deux, vérifié via
# gst-inspect-1.0) sature un coeur CPU à 100% en continu et ne peut
# suivre qu'à ~8.8 img/s. Pas un bug de code, un plafond structurel.
# TENTATIVE le même soir : sortir du NV12 brut (vah264dec le produit
# nativement) et faire la conversion NV12→BGR côté Python via
# cv2.cvtColor au lieu de videoconvert - ABANDONNÉE et REVERTÉE : la
# surface VAAPI a un stride/padding matériel caché (mesuré via
# `multipartmux` + Content-Length réel : 471040 octets/frame, pas les
# 460800 attendus pour un NV12 640x480 propre - le rapport Y/UV n'est
# même pas un multiple entier, donc pas déductible par calcul simple).
# `videoconvert` ne fait pas QUE de la conversion colorimétrique, il
# dé-paddingue aussi la surface matérielle vers un buffer compact - sans
# lui, la lecture par comptage d'octets fixe se désynchronise une fois
# pour toutes dès la première frame (bande de couleurs parasites en haut
# de l'image, contenu réel décalé en dessous). Vrai correctif pour le
# débit : lire via les bindings GStreamer Python (gi/PyGObject +
# appsink), qui donnent des buffers déjà correctement bornés/dépaddés
# sans compter d'octets à la main - PAS installé dans l'image actuelle
# (`ModuleNotFoundError: No module named 'gi'`), nécessite un rebuild
# Docker. Reste à faire dans une session dédiée, pas un patch rapide.
HW_DECODE_FRAME_BYTES = HW_DECODE_WIDTH * HW_DECODE_HEIGHT * 3
# 21.10: relevé de 8.0 à 20.0 - reproduit en isolé (hors process réel,
# rien d'autre sur le GPU) : démarrage propre, 8.5 img/s. Dans le process
# watchdog réel, le même appel échouait systématiquement dans les 8s -
# seule différence identifiée : l'inférence OpenVINO de chatière tourne
# déjà activement sur le même GPU à ce moment-là (contrairement au test
# isolé), ce qui peut ralentir la négociation initiale VAAPI au-delà de
# 8s sans empêcher le flux une fois établi. Pas encore reconfirmé en vrai
# déploiement avec cette valeur - si ça échoue encore, le filet de
# sécurité (repli logiciel) absorbe quand même, aucun risque.
HW_DECODE_STARTUP_TIMEOUT_S = 20.0
# gst-launch-1.0 a besoin d'un XDG_RUNTIME_DIR valide pour vah264dec
# (sinon "XDG_RUNTIME_DIR is invalid or not set" au démarrage) - /tmp
# est déjà monté noexec sur certains hôtes, mais ce dossier n'a besoin
# que d'être accessible en écriture, pas exécutable.
_GST_XDG_RUNTIME_DIR = "/tmp/xdg-runtime"


def _start_hw_decoder(rtsp_url: str) -> subprocess.Popen | None:
    """Starts a persistent GStreamer process that hardware-decodes (VAAPI,
    via vah264dec) the given RTSP stream and writes raw NV12 frames of
    HW_DECODE_WIDTH x HW_DECODE_HEIGHT to stdout, back to back, one after
    another - read (and converted to BGR) one at a time with
    _read_hw_frame(). Returns None if gst-launch-1.0 itself could not even
    be started (binary missing, bad args) - a stream that opens but fails
    later is instead caught by _read_hw_frame() returning None."""
    cmd = [
        "gst-launch-1.0", "-q",
        "rtspsrc", f"location={rtsp_url}", "latency=200", "protocols=tcp",
        "!", "queue",
        "!", "rtph264depay",
        "!", "h264parse",
        "!", "queue",
        "!", "vah264dec",
        "!", "queue",
        "!", "videoconvert",
        "!", f"video/x-raw,format=BGR,width={HW_DECODE_WIDTH},height={HW_DECODE_HEIGHT}",
        "!", "queue",
        "!", "fdsink", "fd=1", "sync=false",
    ]
    try:
        os.makedirs(_GST_XDG_RUNTIME_DIR, exist_ok=True)
        os.chmod(_GST_XDG_RUNTIME_DIR, 0o700)
        env = os.environ.copy()
        env["XDG_RUNTIME_DIR"] = _GST_XDG_RUNTIME_DIR
        # Isolation GmmLib - voir le commentaire du module ci-dessus.
        # Seul CE sous-processus voit la version récente ; le process
        # Python principal (OpenVINO) garde la version système 22.3.0.
        env["LD_LIBRARY_PATH"] = _GST_PRIVATE_GMM_DIR + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        # 21.10, Sid (retard résiduel qui continuait de grossir lentement
        # même après l'ajout du drain ci-dessous) : bufsize non nul créait
        # un BufferedReader Python qui pré-charge des données dans son
        # propre tampon userspace dès qu'il en a l'occasion - invisible à
        # select() (qui ne voit que l'état du pipe au niveau noyau). Le
        # drain basé sur select() ratait donc systématiquement tout ce qui
        # était déjà pré-chargé côté Python, laissant une petite part de
        # retard s'accumuler en continu. bufsize=0 (non bufferisé) : chaque
        # read() correspond à un seul appel système, donc select() reflète
        # fidèlement ce qu'il reste réellement à lire - _read_one_hw_frame
        # gère déjà les lectures partielles (boucle while + select par
        # morceau), donc rien d'autre à changer.
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0, env=env)
    except Exception as e:
        log.warning("Could not start hardware decoder: %s", e)
        return None


def _read_one_hw_frame(proc: subprocess.Popen, timeout_s: float) -> np.ndarray | None:
    """Reads exactly one BGR24 frame from the decoder's stdout pipe, no
    draining. Returns None on EOF/short read (process died or stream ended)
    OR if no data arrives within timeout_s - a plain blocking read() here
    could hang the whole camera thread forever if ffmpeg wedges without
    closing the pipe, which select() with a timeout avoids (11.10, Sid: "il
    faut bien evidemment que toutes ces operations ne nuisent jamais a la
    boucle de detection et au pilotage des aimants" - this runs in the
    separate watchdog container, never the magnet-control one, but a hung
    thread is bad practice regardless of which process it's in)."""
    buf = bytearray()
    deadline = time.monotonic() + timeout_s
    try:
        while len(buf) < HW_DECODE_FRAME_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
            if not ready:
                return None
            chunk = proc.stdout.read(HW_DECODE_FRAME_BYTES - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
    except Exception:
        return None
    return np.frombuffer(bytes(buf), dtype=np.uint8).reshape((HW_DECODE_HEIGHT, HW_DECODE_WIDTH, 3))


# 21.10, Sid ("Toujours en décalage d'heure avec l'autre reolink" - repère
# live confirmé : l'horodatage Reolink incrusté dans la frame "chatiere"
# accusait un retard croissant, +30s de retard pour 60s de temps réel écoulé
# entre deux prélèvements) : même classe de bug que celui déjà corrigé le
# 13.10 pour le chemin logiciel (voir _drain_stale_frames ci-dessus) - mais
# jamais reporté ici. gst-launch-1.0 continue de décoder et d'écrire des
# frames en arrière-plan même pendant que cette boucle est bloquée ailleurs
# (poll HTTP Reolink, inférence YOLO en fenêtre boost) ; _read_one_hw_frame
# seul renvoie alors la frame la plus ANCIENNE encore dans le pipe/les
# buffers GStreamer (FIFO), pas la plus récente. Purement en lecture/copie
# mémoire ici (déjà décodé côté sous-processus), donc pas de coût CPU de
# décodage supplémentaire à vider le retard - contrairement à grab() côté
# cv2 qui esquive le décodage, ici chaque frame vidée a déjà été payée par
# gst-launch, on ne fait que la jeter.
HW_DECODE_MAX_DRAIN = 30


def _read_hw_frame(proc: subprocess.Popen, timeout_s: float = 5.0) -> np.ndarray | None:
    """Reads one BGR24 frame, then drains any extra frames already sitting
    in the pipe so the frame actually returned is the freshest one
    available, not the oldest queued one. See HW_DECODE_MAX_DRAIN's comment
    above for why this is needed."""
    frame = _read_one_hw_frame(proc, timeout_s)
    if frame is None:
        return None
    for _ in range(HW_DECODE_MAX_DRAIN):
        ready, _, _ = select.select([proc.stdout], [], [], 0)
        if not ready:
            break
        newer = _read_one_hw_frame(proc, timeout_s=0.5)
        if newer is None:
            break
        frame = newer
    return frame


def _stop_hw_decoder(proc: subprocess.Popen | None) -> None:
    """Terminates the decoder process cleanly - must be called on every exit
    path (reconnect, camera thread stop) or ffmpeg processes pile up as
    zombies, one per reconnect, slowly starving the container."""
    if proc is None:
        return
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass


class CameraWatcher(threading.Thread):
    """One thread per camera: keeps a VideoCapture alive, polls for prey.

    08.10, Sid ("coupe déjà le modèle pour entrée, jardin japonais et
    terrasse" / "désactive le blocage via watchdog"): detector is now
    optional - None for the 3 peripheral cameras (snapshot-only, no local
    inference at all, see main() below) since they see cats from too far to
    detect reliably and were costing most of the ~376% combined CPU already
    documented above. Separately, the lockdown trigger (on_prey_detected)
    is never wired up anymore for ANY camera, including chatiere - the
    watchdog's own independent lock+email was a second, less reliable layer
    on top of Kittyhack's own already-trusted prey detection on its primary
    camera; chatiere's local model keeps running here only for its other
    use (TRAINING_SAMPLES_DIR capture), not to trigger a lockdown anymore.
    """

    def __init__(self, name: str, rtsp_url: str, detector: MouseDetector | None, on_prey_detected, mqtt_status: "MqttStatus", api_token: str):
        super().__init__(daemon=True, name=f"cam-{name}")
        self.cam_name = name
        self.rtsp_url = rtsp_url
        self.detector = detector
        self.on_prey_detected = on_prey_detected
        self.mqtt_status = mqtt_status
        # 08.10, Sid: used to call POST /camera/wake when this camera's own
        # Reolink AI sees an animal - see _wake_kittyflap_camera() above.
        self.api_token = api_token
        self._stop = threading.Event()
        # Instance attribute (not a run()-local) so a stream reconnect - common
        # on a weak-signal camera - doesn't reset the sampling cadence.
        self._last_sample_at = 0.0
        self._base_poll_interval = CAMERA_POLL_INTERVAL_S.get(name, DEFAULT_POLL_INTERVAL_S)
        # 21.10, Sid ("l'événement réel enregistré dans reolink commence à
        # 8h20:14 - j'ai donc l'entrée de pookie... alors que paws review ne
        # commence qu'à 08:20:20"): ce check (_reolink_dog_cat_detected)
        # tournait au même rythme que le snapshot/inférence (_poll_interval,
        # 8s à l'idle pour chatiere) - un passage vu par Reolink avant que le
        # PIR ne se déclenche pouvait donc rester invisible jusqu'à 8s,
        # coupant pile le début de l'événement (l'entrée avec la proie).
        # C'est un simple GET HTTP sur l'API de la caméra, sans decode vidéo
        # (voir _reolink_dog_cat_detected) - son propre timer, découplé de
        # _poll_interval, pour tourner bien plus vite sans alourdir
        # l'inférence locale.
        self._last_reolink_poll = 0.0

    def stop(self):
        self._stop.set()

    def _poll_interval(self) -> float:
        if self.cam_name == "chatiere" and time.monotonic() < _chatiere_boost_until[0]:
            return CHATIERE_BOOST_INTERVAL_S
        return self._base_poll_interval

    def run(self):
        # 08.10, Sid ("on peut décoder que quand y a un mouvement reolink?"):
        # a camera with its own Reolink AI and no local model no longer
        # needs a persistent RTSP connection decoding every frame nonstop
        # just to maybe save one occasionally - see _run_peripheral().
        # Chatiere also gained a Reolink AI today (camera swap - see
        # REOLINK_HTTP above) but still needs its own local model
        # (self.detector, from CAMERAS_WITH_DETECTION) for real prey
        # detection up close, and low latency on a real PIR trigger - keyed
        # on self.detector rather than REOLINK_HTTP membership alone so it
        # keeps the original always-connected approach, just with an added
        # Reolink poll as a third trigger source (see _run_continuous()).
        if self.cam_name in REOLINK_HTTP and self.detector is None:
            self._run_peripheral()
        else:
            self._run_continuous()

    def _run_peripheral(self):
        """No persistent VideoCapture at all - poll Reolink's own AI state
        (cheap, no video decode involved) once per _base_poll_interval, and
        only open+grab+close the RTSP stream for that one tick's worth of
        work (live-preview snapshot always, training sample + chatiere
        trigger only when Reolink actually saw an animal)."""
        reolink_cfg = REOLINK_HTTP[self.cam_name]
        self.mqtt_status.publish_camera_status(self.cam_name, "online")
        while not self._stop.is_set():
            detected = _reolink_dog_cat_detected(reolink_cfg)
            frame = _grab_single_frame(self.rtsp_url)
            if frame is None:
                log.warning("[%s] Could not grab a frame this tick.", self.cam_name)
            elif not _is_frame_usable(frame):
                log.warning("[%s] Corrupted/blank frame (decode error concealment), skipping.", self.cam_name)
            else:
                _save_frame_snapshot(self.cam_name, frame)
                now = time.monotonic()
                if detected and now - self._last_sample_at >= TRAINING_SAMPLE_COOLDOWN_S:
                    self._last_sample_at = now
                    _save_training_sample(self.cam_name, frame)
                    # 08.10, Sid ("comme ça elle est prête en cas de
                    # défaillance du PIR"): backup trigger for chatiere's
                    # own inference - see _run_continuous()'s gate.
                    _extend_chatiere_trigger(CHATIERE_REOLINK_TRIGGER_DURATION_S)
                    # 08.10, Sid ("on couvre tous les cas et on utilise
                    # toutes les ressources selon leur capacité"): also
                    # give the internal flap camera's own model the same
                    # backup wake-up - a cat spotted on a peripheral camera
                    # is heading towards the flap, same reasoning as the
                    # chatiere trigger just above.
                    _wake_kittyflap_camera(self.api_token, CHATIERE_REOLINK_TRIGGER_DURATION_S)
            self._stop.wait(self._base_poll_interval)

    def _run_continuous(self):
        # 21.10, Sid ("Y avait pas d'autres options que ça ?") - RÉACTIVÉ
        # avec l'isolation LD_LIBRARY_PATH (voir le commentaire complet
        # au-dessus de _start_hw_decoder pour le détail du conflit GmmLib/
        # OpenVINO et de sa résolution). Toute autre caméra, ou chatière
        # si le matériel ne démarre pas proprement, utilise le chemin
        # logiciel cv2.VideoCapture existant, inchangé - jamais bloqué
        # sans caméra fonctionnelle.
        use_hw = self.cam_name == "chatiere"
        while not self._stop.is_set():
            hw_proc = None
            cap = None
            if use_hw:
                hw_proc = _start_hw_decoder(self.rtsp_url)
                if hw_proc is not None:
                    deadline = time.monotonic() + HW_DECODE_STARTUP_TIMEOUT_S
                    hw_ok = False
                    while time.monotonic() < deadline:
                        if hw_proc.poll() is not None:
                            break
                        if _read_hw_frame(hw_proc, timeout_s=2.0) is not None:
                            hw_ok = True
                            break
                    if not hw_ok:
                        log.warning(
                            "[%s] Hardware decoder failed to produce a frame within %ss - "
                            "falling back to software decode.", self.cam_name, HW_DECODE_STARTUP_TIMEOUT_S
                        )
                        _stop_hw_decoder(hw_proc)
                        hw_proc = None
            if hw_proc is None:
                cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
                # 13.10, Sid (root-caused via live evidence - a Reolink clip
                # saved for a real event showed a frozen scene ~18s before
                # the cat actually entered, while the Kittyflap panel
                # tracked real time correctly): best-effort, not sufficient
                # alone - see _drain_stale_frames below for why.
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if not cap.isOpened():
                    log.warning("[%s] Could not open stream, retrying in %ss", self.cam_name, RECONNECT_DELAY_S)
                    self.mqtt_status.publish_camera_status(self.cam_name, "offline")
                    time.sleep(RECONNECT_DELAY_S)
                    continue
            log.info("[%s] Stream opened%s.", self.cam_name, " (hardware decode)" if hw_proc is not None else "")
            self.mqtt_status.publish_camera_status(self.cam_name, "online")
            last_poll = 0.0
            try:
                while not self._stop.is_set():
                    if hw_proc is not None:
                        frame = _read_hw_frame(hw_proc)
                        ok = frame is not None
                    else:
                        _drain_stale_frames(cap)
                        ok, frame = cap.read()
                    if not ok or frame is None:
                        log.warning("[%s] Lost frame, reconnecting.", self.cam_name)
                        self.mqtt_status.publish_camera_status(self.cam_name, "offline")
                        break
                    # 11.10 (round 2): captured right here, before the
                    # Reolink AI poll + YOLO inference below that can each
                    # take real time under load - see _save_event_frame's
                    # own comment for why this matters.
                    frame_captured_wall = time.time()
                    now = time.monotonic()
                    # 21.10, Sid ("l'événement réel... commence à 8h20:14 -
                    # paws review ne commence qu'à 08:20:20"): ce poll
                    # tournait auparavant à l'intérieur du gate
                    # _poll_interval() ci-dessous (8s à l'idle) - un passage
                    # vu par Reolink avant le PIR restait invisible jusqu'à
                    # 8s, coupant le tout début de l'événement. Pur GET HTTP
                    # sur l'API caméra (pas de decode vidéo), sorti de ce
                    # gate avec son propre timer (CHATIERE_REOLINK_POLL_
                    # INTERVAL_S, 2s) pour ne plus dépendre de la cadence
                    # snapshot/inférence.
                    if self.cam_name in REOLINK_HTTP and now - self._last_reolink_poll >= CHATIERE_REOLINK_POLL_INTERVAL_S:
                        self._last_reolink_poll = now
                        if _reolink_dog_cat_detected(REOLINK_HTTP[self.cam_name]):
                            _extend_chatiere_trigger(CHATIERE_REOLINK_TRIGGER_DURATION_S)
                            # 08.10, Sid ("potentiellement cette de camera KF
                            # pour les cas ou PIR a un angle mort"): this
                            # camera's own Reolink AI backs up the internal
                            # flap camera too, not just its own local model.
                            _wake_kittyflap_camera(self.api_token, CHATIERE_REOLINK_TRIGGER_DURATION_S)
                    if now - last_poll < self._poll_interval():
                        continue
                    last_poll = now
                    if not _is_frame_usable(frame):
                        log.warning("[%s] Corrupted/blank frame (decode error concealment), skipping.", self.cam_name)
                        continue
                    _save_frame_snapshot(self.cam_name, frame)
                    # 08.10, Sid ("elle s'active au PIR..."): chatiere's own
                    # model only actually runs within a trigger window (PIR
                    # or, as a backup, a peripheral Reolink detection) - CPU
                    # cost stays near zero the rest of the time, not just
                    # slower (CAMERA_POLL_INTERVAL_S's 8s idle rate above
                    # still governs the snapshot cadence, unaffected).
                    if time.monotonic() >= _chatiere_boost_until[0]:
                        continue
                    # 09.10: chatiere_int has no REOLINK_HTTP entry (older
                    # camera model, RTSP/ONVIF only - see REOLINK_HTTP's own
                    # comment above) and no local model either
                    # (CAMERAS_WITH_DETECTION), so self.detector is None here
                    # - it was the first camera to hit this combination, and
                    # the unconditional .run() below was looping on
                    # "'NoneType' object has no attribute 'run'" every
                    # single frame, burning real CPU on the watchdog
                    # container for no purpose.
                    if self.detector is None:
                        continue
                    try:
                        mouse_probability, any_detection, detections = self.detector.run(frame)
                    except Exception as e:
                        log.error("[%s] Inference error: %s", self.cam_name, e)
                        continue
                    if any_detection and now - self._last_sample_at >= TRAINING_SAMPLE_COOLDOWN_S:
                        self._last_sample_at = now
                        _save_training_sample(self.cam_name, frame, detections)
                    # 10.10, Sid ("deux vidéos côte à côte"): we're already
                    # past the `_chatiere_boost_until` gate above, so every
                    # poll reaching here is within a real trigger window -
                    # save it unconditionally (not gated on any_detection
                    # like the sparse training sample above) so the event
                    # viewer gets a dense, continuous sequence to compare
                    # against the flap's own camera, not just the odd hit.
                    _save_event_frame(self.cam_name, frame, detections, captured_at=frame_captured_wall)
                    # Lockdown trigger disabled - see class docstring above.
                    if mouse_probability >= WATCHDOG_MOUSE_THRESHOLD:
                        log.warning("[%s] Prey suspected (%.0f%%) - lockdown disabled, no action taken.", self.cam_name, mouse_probability)
            finally:
                if hw_proc is not None:
                    _stop_hw_decoder(hw_proc)
                if cap is not None:
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


#  08.10, Sid ("coupe déjà le modèle pour entrée, jardin japonais et
# terrasse"): only chatiere runs local detection now - it's the only one
# close enough to the action to detect reliably, and the other 3 were most
# of the ~376% combined CPU already documented above for no real benefit
# ("les autres n'apportent rien"). They keep their periodic frame snapshot
# (used by Parcours/Presence) - see CameraWatcher.run()'s detector-is-None
# branch - just no model loaded, no inference, no training samples.
CAMERAS_WITH_DETECTION = {"chatiere"}


def main():
    load_config()
    lock_duration = float(CONFIG.get("LOCK_DURATION_AFTER_PREY_DETECTION", 180) or 180)
    log.info("Starting multi-camera watchdog (%d cameras, %d with local detection, threshold %.0f%%)",
              len(CAMERAS), len(CAMERAS_WITH_DETECTION), WATCHDOG_MOUSE_THRESHOLD)

    token = _get_or_create_token()
    secrets = _get_or_create_secrets()
    mqtt_status = MqttStatus(secrets["mqtt"])
    # 08.10, Sid ("désactive le blocage via watchdog"): LockdownController
    # kept (not deleted) in case this is ever wanted back, but its .trigger
    # is no longer wired to any camera - see CameraWatcher.run().
    controller = LockdownController(token, lock_duration, mqtt_status, secrets["smtp"])

    # Each thread gets its own Interpreter instance - a TFLite interpreter is
    # not safe to call concurrently from multiple threads.
    watchers = [
        CameraWatcher(
            name, url,
            MouseDetector() if name in CAMERAS_WITH_DETECTION else None,
            controller.trigger, mqtt_status, token,
        )
        for name, url in CAMERAS.items()
    ]
    for w in watchers:
        w.start()
    for w in watchers:
        w.join()


if __name__ == "__main__":
    main()
