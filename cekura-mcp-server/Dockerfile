FROM python:3.10-slim-bullseye

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CEKURA_OPENAPI_SPEC_PATH=/app/openapi.json

# Build context is the repo root, so both cekura-mcp-server/ and openapi.json are accessible.

WORKDIR /app

# Install Python deps first for layer caching
COPY cekura-mcp-server/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy server source files
COPY cekura-mcp-server/ .

# Copy openapi spec and mint.json from repo root.
# extract_documented_apis.py resolves mint.json as Path(__file__).parent.parent / 'mint.json'
# i.e. /mint.json — so we place it one level above /app.
COPY openapi.json /app/openapi.json
COPY mint.json /mint.json

# Regenerate documented_apis.json at build time — replaces the manual sync_apis.sh step.
# The script writes output to Path(__file__).parent / 'documented_apis.json' = /app/documented_apis.json
RUN python3 extract_documented_apis.py

EXPOSE 8001

CMD ["python3", "openapi_mcp_server.py", "--host", "0.0.0.0", "--port", "8001"]
