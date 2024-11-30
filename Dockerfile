# Use an official Python runtime as a base image
FROM python:3.9-slim

# Install system dependencies for libtorrent
RUN apt-get update && apt-get install -y \
    libtorrent-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy the requirements.txt file
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose the port the app runs on (optional, if your app serves over HTTP)
EXPOSE 8080

# Run the bot script when the container starts
CMD ["python", "bot.py"]
