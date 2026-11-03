FROM python:3.9

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Compose retains this volume on ordinary image updates without host changes.
# Explicit existing bind/named mounts at this path continue to take precedence.
VOLUME ["/app/instance"]

EXPOSE 5000

ARG VCS_REF=unknown
LABEL org.opencontainers.image.source="https://github.com/com-digi-s/adapt-oer" \
      org.opencontainers.image.revision="${VCS_REF}"

CMD ["python", "app.py"]
