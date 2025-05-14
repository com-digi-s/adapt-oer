FROM python:3.9

RUN git clone -b feature/docker --single-branch https://github.com/com-digi-s/adapt-oer.git
RUN mkdir /app; mv adapt-oer/* /app/
WORKDIR /app

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 5000

CMD ["python", "app.py"]
