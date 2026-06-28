FROM python:3.10-slim

WORKDIR /app

# Cài đặt thư viện trước để tận dụng Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]