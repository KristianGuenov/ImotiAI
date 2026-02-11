FROM mcr.microsoft.com/playwright/python:v1.57.0-jammy

# Add virtual X server so we can run --headful inside Docker via xvfb-run
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# requirements must be inside build context (./scraper)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy ONLY the scraper package/code (because build context is ./scraper)
COPY . /app/scraper

# Make /app the import root so "python -m scraper.runner" works
ENV PYTHONPATH=/app

WORKDIR /app

CMD ["python", "-m", "scraper.runner"]
