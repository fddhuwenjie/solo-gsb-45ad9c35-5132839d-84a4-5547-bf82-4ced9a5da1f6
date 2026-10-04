FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    PLANNER_DB=/data/planner.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 容器启动时自动建表（get_db 在 import 时不执行，用启动命令触发一次）
CMD ["sh", "-c", "python -c \"from app.main import get_db; get_db().close()\" && gunicorn --bind 0.0.0.0:${PORT} --workers 2 --timeout 60 app.main:app"]
