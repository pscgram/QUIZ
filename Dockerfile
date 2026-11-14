
FROM python:3.11-slim

# System dependencies required for WeasyPrint and PyMuPDF
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libcairo2 \
    libffi-dev \
    shared-mime-info \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Application directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy complete bot source code
COPY . .

# Create data directory
RUN mkdir -p /app/data

# Application port
EXPOSE 8080

# Start PSCGram Quizbot
CMD ["python", "run.py"]
