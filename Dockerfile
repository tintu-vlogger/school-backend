FROM python:3.12-slim

WORKDIR /app

# Pango and fonts for server-side PDF letters (WeasyPrint)
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN echo "cache-bust-$(date +%s)" > /tmp/.build-id

RUN chmod +x start.sh

CMD ["./start.sh"]
