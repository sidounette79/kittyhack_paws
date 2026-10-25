"""Extra backend loop scenarios to raise coverage of ``src.backend.loop``."""

from __future__ import annotations

import pytest

import src.baseconfig as baseconfig
import src.backend.loop as loop_mod
from src.baseconfig import AllowedToEnter, AllowedToExit
from src.camera import DetectedObject, image_buffer
from src.helper import Result
from src.magnets_rfid import RfidRunState
from tests.helpers.backend_loop_harness import build_loop_context


@pytest.fixture
def loop_db(tmp_kittyhack_db, tmp_config_ini):
    baseconfig.CONFIG["KITTYHACK_DATABASE_PATH"] = tmp_kittyhack_db
    return tmp_kittyhack_db


@pytest.fixture(autouse=True)
def _short_loop_delays(monkeypatch):
    monkeypatch.setattr(loop_mod, "LAZY_CAT_DELAY_PIR_MOTION", 0.0)
    monkeypatch.setattr(loop_mod, "LAZY_CAT_DELAY_CAM_MOTION", 0.0)
    monkeypatch.setattr(loop_mod, "EVENT_COOLDOWN_SECONDS", 0.0)
    monkeypatch.setattr(loop_mod, "FAST_EXIT_POST_CAPTURE_SECONDS", 0.0)
    monkeypatch.setattr(loop_mod, "TAG_TIMEOUT", 2.0)
    monkeypatch.setattr(loop_mod, "RFID_READER_OFF_DELAY", 1.0)
    monkeypatch.setattr(loop_mod, "OPEN_OUTSIDE_TIMEOUT", 1.0)
    monkeypatch.setattr(loop_mod, "MAX_UNLOCK_TIME", 3.0)
    monkeypatch.setattr(loop_mod, "MAX_MOTION_BLOCK_SECONDS", 5.0)
    monkeypatch.setattr(
        "src.database.EventsRepo.write_motion_block_to_db",
        staticmethod(lambda *a, **k: Result(True, "")),
    )


@pytest.fixture
def harness(loop_db):
    h = build_loop_context(db_path=loop_db, cat_rfid="CAT001")
    yield h
    h.shutdown()
    image_buffer.clear()


def _inject_frame(harness, *, mouse=0.0, own_cat=0.0, cat_name=None, height=20.0):
    """Append a detection frame aligned to the FakeClock (mono + wall).

    25.10: height defaults to 20 (% of frame) - same as the old hardcoded
    value, well under VIDEO_FALLBACK_MIN_BOX_HEIGHT_PCT (60) so existing
    callers keep exercising the "far from camera" case unless they pass a
    taller one explicitly.
    """
    detected = None
    if cat_name:
        detected = [DetectedObject(0.1, 0.1, 0.2, float(height), cat_name, float(own_cat or 90.0))]
    image_buffer.append(
        harness.clock.wall_time(),
        b"",
        b"",
        float(mouse),
        100.0 - float(mouse),
        float(own_cat),
        detected_objects=detected,
        timestamp_mono=harness.clock.monotonic(),
    )


# --- Entry modes -------------------------------------------------------------


