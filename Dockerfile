FROM python:3.11-slim

# ffmpeg: used by src/camera.py to decode the external RTSP (Reolink) stream.
# libgl1/libglib2.0-0: transitive runtime deps of opencv-python-headless.
# intel-media-va-driver/vainfo: 11.10 (round 2), Sid ("meilleures photos
# avec moins de charge CPU... regarde pour le GPU et mets en place ce dont
# on a parle") - VAAPI userspace driver (iHD) for OLDMEDIA's Intel iGPU
# (J4125, Gemini Lake), passed through via compose.yaml's `devices:` on
# kittyhack-watchdog only. Lets watchdog.py hardware-decode chatiere's
# full-resolution Reolink stream instead of software-decoding it - without
# this package libva has the runtime but no actual driver to load
# ("unknown libva error" at init, confirmed live before adding this).
# vainfo is just for manual troubleshooting (`docker exec ... vainfo`), not
# used by the app itself - cheap to keep.
#
# 21.10, Sid ("on va l'ouvrir maintenant" puis, après avoir trouvé que
# vah264dec a besoin de libigdgmm12 >= 22.7.2+ds1 alors qu'OpenVINO sur ce
# GPU a besoin de 22.3.0 EXACT (voir plus bas) - testé les deux en direct,
# aucune ne satisfait les deux à la fois : "Tu peux reproduire le souci de
# base dans ton container éphémère ?" puis "Y avait pas d'autres options
# que ça ?") - gstreamer1.0-tools/plugins-base/plugins-good/plugins-bad
# pour le décodage matériel chatière via GStreamer (vah264dec), AVEC
# isolation : `apt-get install` ci-dessous, sans conflit à ce stade
# (libigdgmm12 pas encore épinglé), tire naturellement la version récente
# de GmmLib pour satisfaire intel-media-va-driver - une copie est gardée
# dans /opt/gmm-new AVANT que le bloc OpenVINO plus bas ne force le
# système entier sur 22.3.0. `watchdog.py` (_start_hw_decoder) pointe
# `LD_LIBRARY_PATH` sur cette copie privée pour le SEUL sous-processus
# gst-launch-1.0 - le process Python principal (OpenVINO) ne voit jamais
# cette variable, garde la version système 22.3.0. Testé dans un
# conteneur jetable avant d'écrire ça ici : décodage matériel ET
# inférence GPU OpenVINO fonctionnent simultanément, chacun avec sa
# propre version. Voir KITTYHACK_ROADMAP.md pour le détail complet de
# l'exploration (tentative sans isolation d'abord, abandonnée, puis
# cette solution trouvée).
#
# The dpkg-divert line right before the install is a real, confirmed fix
# (not precautionary): a first build attempt hung for 18+ minutes with
# `update-mime-database` stuck in D-state (uninterruptible disk sleep) -
# a known issue where shared-mime-info's postinst trigger (pulled in
# transitively by this driver package) can wedge in a container. This app
# never needs the system MIME database rebuilt, so the real binary is
# diverted and replaced with a no-op before anything can trigger it.
RUN dpkg-divert --local --rename --add /usr/bin/update-mime-database \
    && ln -s /bin/true /usr/bin/update-mime-database \
    && apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    gettext \
    intel-media-va-driver \
    vainfo \
    wget \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    && mkdir -p /opt/gmm-new \
    && cp /usr/lib/x86_64-linux-gnu/libigdgmm.so.12* /opt/gmm-new/ \
    && rm -rf /var/lib/apt/lists/*

# 20.10, Sid ("je veux le GPU partout où c'est possible") - the OpenCL/
# Level-Zero compute stack OpenVINO needs to actually see the iGPU (as
# opposed to intel-media-va-driver above, which is VAAPI/video-decode
# only - a separate stack). NOT in Debian's apt repos at all (verified
# live tonight: `apt list intel-opencl-icd` returns nothing) - real
# GitHub release .deb files, and the EXACT versions matter: anything from
# compute-runtime branch 24.39+ (Oct 2024) has dropped Gemini Lake/UHD 600
# support entirely (confirmed via Intel's own release notes), and even
# 24.13 (nominally "before" that cutoff) still failed to expose a GPU
# device in a real test tonight. 23.05.25593.11 + its EXACT matching
# intel-graphics-compiler release (igc-1.0.13230.7, named in 23.05's own
# release notes - do not pair with a newer/older igc, dependency versions
# must match exactly or dpkg refuses to configure) is the last combination
# confirmed working on this exact hardware (OLDMEDIA, Celeron J4125).
# Previously live-installed by hand and lost on a later container
# recreate (privileged/devices added to compose.yaml) - baked in for real
# this time so that never happens again.
#
# 21.10: ce dpkg -i de libigdgmm12 22.3.0 a maintenant un second rôle,
# intentionnel - il force le système ENTIER à revenir sur 22.3.0 après
# que l'install de GStreamer ci-dessus a naturellement tiré une version
# plus récente pour satisfaire intel-media-va-driver (déjà copiée à part
# dans /opt/gmm-new avant ce downgrade). Ne pas réordonner ces deux
# blocs - l'ordre (gstreamer+copie, PUIS ce downgrade) est ce qui fait
# marcher les deux versions en même temps.
RUN wget -q -P /tmp/intel-gpu \
    https://github.com/intel/intel-graphics-compiler/releases/download/igc-1.0.13230.7/intel-igc-core_1.0.13230.7_amd64.deb \
    https://github.com/intel/intel-graphics-compiler/releases/download/igc-1.0.13230.7/intel-igc-opencl_1.0.13230.7_amd64.deb \
    https://github.com/intel/compute-runtime/releases/download/23.05.25593.11/intel-level-zero-gpu_1.3.25593.11_amd64.deb \
    https://github.com/intel/compute-runtime/releases/download/23.05.25593.11/intel-opencl-icd_23.05.25593.11_amd64.deb \
    https://github.com/intel/compute-runtime/releases/download/23.05.25593.11/libigdgmm12_22.3.0_amd64.deb \
    && dpkg -i /tmp/intel-gpu/*.deb \
    && rm -rf /tmp/intel-gpu

WORKDIR /app

# Installed from requirements files baked into the image (rebuild image on dependency
# changes); the actual application source is bind-mounted at runtime (see compose.yaml)
# so day-to-day code edits (dual-camera work) don't need an image rebuild.
COPY requirements.txt requirements_remote.txt ./
# 20.10, Sid ("je veux le GPU partout où c'est possible") - real failure
# tonight after the driver fix finally worked: the built image (CUDA-
# flavored torch pulled in by plain `torch==2.6.0`, nvidia-cublas-cu12/
# nvidia-cudnn-cu12/etc - entirely unused, this host has no NVIDIA GPU,
# the real GPU work here is Intel iGPU via OpenVINO) came out over
# GitHub's 2GB release-asset limit. --extra-index-url here only, not in
# requirements.txt itself - that file is shared with the native ARM
# kittyflap build (different platform, different wheels needed there;
# this index is x86_64-only and simply won't match on ARM, so it's inert
# there rather than risky).
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu \
    -r requirements.txt -r requirements_remote.txt

ENV KITTYHACK_MODE=remote
ENV KITTYHACK_INSTALL_BASE=/data

EXPOSE 80

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "80", "--ws-ping-interval", "30", "--ws-ping-timeout", "120"]
