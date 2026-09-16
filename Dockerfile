FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# HEADLESS deve ser "true" em produção (imagem não tem display gráfico)
ENV HEADLESS=true

CMD ["python", "bot/main.py"]
