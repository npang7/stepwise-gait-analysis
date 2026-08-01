FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV MPLBACKEND=Agg

COPY requirements.txt .
RUN python -m pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt --default-timeout=120

COPY stepwise_gait_analysis.py ./
COPY stepwise_reference_pipeline.py ./
COPY stepwise_cloudrun_flask/app.py ./app.py

EXPOSE 80

CMD ["gunicorn", "-b", "0.0.0.0:80", "app:app", "--timeout", "180"]
