# Green-screen live server: browser green screen + MCP streamable HTTP (/mcp) on one TN3270 session.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . \
    && useradd --create-home --uid 10001 app \
    && rm -rf /src

USER 10001
WORKDIR /home/app

# Behind a TLS-terminating proxy (Azure Container Apps): trust X-Forwarded-Proto so the page's
# Origin check sees https.
ENV FORWARDED_ALLOW_IPS=*

EXPOSE 3271
CMD ["green-screen-live", "--host", "0.0.0.0", "--port", "3271"]
