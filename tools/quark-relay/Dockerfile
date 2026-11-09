FROM python:3.12-alpine
WORKDIR /app
COPY relay.py /app/relay.py
ENV PYTHONUNBUFFERED=1 LISTEN_PORT=8080
EXPOSE 8080
USER nobody
CMD ["python", "/app/relay.py"]
