FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    iputils-ping \
    dnsutils \
    traceroute \
    curl \
    iproute2 \
    nmap \
    wireguard-tools \
    iptables \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY netcheck.py .

EXPOSE 8090

HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8090/health || exit 1

CMD ["python", "netcheck.py", "--serve"]
