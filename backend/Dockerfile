FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app

# Runs as a plain user, and never writes to its own files, so Kubernetes can mount them read-only.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin easydocs
USER 10001

EXPOSE 8000
# Only cloudflared (or, locally, this machine) can reach the app, so trust its forwarded headers:
# that's how the sign-in throttle sees each visitor's own address.
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
