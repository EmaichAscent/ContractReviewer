FROM python:3.12-slim

# Install LibreOffice for .doc file conversion
RUN apt-get update && \
    apt-get install -y --no-install-recommends libreoffice-writer && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create necessary directories
RUN mkdir -p uploads results data data/reference_contracts

# Copy default data files to a seed directory (volume mounts over /app/data)
RUN cp -r data /app/data_defaults

EXPOSE ${PORT:-5000}

CMD python seed_data.py && gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 1 --threads 2 --timeout 600 app:app
