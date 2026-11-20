# Scheduler only — the sticks run worker/worker.py directly.
#   docker build -t ratrun .
#   docker run -d -p 8700:8700 -v ratrun:/data -e RATRUN_ADMIN_TOKEN=... ratrun
FROM python:3.12-slim
WORKDIR /app
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server/ .
COPY cli/ratrun.py /usr/local/bin/ratrun
ENV RATRUN_DB=/data/ratrun.db
VOLUME /data
EXPOSE 8700
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8700", "--proxy-headers"]
