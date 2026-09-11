# Playwright's own image: it ships Chromium plus every system library the
# browser needs. Building on python:slim means chasing ~40 apt packages by hand.
FROM mcr.microsoft.com/playwright/python:v1.62.0-jammy

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The browser is already in the base image; this just confirms the binary the
# pinned playwright version expects is present.
RUN python -c "from playwright.sync_api import sync_playwright; \
    p=sync_playwright().start(); print('chromium:', p.chromium.executable_path); p.stop()"

ENV PYTHONUNBUFFERED=1
EXPOSE 8012

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8012"]
