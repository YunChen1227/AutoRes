FROM python:3.11-slim

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码
COPY autores/ ./autores/
COPY frontend/ ./frontend/
COPY tools/ ./tools/

ENV PYTHONUNBUFFERED=1 \
    AUTORES_CONFIG=/app/config.yaml

# 入口由 docker-compose 覆盖（scanner / api 两种）。默认起 API。
CMD ["uvicorn", "autores.server.main:app", "--host", "0.0.0.0", "--port", "8080"]
