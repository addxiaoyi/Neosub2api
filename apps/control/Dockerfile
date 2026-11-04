FROM python:3.11-slim

# app/config.py 取 parents[3]、app/routers/system.py 取 parents[4] 反推仓库根，
# 容器内必须保持 <仓库根>/apps/control/app/... 这个层级，否则启动即 IndexError
WORKDIR /repo/apps/control
ENV PYTHONUNBUFFERED=1 \
    UNIFIED_DATA_DIR=/data \
    UNIFIED_HOST=0.0.0.0 \
    UNIFIED_PORT=8000

# 构建 context 是仓库根（compose 里 dockerfile 指向本文件），所以路径都带 apps/control 前缀
COPY apps/control/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/control/app ./app
# 定价种子与更新日志按仓库根路径读取（app/routers/pricing.py、app/routers/system.py）
COPY config /repo/config
COPY CHANGELOG.md /repo/CHANGELOG.md

EXPOSE 8000
CMD ["python", "-m", "app.main"]
