FROM python:3.13-slim

WORKDIR /app
COPY infra/requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY src ./src
COPY apps ./apps
COPY config ./config
ENV PYTHONPATH=/app/src
EXPOSE 8000
