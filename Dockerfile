FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY client.py server.py ./

# Streamable-http transport. Override via env at runtime.
ENV MCP_HOST=0.0.0.0 \
    MCP_PORT=8000
EXPOSE 8000

CMD ["python", "server.py"]
