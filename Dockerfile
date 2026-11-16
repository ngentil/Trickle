# Scheduler only — the sticks run worker/worker.py directly.
#   docker build -t moonpickle .
#   docker run -d -p 8700:8700 -v moonpickle:/data -e MOONPICKLE_ADMIN_TOKEN=... moonpickle
FROM python:3.12-slim
WORKDIR /app
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server/ .
COPY cli/moonpickle.py /usr/local/bin/moonpickle
ENV MOONPICKLE_DB=/data/moonpickle.db
VOLUME /data
EXPOSE 8700
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8700", "--proxy-headers"]
