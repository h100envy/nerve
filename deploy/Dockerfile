FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir .
RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "nerve.cli", "run"]
