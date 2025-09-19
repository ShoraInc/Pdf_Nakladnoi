# Use Python 3.13 slim image
FROM python:3.13-slim

# Install system dependencies including poppler
RUN apt-get update && apt-get install -y \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create temp directory
RUN mkdir -p /tmp/pdf_bot

# Run the bot
CMD ["python", "pdf_bot.py"]
