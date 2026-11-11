# llmgate demo container: the gateway plus two simulated providers, no real upstream.
#
# Build:  docker build -t llmgate .
# Run:    docker run --rm -p 8000:8000 llmgate
#         curl -H 'Authorization: Bearer demo-key' -H 'content-type: application/json' \
#           -d '{"model":"gpt-sim","messages":[{"role":"user","content":"hi"}]}' \
#           localhost:8000/v1/chat/completions
#
# Real upstreams: mount a config (see examples.config.json) and override the
# command with the config factory. Secrets come from the environment only:
#   docker run --rm -p 8000:8000 \
#     -e LLMGATE_CONFIG=/cfg/prod.json -e LLMGATE_KEYS -e PRIMARY_API_KEY \
#     -v "$PWD/my.json:/cfg/prod.json:ro" \
#     llmgate python -m uvicorn --factory llmgate.config:app --host 0.0.0.0 --port 8000
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY llmgate ./llmgate
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home llmgate
USER llmgate
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
CMD ["python", "-m", "uvicorn", "llmgate.demo_app:app", "--host", "0.0.0.0", "--port", "8000"]
