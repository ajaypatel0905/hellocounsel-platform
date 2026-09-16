FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY pyproject.toml README.md ./
COPY counsel ./counsel
RUN pip install -q .
ENV PORT=8080 DB_PATH=/data/counsel.db
VOLUME ["/data"]
EXPOSE 8080
CMD ["sh", "-c", "uvicorn counsel.api:create_app --factory --host 0.0.0.0 --port ${PORT}"]
