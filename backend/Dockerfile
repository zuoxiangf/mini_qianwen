# syntax=docker/dockerfile:1
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
    -i http://mirrors.cloud.aliyuncs.com/pypi/simple/ \
    --trusted-host mirrors.cloud.aliyuncs.com \
    --prefer-binary \
    -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]