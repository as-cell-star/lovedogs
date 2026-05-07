# Use an official Python runtime as a parent image
FROM python:3.11-slim
# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# Set the working directory in the container
WORKDIR /app
# Install system dependencies
# libglib2.0-0 and libgl1 are often needed for OpenCV even in headless mode
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libglib2.0-0 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*
# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Copy the rest of the application code
COPY . .
# Expose the port the app runs on
EXPOSE 8000
# Command to run the application using the PORT environment variable (Railway assigns this dynamically)
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
