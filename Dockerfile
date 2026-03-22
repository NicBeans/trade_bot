# Build stage
FROM python:3.12-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Runtime stage
FROM python:3.12-slim

WORKDIR /app

# Copy installed packages
COPY --from=builder /install /usr/local

# Copy application
COPY . .

# Create logs directory
RUN mkdir -p /app/logs

EXPOSE 8080

# Use exec form for proper signal handling
ENTRYPOINT ["python", "main.py"]
