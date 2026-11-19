FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (libatomic1 is strictly required by playwright's node runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    ca-certificates \
    libatomic1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium

COPY . .

# Universal port: Render provides PORT (e.g. 10000), Hugging Face uses 7860
ENV PORT=7860
EXPOSE 7860

CMD ["python", "app.py"]
