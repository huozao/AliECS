FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src:/app

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY config ./config
COPY src ./src
COPY scripts ./scripts

CMD ["python", "-m", "tplus_datahub.jobs.job_sync_bom"]
