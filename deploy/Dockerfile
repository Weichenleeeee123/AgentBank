FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY requirements.lock ./
COPY backend ./backend
COPY frontend ./frontend
COPY evals ./evals
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps .

ENV PYTHONPATH=/app/backend
EXPOSE 8000
CMD ["uvicorn", "agentbank.api.run:app", "--host", "0.0.0.0", "--port", "8000"]
