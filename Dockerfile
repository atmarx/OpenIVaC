FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# System deps for ffmpeg subtitle burning and video encoding
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default: run all videos headless
CMD ["python", "run.py"]
