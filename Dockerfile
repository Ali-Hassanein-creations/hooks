FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY alembic.ini ./
COPY migrations ./migrations
RUN useradd --create-home app
USER app
EXPOSE 8000
CMD ["uvicorn", "webhooks.main:app", "--host", "0.0.0.0", "--port", "8000"]
