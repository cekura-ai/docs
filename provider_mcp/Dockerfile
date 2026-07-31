FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ELEVENLABS_MCP_OUTPUT_MODE=resources

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 10001 appuser
COPY --chown=appuser:appuser . .

USER appuser
EXPOSE 8080

CMD ["python", "server.py"]
