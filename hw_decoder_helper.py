"""21.10, Sid ("creuse le vrai fixe" - videoconvert sature le CPU à 15
img/s, vapostproc/vaapipostproc confirmés absents des paquets Debian
trixie disponibles - gst-launch-1.0 en CLI n'a pas d'autre option) :
petit sous-processus séparé (même isolation LD_LIBRARY_PATH que
l'ancienne commande gst-launch-1.0 - voir watchdog.py/_start_hw_decoder)
qui décode sur GPU (vah264dec, inchangé) et convertit NV12->BGR via
OpenCV au lieu de GStreamer's videoconvert (logiciel, lent). Contrairement
à la tentative ratée plus tôt ce soir (comptage d'octets à la main, cassé
par un stride matériel caché), ce script lit via appsink + GstVideo.
VideoInfo - le stride RÉEL de chaque plan (Y et UV) vient de l'API
GStreamer elle-même, jamais deviné.

appsink (max-buffers=1, drop=True) règle aussi le problème de retard : il
garde toujours la frame la PLUS RÉCENTE en interne, jette automatiquement
les anciennes avant même qu'on les lise - plus besoin du drain manuel
côté watchdog.py pour ce chemin (gardé quand même, inoffensif).

Protocole stdout : frames BGR24 brutes de width*height*3 octets, bout à
bout - IDENTIQUE à l'ancien protocole gst-launch-1.0+fdsink, donc
_read_hw_frame côté watchdog.py n'a rien à changer.
"""

import sys

import cv2
import gi
import numpy as np

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstApp, GstVideo  # noqa: E402


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: hw_decoder_helper.py <rtsp_url> <width> <height>", file=sys.stderr)
        return 2
    rtsp_url, width, height = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])

    Gst.init(None)
    # 21.10, Sid - sur un conteneur fraîchement démarré (jamais de
    # ~/.cache/gstreamer-1.0/registry.x86_64.bin construit avec succès
    # auparavant), Gst.init(None) seul ne trouve PAS vah264dec par nom
    # (confirmé en direct : "no element vah264dec", y compris via
    # gst-launch-1.0 en CLI, pas spécifique à Python/gi) - mais inspecter
    # le .so directement, OU rescanner explicitement son dossier comme
    # ci-dessous, le trouve à coup sûr. Pas de dépendance à un cache
    # construit "par chance" lors d'essais précédents (c'était le cas en
    # prod ce soir, mais pas garanti sur un déploiement propre).
    Gst.Registry.get().scan_path("/usr/lib/x86_64-linux-gnu/gstreamer-1.0")

    pipeline_str = (
        f'rtspsrc location="{rtsp_url}" latency=200 protocols=tcp '
        "! queue leaky=downstream max-size-buffers=10 "
        "! rtph264depay ! h264parse "
        "! queue leaky=downstream max-size-buffers=10 "
        "! vah264dec "
        f"! video/x-raw,format=NV12,width={width},height={height} "
        "! appsink name=sink emit-signals=False sync=False max-buffers=1 drop=True"
    )
    try:
        pipeline = Gst.parse_launch(pipeline_str)
    except Exception as e:
        print(f"pipeline parse failed: {e}", file=sys.stderr)
        return 1
    appsink = pipeline.get_by_name("sink")

    bus = pipeline.get_bus()
    pipeline.set_state(Gst.State.PLAYING)

    out = sys.stdout.buffer
    frame_bytes = width * height * 3
    rc = 0
    first_frame = True
    try:
        while True:
            # Surfaces real pipeline errors (RTSP drop, decode failure) fast
            # instead of silently hanging on try_pull_sample - same
            # reconnect-on-death contract the old gst-launch-1.0 process had.
            msg = bus.timed_pop_filtered(0, Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if msg is not None:
                if msg.type == Gst.MessageType.ERROR:
                    err, debug = msg.parse_error()
                    print(f"pipeline error: {err} ({debug})", file=sys.stderr)
                else:
                    print("pipeline EOS", file=sys.stderr)
                break

            # 21.10, Sid - testé en direct : la toute première frame a mis
            # 3.75s à arriver (RTSP+VAAPI pas encore chauds), largement
            # au-dessus d'un timeout de 2s qui faisait abandonner le script
            # avant même d'avoir essayé. Même budget que
            # HW_DECODE_STARTUP_TIMEOUT_S côté watchdog.py pour la première
            # frame ; 2s ensuite suffit largement (le flux tourne déjà).
            pull_timeout = (20 if first_frame else 2) * Gst.SECOND
            sample = appsink.try_pull_sample(pull_timeout)
            first_frame = False
            if sample is None:
                print("no sample within timeout", file=sys.stderr)
                break

            buf = sample.get_buffer()
            caps = sample.get_caps()
            vinfo = GstVideo.VideoInfo.new_from_caps(caps)
            if vinfo is None:
                print("could not parse caps", file=sys.stderr)
                break

            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if not ok:
                print("buffer map failed", file=sys.stderr)
                break
            try:
                data = mapinfo.data
                y_stride = vinfo.stride[0]
                uv_stride = vinfo.stride[1]
                y_offset = vinfo.offset[0]
                uv_offset = vinfo.offset[1]
                h, w = vinfo.height, vinfo.width

                y_plane = np.frombuffer(
                    data, dtype=np.uint8, count=y_stride * h, offset=y_offset
                ).reshape((h, y_stride))[:, :w]
                uv_plane = np.frombuffer(
                    data, dtype=np.uint8, count=uv_stride * (h // 2), offset=uv_offset
                ).reshape((h // 2, uv_stride))[:, :w]
                nv12 = np.ascontiguousarray(np.vstack([y_plane, uv_plane]))
            finally:
                buf.unmap(mapinfo)

            bgr = cv2.cvtColor(nv12, cv2.COLOR_YUV2BGR_NV12)
            if bgr.shape[0] != height or bgr.shape[1] != width:
                # Caps negotiated a different size than expected - fail
                # loud rather than write a frame the parent can't frame
                # correctly (exactly the bug from the earlier NV12 attempt).
                print(
                    f"size mismatch: got {bgr.shape[1]}x{bgr.shape[0]}, expected {width}x{height}",
                    file=sys.stderr,
                )
                break
            payload = bgr.tobytes()
            if len(payload) != frame_bytes:
                print(f"payload size {len(payload)} != expected {frame_bytes}", file=sys.stderr)
                break
            out.write(payload)
            out.flush()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    except Exception as e:
        print(f"unexpected error: {e}", file=sys.stderr)
        rc = 1
    finally:
        pipeline.set_state(Gst.State.NULL)
    return rc


if __name__ == "__main__":
    sys.exit(main())
