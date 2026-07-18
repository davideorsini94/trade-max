# ---------- Stage 1: build frontend ----------
FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---------- Stage 2: backend + static frontend ----------
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt ./
# tzdata: zoneinfo needs the IANA database for Europe/Rome (scheduler jobs).
RUN pip install --no-cache-dir -r requirements.txt tzdata

COPY backend/ ./backend/
COPY --from=frontend /build/dist ./frontend/dist

# SQLite file lives on a mounted volume (see docker-compose.yml).
RUN mkdir -p /app/data
ENV DB_PATH=/app/data/trademax.db \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app/backend
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
