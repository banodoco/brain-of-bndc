# Python 3.11 required by vibecomfy (support-agent workflow tools)
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies required by the bot
# - libgl1: Required for OpenCV (opencv-python-headless)
# - libglib2.0-0: Required for OpenCV
# - ffmpeg: Required for moviepy video processing
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --no-deps \
    "vibecomfy @ git+https://github.com/peteromallet/VibeComfy.git@ddac29416ed6b08828cd75cb3f36b6b5a592d224"

# Copy the entire application
COPY . .

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Railway provides the PORT environment variable
# The bot will listen on this port for webhooks (if configured)
EXPOSE 8080

# Run the bot
CMD ["python", "main.py"]



