FROM python:3.11-slim

# Chrome + Xvfb + system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates unzip \
    xvfb \
    fonts-liberation fonts-dejavu-core \
    libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 \
    libcairo2 libcups2 libdbus-1-3 libdrm2 libgbm1 \
    libglib2.0-0 libgtk-3-0 libnspr4 libnss3 \
    libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 \
    libxext6 libxfixes3 libxkbcommon0 libxrandr2 \
    libxss1 libxtst6 \
    && wget -q -O /tmp/chrome.deb \
       https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y /tmp/chrome.deb \
    && rm /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
ENV DISPLAY=:99
ENV STORAGE_ROOT=/app/storage

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

RUN mkdir -p /app/storage/deployments /app/storage/pella_state

EXPOSE 8000

# Start Xvfb, then uvicorn
CMD Xvfb :99 -screen 0 1280x900x24 -nolisten tcp & \
    sleep 2 && \
    uvicorn main:app --host 0.0.0.0 --port 8000