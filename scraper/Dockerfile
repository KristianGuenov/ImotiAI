FROM mcr.microsoft.com/playwright/python:v1.57.0-jammy

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy ONLY the scraper package/code (because build context is ./scraper)
COPY . /app/scraper

# Make /app the import root so "python -m scraper.runner" works
ENV PYTHONPATH=/app

# Keep working dir at /app so relative paths (if any) resolve near /app/targets.yml and /app/content.js
WORKDIR /app

CMD ["python", "-m", "scraper.runner"]
