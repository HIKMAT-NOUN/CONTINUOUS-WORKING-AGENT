FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY relay.py .
ENV RELAY_HOST=0.0.0.0 RELAY_PORT=8900
EXPOSE 8900
CMD ["python", "relay.py"]
