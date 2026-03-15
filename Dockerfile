FROM python:3.11-slim

# Install Node.js (required for PO token generation)
RUN apt-get update && apt-get install -y \
    curl \
    gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install PO token plugin
RUN pip install bgutil-ytdlp-pot-provider

# Copy the rest of the application
COPY . .

# Make start script executable
RUN chmod +x start.sh

# Expose ports (5000 for Flask, 4416 for PO token server)
EXPOSE 5000 4416

# Start both services
CMD ./start.sh
