FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY plex_mal_sync.py .
ENV CONFIG_PATH=/data/config.json
EXPOSE 5057
CMD ["python3", "plex_mal_sync.py"]
