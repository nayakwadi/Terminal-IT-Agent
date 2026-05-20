FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

COPY requirements-agent.txt .
RUN pip install --no-cache-dir -r requirements-agent.txt

COPY itopsorchestrator_demo_uc01.py agentcore_app.py ./

EXPOSE 8080

CMD ["python", "agentcore_app.py"]
