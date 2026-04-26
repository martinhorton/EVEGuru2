FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir psycopg2-binary

COPY loader.py .

CMD ["python", "loader.py"]
