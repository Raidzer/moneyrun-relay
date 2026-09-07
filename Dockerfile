FROM python:3.12.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY relay.py ./
RUN mkdir /app/.local

ENTRYPOINT ["python", "relay.py"]
CMD ["run", "--non-interactive"]
