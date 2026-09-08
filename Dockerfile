FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV FASTERP_DB=/data/fasterp.sqlite
ENV FASTERP_PORT=5011
EXPOSE 5011 5012
CMD ["sh", "-c", "if [ -n \"$DB_URL\" ]; then python scripts/migrate_postgres.py; fi && python web_app.py"]
