# Build from monorepo root:
#   docker build -f sas-agent/Dockerfile -t sas-agent:monolithic .
FROM python:3.11-slim

WORKDIR /app/sas-agent

COPY sas-agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sas-agent/ .

ENV BAND_PROFILE=cbrs_winnforum \
    SAS_DATABASE_PATH=/data/sas_mvp.db

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 9000 9001

CMD ["python", "main.py"]
