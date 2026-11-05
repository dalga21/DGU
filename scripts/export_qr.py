"""Create a printable QR set. Defaults mirror the five demo locations."""
import argparse
import sqlite3
from pathlib import Path
from urllib.parse import urlparse,quote
import qrcode

parser=argparse.ArgumentParser(description='Экспорт QR-кодов УПК')
parser.add_argument('--base-url',default='http://localhost:8000')
parser.add_argument('--output',default='docs/qr')
parser.add_argument('--database',help='Существующая SQLite-база, если нужны изменённые помещения')
args=parser.parse_args()
base=args.base_url.rstrip('/')
if urlparse(base).scheme not in ('http','https') or not urlparse(base).netloc: parser.error('Нужен полный HTTP(S)-адрес приложения')
locations=[('room-214','214 — Учебная аудитория'),('lab-305','305 — Компьютерная лаборатория'),('hall-1','Холл — Общая зона'),('library-208','208 — Библиотека'),('room-112','112 — Учебная аудитория')]
if args.database:
 path=Path(args.database).resolve()
 if not path.is_file(): parser.error('База не найдена')
 with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
  locations=[(key,room+' — '+zone) for key,room,zone in db.execute('SELECT qr_key,room,zone_name FROM locations ORDER BY id')]
out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
lines=['# Демонстрационные QR-коды','',f'Адрес приложения: `{base}`.','', 'Для смартфона используйте доступный ему сетевой адрес и пересоздайте набор. Коды содержат только ключ помещения, без персональных данных.','']
for key,label in locations:
 if any(ch not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for ch in key): raise ValueError('Некорректный QR-ключ')
 url=base+'/#/new?location='+quote(key,safe='')
 qrcode.make(url).save(out/(key+'.png'))
 lines.extend(['## '+label,'',f'![QR: {label}]({key}.png)','',f'[Открыть форму]({url})',''])
(out/'README.md').write_text('\n'.join(lines),encoding='utf-8')
print(f'Сохранено QR-кодов: {len(locations)}. Папка: {out}')
