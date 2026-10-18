FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk fontconfig curl libgomp1 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir 'mineru==4.0.11'
ENV MINERU_HOME=/var/lib/mineru MINERU_MODEL_SOURCE=local
ENTRYPOINT ["mineru-kit"]
CMD ["api-server", "--host", "0.0.0.0", "--port", "8000", "--tier", "standard", "--concurrency", "1", "--upload-dir", "/var/lib/mineru/uploads"]
