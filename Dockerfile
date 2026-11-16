FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY parcelaudit ./parcelaudit
RUN pip install --no-cache-dir '.[web,llm]'
EXPOSE 8000
CMD ["uvicorn", "parcelaudit.web:app", "--host", "0.0.0.0", "--port", "8000"]
