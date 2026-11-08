FROM gcr.io/google.com/cloudsdktool/google-cloud-cli:slim AS gcloud
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libegl1 libgl1 libgl1-mesa-dri libosmesa6 libglib2.0-0 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=gcloud /usr/lib/google-cloud-sdk /opt/google-cloud-sdk
ENV PATH="/opt/google-cloud-sdk/bin:${PATH}" CLOUDSDK_PYTHON=/usr/local/bin/python \
    MUJOCO_GL=osmesa PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    LP_NUM_THREADS=2 OMP_NUM_THREADS=2 HARNESS_CLAUDE_BACKEND=api
RUN pip install --no-cache-dir dreamlabs==1.0.37 mujoco==4.0.0 numpy==2.2.6 \
    pillow==13.0.0 PyYAML==6.0.3 scipy==1.18.1 psutil==7.2.2
WORKDIR /app
COPY harness/ harness/
COPY sim_scenes/ sim_scenes/
COPY plans/ plans/
COPY lessons/ lessons/
COPY experiments/sim_match.py experiments/sim_match.py
COPY sim_station.py ./
COPY cloud/ cloud/
ARG SOURCE_REVISION=unknown
ENV SOURCE_REVISION=${SOURCE_REVISION}
RUN gcloud version && python -m cloud.validate
ENTRYPOINT ["python", "-m", "cloud.entrypoint"]
