FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
RUN python -c 'import subprocess, sys, tomllib; deps = tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]; subprocess.check_call([sys.executable, "-m", "pip", "install", "--no-cache-dir", *deps])'
ENV PORT=8080 APP_DIR=/mnt/grants PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "/mnt/grants/app.py"]
