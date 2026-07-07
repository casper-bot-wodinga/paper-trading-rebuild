FROM python:3.12-slim

RUN pip install --no-cache-dir \
    flask \
    psycopg2-binary \
    python-dotenv

ENV PYTHONUNBUFFERED=1

COPY . /app/
WORKDIR /app
EXPOSE 5004

CMD ["python3", "src/pg_dashboard.py"]
