# syntax=docker/dockerfile:1.7
# Backend image (API, worker, migrations and admin CLI share one codebase/image).

FROM python:3.13.16-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
RUN pip install uv==0.8.21
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13.16-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
# pip/ensurepip are not needed at runtime (and vendor their own urllib3/msgpack copies).
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
      /usr/local/lib/python3.13/ensurepip /usr/local/bin/pip /usr/local/bin/pip3* \
 && groupadd --system --gid 10001 pulseops \
 && useradd --system --uid 10001 --gid pulseops --no-create-home --shell /usr/sbin/nologin pulseops \
 && mkdir -p /var/lib/pulseops/ledger && chown -R pulseops:pulseops /var/lib/pulseops
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY alembic.ini ./
COPY migrations ./migrations
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "pulseops.api.serve"]
