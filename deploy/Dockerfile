FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml ./
RUN pip install --no-cache-dir ".[postgres]" || pip install --no-cache-dir \
    fastapi "uvicorn[standard]" "sqlalchemy>=2.0" alembic pydantic pydantic-settings \
    jinja2 python-multipart apscheduler httpx passlib itsdangerous "psycopg[binary]"

COPY . .
RUN pip install --no-cache-dir -e . || true

EXPOSE 8000
CMD ["uvicorn", "central.main:app", "--host", "0.0.0.0", "--port", "8000"]
