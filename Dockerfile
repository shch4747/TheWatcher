FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project

COPY shared ./shared
COPY agents ./agents
COPY scripts ./scripts
# Not used by ingestion any more (ADR-0012: its prompts are code
# constants), but the loader and the benchmark still read this dir -
# and a silently-empty skills/ is how the old pipeline ended up running
# with no system prompts at all in the container.
COPY skills ./skills
COPY main.py ./main.py
RUN uv sync --no-dev

EXPOSE 8000
CMD ["uv", "run", "python", "main.py"]
