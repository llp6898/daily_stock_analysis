#!/bin/bash
# Railway 启动脚本
# 用途：安装依赖 → 启动 Flask 服务

set -e

echo "安装依赖..."
pip install -r requirements.txt --quiet 2>&1 | tail -3

echo "启动服务..."
export PORT=10000
export API_PORT=10000
export TUSHARE_TOKEN=${TUSHARE_TOKEN:-b2d323ce6e8bf2c1549a72fd08538c1dc1ac4bf563550632c1a01759}

echo "TUSHARE_TOKEN: ${TUSHARE_TOKEN:0:10}..."
echo "PORT: $PORT"
echo "Starting Flask app..."

exec python main.py