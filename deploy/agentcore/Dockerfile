# Bedrock AgentCore Runtime image.
#
# AgentCore requires linux/arm64, so build with:
#   docker buildx build --platform linux/arm64 -f deploy/agentcore/Dockerfile .
# Build context is the repository root.
FROM --platform=linux/arm64 python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PRODUCTION_ORCHESTRATOR_SESSIONS=/tmp/production-orchestrator-sessions

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.8.17 \
    && uv export --no-dev --frozen --no-emit-project -o requirements.txt \
    && pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y uv \
    && rm requirements.txt

COPY src /app/src

# The runtime writes only session state, so it does not need root.
RUN useradd --create-home --uid 10001 orchestrator \
    && mkdir -p ${PRODUCTION_ORCHESTRATOR_SESSIONS} \
    && chown -R orchestrator:orchestrator /app ${PRODUCTION_ORCHESTRATOR_SESSIONS}
USER orchestrator

EXPOSE 8080

# Provider is passed at deploy time; the judged path is:
#   --provider bedrock-workflow --model amazon.nova-lite-v1:0
#   --aws-region <region> --aws-credential-source container-role
CMD ["python", "-m", "production_orchestrator.agentcore_app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--root", "/tmp/production-orchestrator-sessions"]
