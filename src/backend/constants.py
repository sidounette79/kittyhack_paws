"""Timing constants for the backend control loop."""
from src.baseconfig import CONFIG

TAG_TIMEOUT = 30.0               # after 30 seconds, a detected tag is considered invalid
RFID_READER_OFF_DELAY = 15.0     # Turn the RFID reader off 15 seconds after the last detected motion outside
OPEN_OUTSIDE_TIMEOUT = 6.0 + CONFIG['PIR_INSIDE_THRESHOLD'] # Keep the magnet to the outside open for 6 + PIR_INSIDE_THRESHOLD seconds after the last motion on the inside
MAX_UNLOCK_TIME = 60.0           # Maximum time the door is allowed to stay open
LAZY_CAT_DELAY_PIR_MOTION = 6.0  # Keep the PIR active for an additional 6 seconds after the last detected motion when using PIR-based motion detection
LAZY_CAT_DELAY_CAM_MOTION = 12.0 # Keep the PIR active for an additional 12 seconds after the last detected motion when using camera-based motion detection
FAST_EXIT_POST_CAPTURE_SECONDS = 6.0  # Extra recording after fast-lock exit crossing before finalizing the event
EVENT_COOLDOWN_SECONDS = 3.0     # After an event is finalized, ignore all new motion triggers for this long (PIR settling)
MAX_MOTION_BLOCK_SECONDS = 90.0  # Finalize a motion block after this time even if outside motion never falls cleanly
# 02.11, Sid ("Boubou dort à 1m de la chatière dehors... un chat dedans ne
# peut pas sortir car les mouvements de Boubou font que la chatière se met
# en mode entrée"): confirmed structurally - unlock_inside is re-evaluated
# every tick and stays claimed as long as a known cat keeps being detected
# outside, and "only one magnet" (confirmed necessary even with the new
# 36W/3A PSU - real crash reproduced 02.11, see CRASH_KITTYFLAP.md
# Incident #3) then blocks any competing exit the whole time. Fairness
# fix: if inside has been unlocked for entry for at least this long with
# ZERO inside PIR motion recorded since the unlock (strong evidence no cat
# is actually mid-crossing, just a lingering outside presence holding the
# claim), a genuinely pending exit request is allowed to force a lock and
# take the outside magnet instead. Deliberately generous (most real
# entries resolve in a few seconds once a cat commits) and gated on "zero
# inside motion since unlock" specifically so a cat actually walking
# through can never be interrupted mid-passage.
INSIDE_ENTRY_YIELD_FOR_EXIT_S = 10.0
# 02.11, Sid ("branche le lien reolink voit une proie et informe HK déjà"):
# was PHASE 0 (observation only) since 25.10 - see src/api.py's
# reolink_prey_signal() docstring for the original plan. Today's real
# incident (16:31, Patoune brought in prey - Kittyflap's own local model
# never flagged it at all, Reolink saw it clearly, box visible, 73%/65%
# confidence) is exactly the case that Phase 0 was collecting evidence
# for. This is PHASE 2 of that plan specifically ("prolonger la fenêtre
# de blocage post-proie existante, jamais un verrou indépendant") - a
# confident-enough Reolink signal now feeds the SAME
# backend_main.prey_detection_mono / LOCK_DURATION_AFTER_PREY_DETECTION
# gate Kittyflap's own detection already uses, never a separate lock.
# Threshold matches MOUSE_THRESHOLD's own default (70.0) - the real
# incident's first signal (73%) would have cleared it with room to
# spare, its second (65%) would not have (but didn't need to, the first
# one already opened the 300s window well before the cat actually
# crossed).
REOLINK_PREY_SIGNAL_MIN_CONFIDENCE = 70.0
# 02.11, Sid ("Est-ce que ça peut aussi déclencher si y a une répétition
# de détection même en dessous de 70 ? Si y a 3x 68% par exemple ? 1x si
# supérieur mais aussi couvrir la quantité ?") - second path alongside
# REOLINK_PREY_SIGNAL_MIN_CONFIDENCE's single strong signal: several
# weaker-but-repeated signals within a short window are also real
# evidence, not noise - a cat genuinely carrying prey tends to trigger
# more than one borderline detection across consecutive frames as it
# approaches, even if none alone clears the main threshold. Window needs
# to comfortably cover a few consecutive frames during an active trigger
# (CHATIERE_BOOST_INTERVAL_S=0.3s in watchdog.py) without being so wide
# it starts treating signals from genuinely separate, unrelated passages
# as one streak.
REOLINK_PREY_SIGNAL_REPEAT_COUNT = 3
REOLINK_PREY_SIGNAL_REPEAT_MIN_CONFIDENCE = 60.0
REOLINK_PREY_SIGNAL_REPEAT_WINDOW_S = 15.0
CAMERA_IDLE_RESUME_HOLD_S = 20.0  # PAUSE_CAMERA_WHEN_IDLE: keep the chatiere camera model running this long after the last real PIR edge
GLANCE_NOTIFICATION_COOLDOWN_S = 600.0  # 05.10, Sid: "a regarde" push notification - per-RFID cooldown, far more frequent than entry/exit so needs one
# 08.10, Sid ("forcer le systeme a ecrire... en forcant l'utilisation du vrai
# RFID... meme si le systeme se trompe"): a real chip read sometimes arrives
# a beat after a motion block already concluded without one (confirmed live
# in the logs tonight) - for an actual passage the cat is physically right
# at the reader, so a late real read is worth backfilling into the
# already-written events row. Notifications stay instant/best-effort per
# her explicit call ("la notif ne reste pas, c'est pas bien grave") - only
# the DB (Parcours/Chronologie/stats/revision) gets corrected.
RFID_BACKFILL_WINDOW_S = 15.0

# 25.10, Sid ("faut qu'on ajuste mais je sais pas comment... on teste ça"):
# ENTRY_REQUIRE_RFID_READ (24.10) correctly stops a cat merely lounging in
# the catio from opening the door by video alone, but it also blocked a
# genuine entry attempt (Nala, 41s of continuous high-confidence video ID,
# chip never read in that window). Real data from already-confirmed HK
# captures (25.10) showed two fairly distinct clusters by detection box
# height: close-up/entering shots run ~78-98%, farther/lounging ones ~37-
# 47% - this sits in the gap between them. When ENTRY_REQUIRE_RFID_READ is
# on, a video identity is now trusted anyway if its most recent contributing
# frame's box is at least this tall - "confidently identified AND currently
# close to the camera", not just "recognized from somewhere in the catio".
# A real chip read still always wins outright, unaffected by this. Not yet
# confirmed on-device - first cut from real capture data, tune after seeing
# it in practice (see [[project_kittyhack_crash_master]]-style follow-up).
VIDEO_FALLBACK_MIN_BOX_HEIGHT_PCT = 60.0

# 09.10, Sid ("un de chaque coté de la chatière, histoire de savoir qui
# gagne l'affrontement visuel... ils font ca assez souvent"): wait_for_
# outside_rising_after_exit is only ever cleared by a motion-outside RISING
# edge (loop.py), but a confrontation at the flap keeps motion_outside
# continuously at 1 - the edge never comes, so a legitimate entry decision
# right after an exit can get stuck ignored indefinitely. This safety
# timeout force-clears the flag after this many seconds even without that
# edge, so a real entry attempt is never blocked for longer than this.
WAIT_FOR_OUTSIDE_RISING_TIMEOUT_S = 10.0
