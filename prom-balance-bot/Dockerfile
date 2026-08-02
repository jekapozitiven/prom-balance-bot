FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Браузер уже есть в образе Playwright; на всякий случай гарантируем chromium
RUN playwright install chromium

CMD ["python", "bot.py"]
