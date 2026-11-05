"""Explicit live check, without opening or changing the application database."""
import argparse
import os
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import load_env
from groq_chat import DEFAULT_MODEL, GroqChat

parser = argparse.ArgumentParser(description='Проверка Groq без вывода ключа')
parser.add_argument('--live', action='store_true', help='Разрешить один реальный запрос в Groq')
args = parser.parse_args()
load_env()
key = os.getenv('GROQ_API_KEY', '').strip()
if not key:
    print('GROQ_API_KEY не настроен. Доступны локальные ответы.')
    raise SystemExit(1)
print('Ключ настроен. Значение не выводится.')
if not args.live:
    print('Для проверки ответа запустите с --live. Это расходует квоту Groq.')
    raise SystemExit(0)
chat = GroqChat(key, os.getenv('GROQ_MODEL', DEFAULT_MODEL))
try:
    result = chat.answer('Объясни простыми словами, что такое алгоритм, в двух предложениях.')
    if result['mode'] != 'groq':
        print('Реальный ответ не получен:', result.get('fallback_reason', result['mode']))
        print(result.get('notice', 'Проверьте настройки.'))
        raise SystemExit(1)
    if not re.search('[А-Яа-яЁё]', result['message']):
        print('Ответ получен, но русский текст не обнаружен.')
        raise SystemExit(1)
    print('Получен реальный ответ Groq на русском. Модель:', result['model'])
finally:
    chat.close()
