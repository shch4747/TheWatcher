FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project

COPY shared ./shared
COPY agents ./agents
COPY main.py ./main.py
RUN uv sync --no-dev

EXPOSE 8000
CMD ["uv", "run", "python", "main.py"]
