FROM python:3.12-slim

# Все зависимости закреплены (requirements.txt): образ повторяет проверенную сборку.
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

# Внутренний корневой сертификат Jira/Confluence, если он не публичный:
# COPY corp-ca.crt /usr/local/share/ca-certificates/corp-ca.crt
# RUN update-ca-certificates

COPY server.py /app/
WORKDIR /app
ENV TRANSPORT=streamable-http HOST=0.0.0.0 PORT=8000 HOME=/tmp
EXPOSE 8000
USER nobody
CMD ["python", "server.py"]
