FROM python:3.12-slim

WORKDIR /srv

COPY api/requirements.txt ./api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt

COPY api ./api
COPY db ./db

WORKDIR /srv/api
ENV PYTHONUNBUFFERED=1
