FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY monitor.py .

RUN useradd --create-home appuser
USER appuser

ENTRYPOINT ["python", "/app/monitor.py"]