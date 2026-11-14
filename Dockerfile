# Scheduler only — the sticks run worker/worker.py directly.
#   docker build -t trickle .
#   docker run -d -p 8700:8700 -v trickle:/data -e TRICKLE_ADMIN_TOKEN=... trickle
FROM python:3.12-slim
WORKDIR /app
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server/ .
COPY cli/trickle.py /usr/local/bin/trickle
ENV TRICKLE_DB=/data/trickle.db
VOLUME /data
EXPOSE 8700
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8700", "--proxy-headers"]
