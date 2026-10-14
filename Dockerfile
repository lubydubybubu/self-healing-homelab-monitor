FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY homelab_monitor ./homelab_monitor
COPY demo ./demo
RUN pip install --no-cache-dir . \
 && useradd --system --no-create-home monitor
USER monitor
ENTRYPOINT ["homelab-monitor"]
