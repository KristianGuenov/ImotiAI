FROM mcr.microsoft.com/playwright/python:v1.57.0-jammy

WORKDIR /app

# Install Python deps (pin playwright==1.57.0 to match this image)
COPY scraper/requirements.txt /app/scraper/requirements.txt
RUN pip install --no-cache-dir -r /app/scraper/requirements.txt

# Copy scraper package
COPY scraper /app/scraper

# Ensure `python -m scraper.detail_runner` works
ENV PYTHONPATH=/app

CMD ["python", "-m", "scraper.runner"]
