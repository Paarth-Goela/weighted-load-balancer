# Build context: the load-balancer/ repository root (see docker-compose.yml),
# so the Milestone 2 runtime module can be reused without duplication.

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/lib
WORKDIR /app

COPY traffic-manager/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# uid/gid 99 = the 'haproxy' user in the HAProxy image, so this process (and
# nothing else) can open the mode-600 Runtime API socket.
RUN groupadd -g 99 lbadmin \
 && useradd -u 99 -g 99 -M -d /app -s /usr/sbin/nologin trafficmgr \
 && mkdir -p /app/data /app/lib /run/haproxy-runtime \
 && chown 99:99 /app/data /run/haproxy-runtime

COPY runtime/haproxy_runtime.py /app/lib/haproxy_runtime.py
COPY traffic-manager/app /app/app

USER 99:99
EXPOSE 9000
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "9000", "--workers", "1"]

# ------------------------------------------------------------------- test
# docker build -f traffic-manager/Dockerfile --target test -t traffic-manager-test .
# docker run --rm traffic-manager-test
FROM runtime AS test
USER root
RUN apt-get update && apt-get install -y --no-install-recommends openssl \
 && rm -rf /var/lib/apt/lists/*
COPY traffic-manager/requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY traffic-manager/pytest.ini /app/pytest.ini
COPY traffic-manager/tests /app/tests
USER 99:99
CMD ["pytest", "-q", "-m", "not integration"]
