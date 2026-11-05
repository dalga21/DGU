"""Check the Render start command against an isolated database; never deploys."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import load_env


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='Один реальный запрос Groq через Gunicorn')
    args = parser.parse_args()
    load_env()
    binary = Path(sys.executable).with_name('gunicorn')
    if not binary.is_file():
        found = shutil.which('gunicorn')
        if not found: raise SystemExit('Gunicorn не найден. Установите requirements.txt.')
        binary = Path(found)
    if args.live and not os.getenv('GROQ_API_KEY', '').strip():
        raise SystemExit('Для --live нужен GROQ_API_KEY в окружении или локальном .env.')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='upk-gunicorn-') as tmp:
        env = {**os.environ, 'PORT': str(port), 'UPK_DATA_DIR': tmp,
               'PUBLIC_BASE_URL': f'http://127.0.0.1:{port}', 'COOKIE_SECURE': '0'}
        if not args.live: env['GROQ_API_KEY'] = ''
        with open(Path(tmp)/'gunicorn.log', 'w+') as log:
            proc = subprocess.Popen([str(binary), 'app:create_app()', '--bind', '0.0.0.0:'+str(port)],
                                    cwd=ROOT, env=env, stdout=log, stderr=log)
            base = f'http://127.0.0.1:{port}'
            try:
                deadline = time.monotonic()+15
                while time.monotonic() < deadline:
                    if proc.poll() is not None: raise RuntimeError('Gunicorn завершился до открытия порта.')
                    try:
                        with urllib.request.urlopen(base+'/api/meta', timeout=1) as response:
                            meta = json.load(response)
                        break
                    except (urllib.error.URLError, TimeoutError): time.sleep(.1)
                else: raise RuntimeError('Gunicorn не открыл порт за 15 секунд.')
                assert meta['ai_mode'] == ('groq' if args.live else 'demo')
                with urllib.request.urlopen(base, timeout=2) as response: assert response.status == 200
                req = urllib.request.Request(base+'/api/assistant', method='POST',
                    data=json.dumps({'text':'Объясни студенту, что такое алгоритм, в двух предложениях.'}).encode(),
                    headers={'Content-Type':'application/json','X-UPK-Request':'1','Origin':base})
                with urllib.request.urlopen(req, timeout=25) as response:
                    answer = json.load(response)
                assert answer['mode'] == ('groq' if args.live else 'demo'), answer.get('fallback_reason', answer['mode'])
                assert re.search('[А-Яа-яЁё]', answer['message'])
                try: urllib.request.urlopen(base+'/api/tickets', timeout=2)
                except urllib.error.HTTPError as error: assert error.code == 401
                else: raise AssertionError('Панель заявок не защищена')
                assert Path(tmp, 'upk.sqlite3').is_file()
                key = os.getenv('GROQ_API_KEY', '')
                log.flush(); log.seek(0)
                if key: assert key not in log.read()
                print('Gunicorn app:create_app() --bind 0.0.0.0:$PORT: OK')
                print('Главная: 200; защита заявок: 401; чат:', answer['mode'], '; ответ по-русски: OK.')
                print('Использована временная база. Рабочая база не изменялась.')
            finally:
                proc.terminate()
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=3)


if __name__ == '__main__': main()
