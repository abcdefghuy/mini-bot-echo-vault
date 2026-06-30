FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY scraper/ scraper/
COPY uploader/ uploader/
COPY utils/ utils/
COPY main.py .

# Run the pipeline
ENTRYPOINT ["python", "main.py"]
