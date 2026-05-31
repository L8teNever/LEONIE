FROM python:3.12-slim

LABEL maintainer="redirect-server"
LABEL description="Lightweight HTTP redirect server"

# Arbeitsverzeichnis
WORKDIR /app

# Abhängigkeiten installieren
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Anwendungscode kopieren
COPY server.py .

# Config wird per Volume eingebunden (siehe docker-compose.yml)
# Konfiguration kann also zur Laufzeit geändert werden

# Port freigeben (muss mit config.yml übereinstimmen)
EXPOSE 8080

# Server starten
CMD ["python", "server.py", "--config", "/config/config.yml"]
