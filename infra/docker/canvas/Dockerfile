FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8777
CMD ["gunicorn", "--bind", "0.0.0.0:8777", "--workers", "2", "app:app"]
