FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY core.py server.py oauth_login.py template_xlsx.py static.css ./
RUN useradd --system --home /app --shell /usr/sbin/nologin wismail \
    && mkdir -p /app/data \
    && chown -R wismail:wismail /app
USER wismail
EXPOSE 8765
CMD ["python", "server.py"]