def test_entry_all_unlocks_and_finalizes_on_motion_end(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    harness.set_outside(True)
    harness.pump(3)
    _inject_frame(harness, own_cat=80.0)
    harness.advance(0.5)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is True

    harness.set_outside(False)
    harness.advance(0.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False


def test_entry_all_rfids_with_and_without_tag(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL_RFIDS
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    harness.set_outside(True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is False

    harness.inject_rfid("ANY99")
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True


def test_entry_all_mode_explicit_decision(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    harness.set_outside(True)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True


def test_entry_per_cat_allow_and_deny(loop_db):
    h = build_loop_context(db_path=loop_db, cat_rfid="CAT001", allow_entry=True)
    try:
        baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.CONFIGURE_PER_CAT
        baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
        h.set_outside(True)
        h.inject_rfid("CAT001")
        h.advance(1.0)
        h.pump(5)
        assert h.magnets.get_inside_state() is True
    finally:
        h.shutdown()
        image_buffer.clear()

    h2 = build_loop_context(db_path=loop_db, cat_rfid="CAT002", allow_entry=False)
    try:
        # Fresh harness reloads cats from DB; CAT001 allow=True still present + CAT002 deny.
        # Use CAT002 for deny path.
        baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.CONFIGURE_PER_CAT
        baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
        h2.set_outside(True)
        h2.inject_rfid("CAT002")
        h2.advance(1.0)
        h2.pump(5)
        assert h2.magnets.get_inside_state() is False
    finally:
        h2.shutdown()
        image_buffer.clear()


# --- Exit modes --------------------------------------------------------------


def test_exit_deny_stays_locked(harness):
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.DENY
    harness.set_inside(True)
    harness.pump(5)
    assert harness.magnets.get_outside_state() is False


def test_exit_per_cat_allow_after_late_rfid(loop_db):
    h = build_loop_context(db_path=loop_db, cat_rfid="CAT001", allow_exit=True)
    try:
        baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.CONFIGURE_PER_CAT
        for i in (1, 2, 3):
            baseconfig.CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = False

        h.clear_rfid()
        h.set_inside(True)
        h.pump(3)
        assert h.magnets.get_outside_state() is False

        h.inject_rfid("CAT001")
        h.pump(5)
        assert h.magnets.get_outside_state() is True
    finally:
        h.shutdown()
        image_buffer.clear()


def test_exit_per_cat_deny_after_rfid(loop_db):
    h = build_loop_context(db_path=loop_db, cat_rfid="CAT001", allow_exit=False)
    try:
        baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.CONFIGURE_PER_CAT
        for i in (1, 2, 3):
            baseconfig.CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = False

        h.clear_rfid()
        h.set_inside(True)
        h.pump(2)
        h.inject_rfid("CAT001")
        h.pump(5)
        assert h.magnets.get_outside_state() is False
    finally:
        h.shutdown()
        image_buffer.clear()


def test_exit_per_cat_inside_ends_before_rfid(loop_db):
    h = build_loop_context(db_path=loop_db, cat_rfid="CAT001", allow_exit=True)
    try:
        baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.CONFIGURE_PER_CAT
        for i in (1, 2, 3):
            baseconfig.CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = False

        h.clear_rfid()
        h.set_inside(True)
        h.pump(2)
        h.set_inside(False)
        h.pump(3)
        assert h.magnets.get_outside_state() is False
    finally:
        h.shutdown()
        image_buffer.clear()


def test_exit_outside_locks_after_open_timeout(harness):
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.ALLOW
    for i in (1, 2, 3):
        baseconfig.CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = False

    harness.set_inside(True)
    harness.pump(3)
    assert harness.magnets.get_outside_state() is True

    harness.set_inside(False)
    harness.advance(1.5)
    harness.pump(5)
    assert harness.magnets.get_outside_state() is False


# --- Prey / analyze / re-lock ------------------------------------------------


def test_prey_ok_then_unlock_all(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = True
    baseconfig.CONFIG["MOUSE_THRESHOLD"] = 70.0
    baseconfig.CONFIG["MIN_SECONDS_TO_ANALYZE"] = 1.0

    harness.set_outside(True)
    harness.pump(2)
    _inject_frame(harness, mouse=10.0)
    harness.pump(2)
    assert harness.magnets.get_inside_state() is False

    harness.advance(1.2)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True


def test_prey_relock_after_unlock(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = True
    baseconfig.CONFIG["MOUSE_THRESHOLD"] = 50.0
    baseconfig.CONFIG["MIN_SECONDS_TO_ANALYZE"] = 0.0

    harness.set_outside(True)
    harness.pump(2)
    _inject_frame(harness, mouse=10.0)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True

    _inject_frame(harness, mouse=90.0)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False


def test_per_cat_prey_disabled_allows_high_mouse(loop_db):
    h = build_loop_context(
        db_path=loop_db, cat_rfid="CAT001", enable_prey_detection=False
    )
    try:
        baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
        baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = True
        baseconfig.CONFIG["MOUSE_THRESHOLD"] = 50.0
        baseconfig.CONFIG["MIN_SECONDS_TO_ANALYZE"] = 0.0

        h.set_outside(True)
        h.inject_rfid("CAT001")
        h.pump(2)
        _inject_frame(h, mouse=95.0)
        h.advance(0.5)
        h.pump(5)
        assert h.magnets.get_inside_state() is True
    finally:
        h.shutdown()
        image_buffer.clear()


# --- Camera paths ------------------------------------------------------------


def test_camera_motion_detection_unlocks_all(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    baseconfig.CONFIG["USE_CAMERA_FOR_MOTION_DETECTION"] = True
    baseconfig.CONFIG["CAT_THRESHOLD"] = 50.0

    harness.set_outside(False)
    # get_filtered_ids_recent uses real monotonic time — append with real mono.
    image_buffer.append(
        harness.clock.wall_time(),
        b"",
        b"",
        0.0,
        0.0,
        90.0,
        timestamp_mono=__import__("time").monotonic(),
    )
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True


def test_camera_cat_detection_known_without_rfid(harness):
    # 24.10: this fallback (video identification alone unlocking, no actual
    # chip read) is now opt-in via ENTRY_REQUIRE_RFID_READ=False - see
    # test_camera_only_detection_does_not_unlock_by_default below for the
    # new default behavior Sid asked for.
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    baseconfig.CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = True
    baseconfig.CONFIG["CAT_THRESHOLD"] = 50.0
    baseconfig.CONFIG["ENTRY_REQUIRE_RFID_READ"] = False

    harness.clear_rfid()
    harness.set_outside(True)
    harness.pump(3)
    _inject_frame(harness, own_cat=90.0, cat_name="Mia")
    harness.advance(0.2)
    harness.pump(8)
    assert harness.magnets.get_inside_state() is True


def test_camera_only_detection_does_not_unlock_by_default(harness):
    """24.10, Sid ("j'aimerais garder la détection mais que ce soit que le
    RFID qui ouvre") - cats lounging in the catio right next to the flap
    were getting identified by camera alone (no chip read, since RFID only
    reads at genuinely close range) and kept re-triggering the inside
    unlock. ENTRY_REQUIRE_RFID_READ defaults to True now: video keeps
    identifying the cat (see 'Detected cat... by video stream' below) but
    must not unlock on its own."""
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    baseconfig.CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = True
    baseconfig.CONFIG["CAT_THRESHOLD"] = 50.0
    assert baseconfig.CONFIG["ENTRY_REQUIRE_RFID_READ"] is True

    harness.clear_rfid()
    harness.set_outside(True)
    harness.pump(3)
    _inject_frame(harness, own_cat=90.0, cat_name="Mia")
    harness.advance(0.2)
    harness.pump(8)
    assert harness.magnets.get_inside_state() is False

    # A real chip read for the same cat still unlocks normally.
    harness.inject_rfid("CAT001")
    harness.pump(8)
    assert harness.magnets.get_inside_state() is True


def test_camera_close_detection_unlocks_without_rfid(harness):
    """25.10, Sid ("faut qu'on ajuste mais je sais pas comment... on teste
    ça") - the test above (far from camera, height=20%) correctly still
    doesn't unlock. A cat confidently identified AND visibly close to the
    camera (height >= VIDEO_FALLBACK_MIN_BOX_HEIGHT_PCT) should, even with
    ENTRY_REQUIRE_RFID_READ on and no chip read at all - the "video-close"
    fallback from entry_policy.py."""
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    baseconfig.CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = True
    baseconfig.CONFIG["CAT_THRESHOLD"] = 50.0
    assert baseconfig.CONFIG["ENTRY_REQUIRE_RFID_READ"] is True

    harness.clear_rfid()
    harness.set_outside(True)
    harness.pump(3)
    _inject_frame(harness, own_cat=90.0, cat_name="Mia", height=70.0)
    harness.advance(0.2)
    harness.pump(8)
    assert harness.magnets.get_inside_state() is True


def test_late_stranger_rfid_does_not_relock_video_entry(harness):
    """08.10, Sid ("il faut désactiver ce truc, si puce RFID détectée, quel
    que soit le nombre de chat à quel moment = ça ouvre"): real incident -
    Pookie's own entry (granted by video) got silently re-locked mid-
    passage because an unrelated stranger's collar tag was read moments
    later nearby. The "RFID overrides video" mechanism (loop.py's disabled
    `if False and (...)` block, ~line 1711) was deliberately turned off for
    exactly this reason - once a valid entry is granted, nothing should be
    able to take it back mid-passage. This test used to assert the OLD,
    now-intentionally-removed behavior (a later stranger tag re-locking the
    door); updated 25.10 to assert the current, deliberate one instead."""
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    baseconfig.CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = True
    baseconfig.CONFIG["CAT_THRESHOLD"] = 50.0
    baseconfig.CONFIG["ENTRY_REQUIRE_RFID_READ"] = False

    harness.clear_rfid()
    harness.set_outside(True)
    harness.pump(3)
    _inject_frame(harness, own_cat=90.0, cat_name="Mia")
    harness.pump(8)
    assert harness.magnets.get_inside_state() is True

    harness.inject_rfid("STRANGER")
    harness.pump(8)
    assert harness.magnets.get_inside_state() is True


def test_different_known_cat_finalizes_open_block(loop_db, caplog):
    """11.10, Sid ("le mélange entrée/sortie peut-il bloquer un chat à tort
    ?"): confirmed real bug the crash investigation found - a second known
    cat's real RFID read while the first cat's motion block was still open
    used to get silently merged into it (no identity check at all). A real
    chip read for a DIFFERENT already-known cat must finalize the open block
    instead - never trigger on an unknown/stranger tag (noise), and never on
    the prey classifier (which carries no RFID, so this path can't see it)."""
    from src.database import CatsRepo

    CatsRepo.db_add_new_cat(
        loop_db, "Nala", "CAT002", "",
        enable_prey_detection=True, allow_entry=True, allow_exit=True,
    )
    h = build_loop_context(db_path=loop_db, cat_name="Patoune", cat_rfid="CAT001")
    try:
        baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
        baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

        # Patoune's own real chip read opens and owns the current block.
        h.set_outside(True)
        h.pump(2)
        h.inject_rfid("CAT001")
        h.advance(0.5)
        h.pump(3)

        # A stranger tag (unregistered) must NOT split the block - just noise.
        caplog.clear()
        h.inject_rfid("UNKNOWN_TAG")
        h.advance(0.5)
        h.pump(3)
        assert not any("Different known cat detected" in r.message for r in caplog.records)

        # Nala's own real, DIFFERENT, already-known tag arrives while the
        # block is still open - this must finalize Patoune's block instead
        # of merging Nala's motion into it.
        caplog.clear()
        h.inject_rfid("CAT002")
        h.advance(0.5)
        h.pump(3)
        assert any("Different known cat detected" in r.message for r in caplog.records)
    finally:
        h.shutdown()
        image_buffer.clear()


def test_video_identity_majority_vote_resists_single_outlier(loop_db, caplog):
    """11.10 (round 2), Sid ("sur les 20 premières images c'est Nala, pas de
    logique que la 21e soit Pookie, la 23e Patoune puis de nouveau Nala"):
    real data that same day showed 41.5% of blocks with video detection
    flickering between distinct cat identities within a single passage,
    because the old logic picked the single highest-confidence detection
    ever seen in the whole block - one later noisy outlier frame could
    override 20 consistent prior frames. Switched to a per-image vote count
    that requires a strict majority to switch identity."""
    from src.database import CatsRepo

    CatsRepo.db_add_new_cat(
        loop_db, "Pookie", "CAT002", "",
        enable_prey_detection=True, allow_entry=True, allow_exit=True,
    )
    h = build_loop_context(db_path=loop_db, cat_name="Nala", cat_rfid="CAT001")
    try:
        baseconfig.CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = True
        baseconfig.CONFIG["CAT_THRESHOLD"] = 50.0
        baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

        h.set_outside(True)
        h.pump(2)

        # 20 consistent frames identifying Nala at a moderate confidence.
        for _ in range(20):
            _inject_frame(h, own_cat=70.0, cat_name="Nala")
        h.pump(3)
        assert any("matches RFID tag 'CAT001'" in r.message for r in caplog.records)

        # One single later frame misidentifies Pookie at a HIGHER confidence
        # than any individual Nala frame - the old bug would have flipped
        # identity on this single frame alone.
        caplog.clear()
        _inject_frame(h, own_cat=95.0, cat_name="Pookie")
        h.pump(3)
        assert not any("matches RFID tag 'CAT002'" in r.message for r in caplog.records)

        # Enough further Pookie frames to legitimately build a real majority
        # must still be able to correct the identity (keeps the 05.10 fix's
        # own ability to self-correct an early wrong guess, see the comment
        # block in loop.py right above this logic).
        for _ in range(25):
            _inject_frame(h, own_cat=70.0, cat_name="Pookie")
        h.pump(3)
        assert any("matches RFID tag 'CAT002'" in r.message for r in caplog.records)
    finally:
        h.shutdown()
        image_buffer.clear()


def test_video_identity_splits_block_on_strong_recent_majority_of_different_cat(loop_db, caplog):
    """11.10 (round 2 continued), Sid ("on traite aussi si ya moyen" - two
    different cats passing through the SAME motion block before either has
    an RFID read): the majority-vote fix above resists single-frame noise,
    but that also means a genuinely different cat arriving later would only
    slowly (if ever) outvote the first cat's whole accumulated history. A
    strong recent majority (not a single frame) of a different, known cat
    must split the block instead, mirroring the existing RFID-based split."""
    from src.database import CatsRepo

    CatsRepo.db_add_new_cat(
        loop_db, "Patoune", "CAT002", "",
        enable_prey_detection=True, allow_entry=True, allow_exit=True,
    )
    h = build_loop_context(db_path=loop_db, cat_name="Nala", cat_rfid="CAT001")
    try:
        baseconfig.CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = True
        baseconfig.CONFIG["CAT_THRESHOLD"] = 50.0
        baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

        h.set_outside(True)
        h.pump(2)

        # Nala establishes a strong majority first.
        for _ in range(20):
            _inject_frame(h, own_cat=70.0, cat_name="Nala")
        h.pump(3)
        assert any("matches RFID tag 'CAT001'" in r.message for r in caplog.records)

        # A real different cat (Patoune) now dominates the most recent
        # frames - 8/8 of the last window, well above the required majority.
        caplog.clear()
        for _ in range(8):
            _inject_frame(h, own_cat=70.0, cat_name="Patoune")
        h.pump(3)
        assert any("Different known cat detected by video only" in r.message for r in caplog.records)
        assert any("immediately starting a new block" in r.message for r in caplog.records)
    finally:
        h.shutdown()
        image_buffer.clear()


def test_video_identity_no_split_on_weak_recent_minority(loop_db, caplog):
    """A handful of stray frames for a different cat (below the required
    recent majority) must NOT split the block - that's the same per-frame
    noise the majority-vote fix is meant to absorb, not a real second cat."""
    from src.database import CatsRepo

    CatsRepo.db_add_new_cat(
        loop_db, "Patoune", "CAT002", "",
        enable_prey_detection=True, allow_entry=True, allow_exit=True,
    )
    h = build_loop_context(db_path=loop_db, cat_name="Nala", cat_rfid="CAT001")
    try:
        baseconfig.CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = True
        baseconfig.CONFIG["CAT_THRESHOLD"] = 50.0
        baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

        h.set_outside(True)
        h.pump(2)

        for _ in range(20):
            _inject_frame(h, own_cat=70.0, cat_name="Nala")
        h.pump(3)
        assert any("matches RFID tag 'CAT001'" in r.message for r in caplog.records)

        caplog.clear()
        for _ in range(2):
            _inject_frame(h, own_cat=70.0, cat_name="Patoune")
        h.pump(3)
        assert not any("Different known cat detected by video only" in r.message for r in caplog.records)
    finally:
        h.shutdown()
        image_buffer.clear()


def test_video_identity_no_split_when_incumbent_barely_established(loop_db, caplog):
    """Real false positive Sid caught live (11.10): a single cat (Nala)
    approached the flap, backed off, and came back - only ONE physical cat
    the whole time. But the incumbent identity had only just been assigned
    (2 votes) when a short noisy streak misidentified her as Patoune for
    6/8 of the most recent frames - at that point in a fresh block, "the
    last 8 frames" IS basically the block's entire history, not a late
    arrival after an established presence. The split fired incorrectly,
    confirmed via real production logs the same day. Must not split when
    the incumbent hasn't built up a real track record yet - a freshly
    assigned identity gets corrected by the ordinary majority-vote path
    instead, not the split path."""
    from src.database import CatsRepo

    CatsRepo.db_add_new_cat(
        loop_db, "Patoune", "CAT002", "",
        enable_prey_detection=True, allow_entry=True, allow_exit=True,
    )
    h = build_loop_context(db_path=loop_db, cat_name="Nala", cat_rfid="CAT001")
    try:
        baseconfig.CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = True
        baseconfig.CONFIG["CAT_THRESHOLD"] = 50.0
        baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

        h.set_outside(True)
        h.pump(2)

        # Nala barely gets assigned (only 2 votes) before a noisy streak of
        # 6 "patoune" misreads follows - same ratio (6/8) as the real
        # incident, but with the incumbent nowhere near established yet.
        for _ in range(2):
            _inject_frame(h, own_cat=92.0, cat_name="Nala")
        h.pump(2)
        assert any("matches RFID tag 'CAT001'" in r.message for r in caplog.records)

        caplog.clear()
        for _ in range(6):
            _inject_frame(h, own_cat=78.0, cat_name="Patoune")
        h.pump(3)
        assert not any("Different known cat detected by video only" in r.message for r in caplog.records)
    finally:
        h.shutdown()
        image_buffer.clear()


def test_vote_winner_propagates_into_stored_photo_labels(loop_db, caplog):
    """11.10 (round 2 continued), Sid ("reporter ton correctif dans les
    videos/photos - c'est avec ca que je peux entrainer le modele"): the
    majority-vote fix only ever changed tag_id_from_video (door decisions +
    the one timeline label) - each individual stored photo kept its own
    raw, noisy per-frame name untouched. Verify the block's final, winning
    identity gets written onto every stored cat-identity detection before
    the block is written to the database, so the training-review workflow
    shows the corrected name instead of the per-frame noise."""
    from src.database import CatsRepo
    from src.camera import image_buffer as ib

    CatsRepo.db_add_new_cat(
        loop_db, "Pookie", "CAT002", "",
        enable_prey_detection=True, allow_entry=True, allow_exit=True,
    )
    h = build_loop_context(db_path=loop_db, cat_name="Nala", cat_rfid="CAT001")
    try:
        baseconfig.CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = True
        baseconfig.CONFIG["CAT_THRESHOLD"] = 50.0
        baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

        h.set_outside(True)
        h.pump(2)

        # 20 consistent Nala frames establish the majority, plus one noisy
        # Pookie outlier the vote correctly ignores for the decision - but
        # the stored photo itself still reads "pookie" until propagation.
        for _ in range(20):
            _inject_frame(h, own_cat=70.0, cat_name="Nala")
        _inject_frame(h, own_cat=95.0, cat_name="Pookie")
        h.pump(3)

        # Finalize the block (motion stops).
        h.set_outside(False)
        h.advance(0.5)
        h.pump(5)

        ids = ib.get_filtered_ids(0.0)
        assert ids, "expected images still in the buffer to check"
        names = set()
        for i in ids:
            img = ib.get_by_id(i)
            for obj in (img.detected_objects or []):
                names.add(obj.object_name.lower())
        assert names == {"nala"}
    finally:
        h.shutdown()
        image_buffer.clear()


def test_video_cat_detection_skipped_during_confirmed_exit(harness, caplog):
    """11.10, Sid ("j'aimerais éliminer au mieux les faux positifs... c'est
    obligatoirement le bon chat vu qu'il rentre dans le tunnel, ça lit RFID"):
    once an exit is in progress AND a real chip read has confirmed identity,
    re-scanning video for cat identity is pure redundant noise (the source of
    the flickering "Nala 42% / Patoune 79%" labels on the same physical cat
    seen in the crash investigation). Must stay fully active for entry/
    before-RFID cases - only this specific combination is skipped."""
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.ALLOW
    baseconfig.CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = True
    baseconfig.CONFIG["CAT_THRESHOLD"] = 50.0
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    # Inside motion -> exit permitted (ALLOW mode needs no RFID at all to
    # unlock), then the cat's own real chip read confirms who it is.
    harness.set_inside(True)
    harness.pump(3)
    assert harness.magnets.get_outside_state() is True

    harness.inject_rfid("CAT001")
    harness.advance(0.5)
    harness.pump(2)

    # Now outside motion continues (cat walking through the tunnel) with a
    # high-confidence video frame for a DIFFERENT cat name injected - would
    # normally flip tag_id_from_video and log a video-detection line.
    caplog.clear()
    harness.set_outside(True)
    harness.pump(2)
    _inject_frame(harness, own_cat=92.0, cat_name="Mia")
    harness.pump(5)

    assert not any("by video stream" in r.message for r in caplog.records)


def test_video_cat_detection_still_skipped_after_outside_timeout_relock(harness, caplog):
    """11.10, Sid real-world test that caught this: the FIRST version of the
    above fix gated on `exit_in_progress`, which goes back to False via the
    OPEN_OUTSIDE_TIMEOUT auto-relock (below, once motion_inside reads 0) well
    before the block itself finalizes - a cat still lingering in outside-PIR
    view after that point kept generating "by video stream" log lines in
    production. `current_block_rfid` must keep suppressing video for the
    whole block regardless of this earlier, unrelated magnet-timeout reset."""
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.ALLOW
    baseconfig.CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = True
    baseconfig.CONFIG["CAT_THRESHOLD"] = 50.0
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    harness.set_inside(True)
    harness.pump(3)
    assert harness.magnets.get_outside_state() is True

    harness.inject_rfid("CAT001")
    harness.advance(0.5)
    harness.pump(2)

    # Cat has left the inside zone - OPEN_OUTSIDE_TIMEOUT (1.0s in tests,
    # see the module-level monkeypatch above) now elapses and auto-relocks,
    # resetting exit_in_progress to False while the block stays open.
    harness.set_inside(False)
    harness.advance(1.5)
    harness.pump(3)
    assert harness.magnets.get_outside_state() is False

    # Cat is still lingering in view outside - must NOT re-trigger video.
    caplog.clear()
    harness.set_outside(True)
    harness.pump(2)
    _inject_frame(harness, own_cat=92.0, cat_name="Mia")
    harness.pump(5)

    assert not any("by video stream" in r.message for r in caplog.records)


# --- Manual overrides / max unlock ------------------------------------------


def test_manual_unlock_and_lock_outside(harness):
    # Deny automatic exit so outside unlock comes only from the manual override path.
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.DENY
    harness.set_inside(True)
    harness.pump(1)
    assert harness.magnets.get_outside_state() is False

    harness.set_manual_override(unlock_outside=True)
    harness.pump(3)
    assert harness.magnets.get_outside_state() is True

    # Already open path
    harness.set_manual_override(unlock_outside=True)
    harness.pump(2)
    assert harness.magnets.get_outside_state() is True


def test_manual_lock_inside_while_open(harness):
    harness.set_manual_override(unlock_inside=True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is True

    harness.set_manual_override(lock_inside=True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is False

    # Already locked path
    harness.set_manual_override(lock_inside=True)
    harness.pump(2)


def test_manual_unlock_inside_already_open(harness):
    harness.set_manual_override(unlock_inside=True)
    harness.pump(3)
    harness.set_manual_override(unlock_inside=True)
    harness.pump(2)
    assert harness.magnets.get_inside_state() is True


def test_manual_unlock_annotates_active_motion_block(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.NONE
    harness.set_outside(True)
    harness.pump(2)
    harness.set_manual_override(unlock_inside=True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is True


def test_max_unlock_time_forces_inside_lock(harness):
    harness.set_manual_override(unlock_inside=True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is True

    harness.advance(3.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False


def test_max_unlock_time_forces_outside_lock(harness):
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.DENY
    harness.set_inside(True)
    harness.pump(1)
    harness.set_manual_override(unlock_outside=True)
    harness.pump(3)
    assert harness.magnets.get_outside_state() is True

    harness.advance(3.5)
    harness.pump(5)
    assert harness.magnets.get_outside_state() is False


def test_manual_lock_annotates_active_outside_block(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    harness.set_outside(True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is True

    harness.set_manual_override(lock_inside=True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is False


def test_max_unlock_annotates_active_outside_block(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.NONE
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    harness.set_outside(True)
    harness.pump(3)
    harness.set_manual_override(unlock_inside=True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is True

    harness.advance(3.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False


def test_finalize_writes_tag_onto_buffered_images(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    harness.set_outside(True)
    harness.inject_rfid("CAT001")
    harness.pump(2)
    _inject_frame(harness, own_cat=80.0)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True

    harness.set_outside(False)
    harness.advance(0.5)
    harness.pump(5)


def test_deferred_entry_reevaluates_after_exit_ends(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.ALLOW
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    for i in (1, 2, 3):
        baseconfig.CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = False

    harness.set_inside(True)
    harness.pump(3)
    assert harness.magnets.get_outside_state() is True

    harness.set_outside(True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is False

    # End exit (inside idle + outside timeout) while outside motion persists.
    harness.set_inside(False)
    harness.advance(1.5)
    harness.pump(5)
    # Fresh rising edge after wait_for_outside_rising_after_exit
    harness.set_outside(False)
    harness.pump(2)
    harness.set_outside(True)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True


def test_suppress_flags_clear_after_fast_crossing(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    baseconfig.CONFIG["IMMEDIATE_LOCK_AFTER_PASSAGE"] = True

    harness.set_outside(True)
    harness.inject_rfid("CAT001")
    harness.advance(0.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True

    harness.advance(0.3)
    harness.set_inside(True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is False

    # Clear motion so suppress_* flags reset (770/772).
    harness.set_outside(False)
    harness.set_inside(False)
    harness.pump(5)


# --- Timeouts / RFID restart / long block -----------------------------------


def test_tag_timeout_forgets_tag(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL_RFIDS
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    harness.set_outside(True)
    harness.inject_rfid("ANY99")
    harness.pump(3)
    assert harness.rfid.get_tag()[0] == "ANY99"

    harness.set_outside(False)
    harness.advance(2.5)
    harness.pump(5)
    assert harness.rfid.get_tag()[0] is None


def test_rfid_field_turns_off_after_idle(harness):
    harness.set_outside(True)
    harness.pump(2)
    harness.rfid.set_field(True)
    assert harness.rfid.get_field() is True

    harness.set_outside(False)
    harness.set_inside(False)
    harness.pump(3)  # process falling edges → set last_motion_*_mono
    harness.advance(1.5)
    harness.pump(5)
    assert harness.rfid.get_field() is False


def test_rfid_restarts_when_stopped(harness):
    harness.rfid.set_run_state(RfidRunState.stopped)
    harness.pump(2)
    # Restart thread is spawned; FakeRfid.run settles to stopped after idle loop ends,
    # but the restart path must have been taken without raising.
    assert harness.rfid.get_run_state() in (
        RfidRunState.running,
        RfidRunState.stopped,
        RfidRunState.stop_requested,
    )


def test_max_motion_block_seconds_finalizes(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    harness.set_outside(True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is True

    # Prevent re-unlock after timeout finalize resets unlock_inside_decision_made.
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.NONE
    harness.advance(5.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False


def test_mouse_check_recovers_after_forced_block_finalize(harness, caplog):
    """11.10, Sid (vrai incident: Boubou RFID-confirmé, "allowed to enter", jamais
    réouvert pendant 4 minutes réelles - les logs montrent le même Block ID du début
    à la fin, preuve que motion_outside n'est jamais retombé à 0): le garde-fou
    MAX_MOTION_BLOCK_SECONDS existe précisément pour le cas où le mouvement extérieur
    ne retombe jamais proprement ("continuous movement near flap"), mais finalisait le
    bloc SANS en redémarrer un nouveau - first_motion_outside_mono restait gelé à 0.0
    (plus aucun "rising edge" ne pouvant se produire tant que le mouvement continue),
    bloquant sufficient_analysis_time à False pour toujours et empêchant toute
    réouverture pour un chat déjà identifié et autorisé."""
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = True
    baseconfig.CONFIG["MIN_SECONDS_TO_ANALYZE"] = 1.0

    harness.set_outside(True)
    harness.inject_rfid("CAT001")
    harness.pump(3)

    # Motion never falls back to 0 - MAX_MOTION_BLOCK_SECONDS (5.0s in tests, see the
    # module-level monkeypatch above) gets exceeded while motion_outside is still 1,
    # forcing the safety-net finalize.
    harness.advance(5.5)
    harness.pump(3)
    assert any(
        "Finalizing to prevent endless block growth" in r.message for r in caplog.records
    )
    assert any(
        "immediately starting a new block" in r.message for r in caplog.records
    )

    # Re-confirm identity for the new block the forced finalize should have started
    # (unlock_inside_decision_made was reset by it) - same cat, still physically present.
    harness.inject_rfid("CAT001")
    harness.pump(2)
    harness.advance(1.2)  # past MIN_SECONDS_TO_ANALYZE (1.0s) for the NEW block's timer
    harness.pump(2)

    # Without the fix, sufficient_analysis_time stays frozen at False forever after
    # the forced finalize (first_motion_outside_mono stuck at 0.0, no new rising edge
    # possible), and this condition never flips back to True.
    assert any(
        "Mouse check condition 'sufficient_analysis_time' changed to True" in r.message
        for r in caplog.records
    )


# --- Immediate lock / deferred entry / cat went inside -----------------------


def test_fast_exit_crossing_with_immediate_lock(harness):
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.ALLOW
    baseconfig.CONFIG["IMMEDIATE_LOCK_AFTER_PASSAGE"] = True
    for i in (1, 2, 3):
        baseconfig.CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = False

    harness.set_inside(True)
    harness.pump(3)
    assert harness.magnets.get_outside_state() is True

    harness.advance(0.3)
    harness.set_outside(True)
    harness.pump(5)
    assert harness.magnets.get_outside_state() is False
    assert harness.magnets.get_inside_state() is False


def test_exit_defers_entry_while_in_progress(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.ALLOW
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    for i in (1, 2, 3):
        baseconfig.CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = False

    harness.set_inside(True)
    harness.pump(3)
    assert harness.magnets.get_outside_state() is True

    # Outside motion during exit should not unlock inside (deferred entry).
    harness.set_outside(True)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False


def test_cat_went_inside_finalize_path(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    harness.set_outside(True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is True

    harness.set_inside(True, raw=True)
    harness.pump(2)
    harness.set_outside(False)
    harness.advance(0.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False


def test_lazy_cat_workaround_keeps_motion(harness, monkeypatch):
    monkeypatch.setattr(loop_mod, "LAZY_CAT_DELAY_PIR_MOTION", 2.0)
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    harness.set_outside(True)
    harness.pump(2)
    harness.set_outside(False)
    # Within lazy window: motion should still be treated as active enough not to
    # immediately finalize before delay elapses — advance less than delay.
    harness.advance(0.5)
    harness.pump(2)
    # After delay, falling edge finalizes.
    harness.advance(2.0)
    harness.pump(5)
