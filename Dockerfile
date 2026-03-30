FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir flask requests mutagen SpotiFLAC

COPY entrypoint.sh app.py downloader.py ./
COPY templates/ templates/

RUN chmod +x entrypoint.sh

EXPOSE 7171
CMD ["./entrypoint.sh"]
