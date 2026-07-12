FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[dashboard]"

# Bind to all interfaces inside the container; persist the event log in /data.
ENV BALLAST_HOST=0.0.0.0 \
    BALLAST_DB=/data/ballast_events.db
RUN mkdir -p /data
VOLUME /data

EXPOSE 8080
CMD ["python", "-m", "ballast.dashboard"]
