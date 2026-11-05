"""Prepare and run the web app, API and sequential durable processing worker."""
import argparse
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / '.venv/bin/python'


def load_environment():
    env = os.environ.copy()
    file = ROOT / '.env'
    if file.exists():
        for line in file.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            key, sep, value = line.partition('=')
            if sep:
                env.setdefault(key.strip(), value.strip().strip('\"').strip("'"))
    return env


def run(args, env, cwd=ROOT):
    subprocess.run([str(x) for x in args], cwd=cwd, env=env, check=True)


def check_port(port):
    with socket.socket() as sock:
        sock.settimeout(1)
        if sock.connect_ex(('127.0.0.1', port)) == 0:
            raise SystemExit(f'Port {port} is already in use. Stop that local service before starting this launcher.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--preview', action='store_true', help='Build and serve a stable preview on port 4173')
    args = parser.parse_args()
    if not PYTHON.exists():
        raise SystemExit('Create .venv with Python 3.12+ and install requirements.lock first. See README.md.')
    env = load_environment()
    if len(env.get('DEMO_PASSWORD', '')) < 12:
        raise SystemExit('Set DEMO_PASSWORD (12+ characters) in .env for local demo accounts.')
    if not args.prepare_only:
        check_port(8000)
        check_port(4173 if args.preview else 5173)
    if not (ROOT / 'backend/private-media/demo-process.mp4').exists():
        run([PYTHON, 'scripts/generate_demo_video.py'], env)
    run([PYTHON, 'manage.py', 'migrate', '--noinput'], env, ROOT / 'backend')
    run([PYTHON, 'manage.py', 'seed_demo'], env, ROOT / 'backend')
    if args.prepare_only:
        return
    npm = shutil.which('npm')
    if not npm or not (ROOT / 'frontend/node_modules').exists():
        raise SystemExit('Install Node.js 24+ and run npm ci in frontend first.')
    if args.preview:
        run([npm, 'run', 'build'], env, ROOT / 'frontend')
    processes = []
    try:
        processes.append(subprocess.Popen([str(PYTHON), 'manage.py', 'runserver', '127.0.0.1:8000', '--noreload'], cwd=ROOT / 'backend', env=env))
        processes.append(subprocess.Popen([str(PYTHON), 'manage.py', 'processing_worker'], cwd=ROOT / 'backend', env=env))
        web_args = [npm, 'run', 'preview', '--', '--port', '4173'] if args.preview else [npm, 'run', 'dev']
        processes.append(subprocess.Popen(web_args, cwd=ROOT / 'frontend', env=env))
        port = 4173 if args.preview else 5173
        print(f'\nОпыт мастера: http://127.0.0.1:{port} · API + web + processing worker · Ctrl+C stops all services.\n', flush=True)
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for process in processes:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    main()
