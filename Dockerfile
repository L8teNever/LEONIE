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

# Port freigeben (muss mit der Umgebungsvariable übereinstimmen)
EXPOSE 8080

# Server starten
CMD ["python", "server.py"]
