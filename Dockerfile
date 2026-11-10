FROM python:3.12-slim AS base
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

FROM base AS test
COPY requirements-test.txt .
RUN pip install --no-cache-dir -r requirements-test.txt
COPY tests ./tests
COPY pytest.ini ./pytest.ini
RUN pytest -q

FROM base AS runtime
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
