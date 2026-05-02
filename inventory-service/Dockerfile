FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY common /app/common
COPY data /app/data
COPY inventory-service /app/inventory-service

RUN mkdir -p /tmp/eci/inventory
EXPOSE 8000
CMD ["uvicorn", "main:app", "--app-dir", "/app/inventory-service/app", "--host", "0.0.0.0", "--port", "8000"]
