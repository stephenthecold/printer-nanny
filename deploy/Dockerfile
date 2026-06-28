FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# msitools (msiinfo/msiextract) + wixl let central build a real Windows .msi for
# the agent on demand (central/msi_builder.py). Installed in its own early layer
# so it's cached across app-code changes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends wixl msitools \
    && rm -rf /var/lib/apt/lists/*

# Install deps first for layer caching.
COPY pyproject.toml ./
RUN pip install --no-cache-dir ".[postgres]" || pip install --no-cache-dir \
    fastapi "uvicorn[standard]" "sqlalchemy>=2.0" alembic pydantic pydantic-settings \
    jinja2 python-multipart apscheduler httpx passlib itsdangerous "psycopg[binary]"

COPY . .
RUN pip install --no-cache-dir -e . || true

EXPOSE 8000
CMD ["uvicorn", "central.main:app", "--host", "0.0.0.0", "--port", "8000"]
