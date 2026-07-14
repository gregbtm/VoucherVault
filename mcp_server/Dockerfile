FROM python:3.12.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /opt/mcp

COPY requirements.txt /opt/mcp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY client.py server.py /opt/mcp/

RUN chown -R www-data:www-data /opt/mcp

# run container as low privileged user
USER www-data

EXPOSE 8100

CMD ["python", "server.py"]
