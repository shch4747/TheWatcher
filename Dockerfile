FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project

COPY shared ./shared
COPY agents ./agents
RUN uv sync --no-dev

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "shared.gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
