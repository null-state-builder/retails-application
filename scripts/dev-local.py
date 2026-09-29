"""Run the local API, worker and frontend; Ctrl-C stops these processes only."""

from pathlib import Path
import signal
import socket
import subprocess
import time
import os

root = Path(__file__).resolve().parent.parent
local = root / ".local"
python = root / "backend/.venv/bin/python"
vite = root / "frontend/node_modules/.bin/vite"
if not all(p.exists() for p in (python, vite, root / "backend/.env")):
    raise SystemExit("Run bash scripts/setup-local.sh first.")

for port in (8000, 5173):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            raise SystemExit(f"Port {port} is occupied; stop its server or choose another configuration.")
with socket.socket() as probe:
    if probe.connect_ex(("127.0.0.1", 55432)):
        raise SystemExit("Start the database: docker compose -f compose.local.yaml up -d --wait")

commands = [
    ("backend", root / "backend", [str(python), "-m", "uvicorn", "config.asgi:application", "--host", "127.0.0.1", "--port", "8000"]),
    ("worker", root / "backend", [str(python), "manage.py", "run_goods_worker"]),
    ("frontend", root / "frontend", [str(vite)]),
]
processes = []
logs = []

def interrupt(_signum, _frame):
    raise KeyboardInterrupt

signal.signal(signal.SIGTERM, interrupt)
try:
    for name, cwd, command in commands:
        log = (local / f"{name}.log").open("a")
        logs.append(log)
        process = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        processes.append((name, process))
    print("Starting KDPS at http://127.0.0.1:5173", flush=True)
    print(f"Logs: {local}\nCtrl-C stops the API, worker and frontend; database data is retained.", flush=True)
    while True:
        for name, process in processes:
            if process.poll() is not None:
                raise SystemExit(f"{name} exited ({process.returncode}); see {local / (name + '.log')}")
        time.sleep(1)
except KeyboardInterrupt:
    print("Stopping local application processes.", flush=True)
finally:
    for _, process in processes:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
    for _, process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    for log in logs:
        log.close()
