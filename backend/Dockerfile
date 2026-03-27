FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --global user.email "agent@autonomous.local" \
    && git config --global user.name "Autonomous Agent" \
    && git config --global init.defaultBranch main

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

COPY src/ ./src/

EXPOSE 8000
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
