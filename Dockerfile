FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates ./templates

ENV DATABASE=/data/lostfound.db \
    PORT=8000

# SQLite 数据目录（可挂载卷持久化）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 启动时自动建表/初始化数据（SEED_DEMO=1 时写入演示物品）
CMD ["python", "app.py"]
