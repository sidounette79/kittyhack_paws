# 20.10, Sid ("tout ce qu'il faut pour soulager le CPU, carte blanche") -
# 3.12 -> 3.11 : openvino==2023.3.0 (seule version qui voit l'iGPU Intel
# sur ce matériel, voir requirements.txt) n'a aucune roue PyPI pour 3.12 -
# vrai échec de build constaté cette nuit. kittyhack-remote (même GPU,
# même openvino==2023.3.0, fonctionne confirmé) tourne déjà en 3.11.
FROM python:3.11-slim

# 20.10, Sid ("je veux le GPU partout où c'est possible") - mêmes pilotes
# Intel que kittyhack-remote (voir son Dockerfile pour l'explication
# complète : versions exactes obligatoires, Gemini Lake/UHD 600 non
# supporté au-delà de compute-runtime 24.39, confirmé en vrai cette nuit
# que même 24.13 échoue, 23.05.25593.11 + igc-1.0.13230.7 est la seule
# combinaison qui fonctionne sur ce matériel).
# ocl-icd-libopencl1 : dépendance du paquet intel-opencl-icd ci-dessous -
# déjà présente côté kittyhack-remote (tirée par ffmpeg/intel-media-va-
# driver), absente ici (image nue, aucun autre paquet apt) - vrai échec de
# build constaté cette nuit sans elle.
RUN apt-get update && apt-get install -y --no-install-recommends wget ocl-icd-libopencl1 \
    && rm -rf /var/lib/apt/lists/* \
    && wget -q -P /tmp/intel-gpu \
    https://github.com/intel/intel-graphics-compiler/releases/download/igc-1.0.13230.7/intel-igc-core_1.0.13230.7_amd64.deb \
    https://github.com/intel/intel-graphics-compiler/releases/download/igc-1.0.13230.7/intel-igc-opencl_1.0.13230.7_amd64.deb \
    https://github.com/intel/compute-runtime/releases/download/23.05.25593.11/intel-level-zero-gpu_1.3.25593.11_amd64.deb \
    https://github.com/intel/compute-runtime/releases/download/23.05.25593.11/intel-opencl-icd_23.05.25593.11_amd64.deb \
    https://github.com/intel/compute-runtime/releases/download/23.05.25593.11/libigdgmm12_22.3.0_amd64.deb \
    && dpkg -i /tmp/intel-gpu/*.deb \
    && rm -rf /tmp/intel-gpu

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 20.10 : même piège que cette nuit côté test manuel - ultralytics tire sa
# propre dépendance opencv-python (GUI, a besoin de libxcb absent de cette
# image slim) en plus de notre opencv-python-headless explicite dans
# requirements.txt ; pip ne sait pas que les deux fournissent le même
# module cv2, celui installé en dernier gagne au niveau fichiers - jamais
# garanti par le seul ordre des lignes de requirements.txt. Un second
# install forcé, après coup, règle ça pour de bon.
RUN pip install --no-cache-dir --force-reinstall --no-deps opencv-python-headless

COPY app/ ./app/

ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8082"]
