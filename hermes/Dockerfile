FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py .

# Run python with unbuffered output so logs print immediately to docker-compose logs
CMD ["python", "-u", "agent.py"]
