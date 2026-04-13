FROM python:3.12-slim

WORKDIR /app

# System deps for psycopg2-binary
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tools/ tools/
COPY templates/ templates/
COPY migrations/ migrations/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

WORKDIR /app/tools

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "telegram_bot.py"]
