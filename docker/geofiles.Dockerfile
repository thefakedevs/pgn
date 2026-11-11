FROM python:3.13-alpine
RUN apk add --no-cache tzdata
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY scripts/geofiles.py /app/geofiles.py
ENTRYPOINT ["python", "/app/geofiles.py"]
