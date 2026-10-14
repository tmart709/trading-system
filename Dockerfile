FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN useradd --create-home --uid 1000 trader
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=trader:trader . .
RUN mkdir -p /app/db /app/logs && chown -R trader:trader /app/db /app/logs
USER trader

ENTRYPOINT ["python", "cli.py"]
CMD ["record"]
