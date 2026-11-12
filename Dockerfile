FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vin_decoder.py .

# Run as an unprivileged user, not root.
RUN useradd --system --no-create-home appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

# The app writes its own JSON request log, so uvicorn's access log is off.
CMD ["uvicorn", "vin_decoder:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
