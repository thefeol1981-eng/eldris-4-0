FROM node:22-alpine AS frontend-build
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app/backend
ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    DEBUG=false \
    SESSION_COOKIE_SECURE=true \
    DATABASE_PATH=/var/data/eldris.sqlite3
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=frontend-build /build/frontend/dist /app/frontend/dist
CMD ["sh", "-c", "gunicorn --workers 1 --threads 100 --bind 0.0.0.0:${PORT:-10000} app:app"]
