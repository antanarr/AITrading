FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements-autopilot.txt .
RUN pip install --no-cache-dir -r requirements-autopilot.txt
COPY copybot/__init__.py copybot/autopilot.py copybot/hyperliquid.py copybot/models.py copybot/perps.py copybot/perp_ledger.py ./copybot/
CMD ["python", "-m", "copybot.autopilot", "scan", "--interval", "300"]
