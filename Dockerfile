FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY atlasfinder_sniper.py .
CMD ["python", "-u", "atlasfinder_sniper.py"]