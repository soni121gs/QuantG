FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Install runtime dependencies only. The build uses standard wheels, so no system toolchain is required.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy app sources
COPY . /app

# Create a non-root user and give ownership of the app directory
RUN mkdir -p /data \
    && groupadd -r app && useradd -r -g app -d /home/app -s /usr/sbin/nologin -m app \
    && chown -R app:app /app /data

# Use non-root user for running the app
USER app

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
