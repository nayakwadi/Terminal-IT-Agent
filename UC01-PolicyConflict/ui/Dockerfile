# Build from the UC01-PolicyConflict/ directory so sample-tickets/ is reachable:
#   docker build -f ui/Dockerfile -t policyconflict-ui .
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

COPY ui/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ui/app.py .
COPY sample-tickets ./sample-tickets

EXPOSE 8080

# Plain HTTP form-post UI — no WebSockets, fully App Runner / Envoy compatible.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
