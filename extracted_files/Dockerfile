FROM python:3.11-slim

WORKDIR /app

COPY extracted_files/Quizbot-main/requirements.txt requirements.txt

RUN pip install --no-cache-dir -r requirements.txt

COPY extracted_files/Quizbot-main/ .

CMD ["python", "run.py"]
