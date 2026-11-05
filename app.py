"""College service desk. Run: python app.py (Python 3.10+)."""
import base64
import csv
import functools
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import warnings
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import qrcode
from flask import Flask, g, jsonify, request, send_file, send_from_directory
from PIL import Image, UnidentifiedImageError
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from assistant import KNOWLEDGE, classify
from db import STATUSES, PRIORITIES, TRANSITIONS, close_db, digest, get_db, initialize, now

ROOT=Path(__file__).resolve().parent
Image.MAX_IMAGE_PIXELS=20_000_000

def load_env():
 path=ROOT/'.env'
 if path.exists():
  for line in path.read_text().splitlines():
   if '=' in line and not line.lstrip().startswith('#'):
    key,value=line.split('=',1); os.environ.setdefault(key.strip(),value.strip().strip('"\''))

def public_base_url():
 explicit=os.getenv('PUBLIC_BASE_URL','').rstrip('/')
 if explicit: return explicit
 codespace=os.getenv('CODESPACE_NAME'); domain=os.getenv('GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN')
 if codespace and domain: return f'https://{codespace}-{os.getenv("PORT","8000")}.{domain}'
 return request.host_url.rstrip('/')

class Problem(Exception):
 def __init__(self,message,status=400): self.message=message; self.status=status

def text_field(data,key,min_len=0,max_len=5000):
 label={'title':'Тема','description':'Описание','category_id':'Категория','requester_name':'Имя','email':'Email','telegram_chat_id':'Telegram chat ID','body':'Комментарий','note':'Результат','login':'Логин','password':'Пароль','text':'Сообщение','building':'Корпус','room':'Кабинет','zone_name':'Назначение помещения','qr_key':'QR-ключ','name':'Название','comment':'Комментарий к оценке'}.get(key,key)
 value=data.get(key,'')
 if not isinstance(value,str): raise Problem(f'Поле «{label}» должно быть текстом.')
 value=value.strip()
 if not min_len<=len(value)<=max_len: raise Problem(f'Поле «{label}»: введите от {min_len} до {max_len} символов.')
 return value

def integer(value,label='Идентификатор'):
 try:
  if isinstance(value,bool): raise ValueError()
  return int(str(value))
 except (ValueError,TypeError): raise Problem(f'{label}: ожидается целое число.')

def payload():
 data=request.get_json(silent=True)
 if not isinstance(data,dict): raise Problem('Ожидается JSON-объект.')
 return data

def protected(admin=False):
 def wrapper(fn):
  @functools.wraps(fn)
  def inner(*args,**kwargs):
   if not g.user: raise Problem('Войдите в учётную запись сотрудника.',401)
   if admin and g.user['role']!='admin': raise Problem('Нужны права администратора.',403)
   return fn(*args,**kwargs)
  return inner
 return wrapper

def normalized_photo(encoded):
 if not isinstance(encoded,str) or len(encoded)>7_000_000: raise Problem('Фото должно быть не больше 5 МБ.')
 try:
  raw=base64.b64decode(encoded.split(',')[-1],validate=True)
  if len(raw)>5*1024*1024: raise Problem('Фото должно быть не больше 5 МБ.')
  with warnings.catch_warnings():
   warnings.simplefilter('error', Image.DecompressionBombWarning)
   with Image.open(io.BytesIO(raw)) as im:
    if im.format not in ('JPEG','PNG','WEBP'): raise Problem('Поддерживаются только JPEG, PNG и WebP.')
    im.load()
    from PIL import ImageOps
    im=ImageOps.exif_transpose(im)
    if 'A' in im.getbands():
     bg=Image.new('RGB',im.size,'white'); bg.paste(im,mask=im.getchannel('A')); im=bg
    else: im=im.convert('RGB')
    im.thumbnail((1920,1920)); output=io.BytesIO(); im.save(output,format='JPEG',quality=85)
    return output.getvalue()
 except (ValueError,UnidentifiedImageError,OSError,Image.DecompressionBombError,Image.DecompressionBombWarning):
  raise Problem('Файл не является допустимым изображением или слишком велик.')

def create_app(test_config=None):
 load_env()
 app=Flask(__name__,static_folder='static',static_url_path='/static')
 app.config.update(DATA_DIR=str(ROOT/'.data'),DATABASE=str(ROOT/'.data/upk.sqlite3'),TESTING=False,SEED_DEMO=True,MAX_CONTENT_LENGTH=8*1024*1024)
 if test_config: app.config.update(test_config)
 directory=Path(app.config['DATA_DIR']); directory.mkdir(parents=True,exist_ok=True); directory.chmod(0o700)
 (directory/'uploads').mkdir(exist_ok=True)
 secret_path=directory/'server-secret'
 if not secret_path.exists():
  secret_path.write_text(secrets.token_hex(32)); secret_path.chmod(0o600)
 app.config['SECRET_KEY']=secret_path.read_text().strip()
 app.teardown_appcontext(close_db)
 initialize(app)
 app.config['DUMMY_HASH']=generate_password_hash(secrets.token_hex(16))
 limits=defaultdict(deque); limit_lock=threading.Lock()

 def rate(key,maximum=30,window=60):
  if app.config['TESTING']: return
  bucket=(request.remote_addr,key); stamp=time.monotonic()
  with limit_lock:
   while limits[bucket] and limits[bucket][0]<stamp-window: limits[bucket].popleft()
   if len(limits[bucket])>=maximum: raise Problem('Слишком много запросов. Подождите минуту и попробуйте снова.',429)
   limits[bucket].append(stamp)
   if len(limits)>10000:
    for k in list(limits):
     if not limits[k] or limits[k][-1]<stamp-3600: del limits[k]

 @app.before_request
 def security():
  g.user=None
  token=request.cookies.get('upk_session','')
  if token:
   user=get_db().execute('SELECT u.id,u.name,u.login,u.role FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1',(digest(token),now())).fetchone()
   if user: g.user=dict(user)
  if request.path.startswith('/api/') and request.method in ('POST','PUT','PATCH','DELETE'):
   if request.headers.get('X-UPK-Request')!='1': raise Problem('Недопустимый источник запроса.',403)
   origin=request.headers.get('Origin')
   if origin and origin not in {request.host_url.rstrip('/'),public_base_url()}: raise Problem('Межсайтовые запросы запрещены.',403)

 @app.after_request
 def headers(response):
  response.headers['X-Content-Type-Options']='nosniff'
  response.headers['Referrer-Policy']='no-referrer'
  response.headers['X-Frame-Options']='DENY'
  response.headers['Permissions-Policy']='camera=(), microphone=(), geolocation=()'
  response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
  if request.path.startswith('/api/'): response.headers['Cache-Control']='no-store'
  return response

 @app.errorhandler(Problem)
 def bad_request(error): return jsonify(error=error.message),error.status
 @app.errorhandler(HTTPException)
 def http_error(error): return jsonify(error='Файл слишком большой. Максимум фото — 5 МБ.' if error.code==413 else 'Запрос не выполнен.'),error.code
 @app.errorhandler(Exception)
 def unexpected(error):
  app.logger.exception('Unhandled server error')
  return jsonify(error='Не удалось выполнить действие. Данные не подтверждены; попробуйте ещё раз.'),500

 def row_ticket(where,args):
  return get_db().execute('''SELECT t.*,c.name category_name,c.icon category_icon,l.building,l.floor,l.room,l.zone_name,u.name assignee_name
   FROM tickets t JOIN categories c ON c.id=t.category_id JOIN locations l ON l.id=t.location_id LEFT JOIN users u ON u.id=t.assignee_id WHERE '''+where,args).fetchone()

 def access_ticket(tid):
  row=row_ticket('t.id=?',(tid,))
  if not row: raise Problem('Заявка не найдена или ссылка недействительна.',404)
  if not g.user:
   token=request.headers.get('X-Tracking-Token','')
   if not token or not hmac.compare_digest(digest(token),row['tracking_token_hash']): raise Problem('Заявка не найдена или ссылка недействительна.',404)
  return row

 def serialize_ticket(row,detail=False,public=False):
  item=dict(row)
  for key in ['tracking_token_hash','request_key_hash']: item.pop(key,None)
  if public:
   for key in ['email','telegram_chat_id','requester_name','assignee_id','assignee_name']: item.pop(key,None)
  if detail:
   db=get_db(); tid=item['id']
   comments=db.execute('SELECT id,author_name,body,visibility,created_at FROM comments WHERE ticket_id=?'+(" AND visibility='public'" if public else '')+' ORDER BY id',(tid,)).fetchall()
   item['comments']=[dict(x) for x in comments]
   events=db.execute('SELECT actor_name,event,old_status,new_status,note,created_at FROM ticket_history WHERE ticket_id=? ORDER BY id',(tid,)).fetchall()
   item['history']=[dict(x) for x in events if not public or x['event'] in ('created','status')]
   if public:
    for event in item['history']: event['actor_name']='Сотрудник УПК' if event['event']!='created' else 'Система'
   item['attachments']=[dict(x) for x in db.execute('SELECT id,file_type,size FROM attachments WHERE ticket_id=?',(tid,))]
   item['allowed_statuses']=TRANSITIONS[item['status']] if not public else []
  return item

 def history(tid,event,old=None,new=None,note=''):
  get_db().execute('INSERT INTO ticket_history(ticket_id,actor_id,actor_name,event,old_status,new_status,note,created_at) VALUES(?,?,?,?,?,?,?,?)',(tid,g.user['id'] if g.user else None,g.user['name'] if g.user else 'Заявитель',event,old,new,note,now()))

 def notifications(tid,message):
  db=get_db(); ticket=db.execute('SELECT * FROM tickets WHERE id=?',(tid,)).fetchone()
  for channel,recipient in [('email',ticket['email']),('telegram',ticket['telegram_chat_id'])]:
   if recipient:
    db.execute('INSERT INTO notifications(ticket_id,channel,destination,body,status,created_at) VALUES(?,?,?,?,?,?)',(tid,channel,recipient,ticket['public_number']+': '+message,'pending' if os.getenv('NOTIFICATIONS_ENABLED')=='1' else 'demo',now()))

 @app.get('/')
 def index(): return send_from_directory(app.static_folder,'index.html')
 @app.get('/api/meta')
 def meta():
  db=get_db()
  return jsonify(user=g.user,categories=[dict(x) for x in db.execute('SELECT id,name,icon FROM categories')],locations=[dict(x) for x in db.execute('SELECT * FROM locations ORDER BY building,floor,room')],statuses=STATUSES,priorities=PRIORITIES,knowledge=KNOWLEDGE,ai_mode='demo',photo_mode='connected' if all(os.getenv(x) for x in ('VISION_API_URL','VISION_API_KEY','VISION_MODEL')) else 'demo',notification_mode='configured' if os.getenv('NOTIFICATIONS_ENABLED')=='1' else 'demo',public_base_url=public_base_url())
 @app.post('/api/login')
 def login():
  rate('login',10); data=payload(); login=text_field(data,'login',1,80); password=text_field(data,'password',1,200)
  db=get_db(); user=db.execute('SELECT * FROM users WHERE login=? AND active=1',(login,)).fetchone()
  # Equal-cost hash check for absent logins; hashes are never returned to clients.
  password_hash=user['password_hash'] if user else app.config['DUMMY_HASH']
  if not check_password_hash(password_hash,password) or not user: raise Problem('Неверный логин или пароль.',401)
  token=secrets.token_urlsafe(32)
  db.execute('DELETE FROM sessions WHERE expires_at<?',(now(),))
  db.execute('INSERT INTO sessions VALUES(?,?,?)',(digest(token),user['id'],(datetime.now(timezone.utc)+timedelta(hours=8)).isoformat(timespec='seconds')))
  db.commit()
  response=jsonify(user={k:user[k] for k in ['id','name','login','role']})
  response.set_cookie('upk_session',token,httponly=True,samesite='Strict',secure=os.getenv('COOKIE_SECURE')=='1',max_age=28800)
  return response
 @app.post('/api/logout')
 def logout():
  db=get_db(); db.execute('DELETE FROM sessions WHERE token_hash=?',(digest(request.cookies.get('upk_session','')),)); db.commit()
  response=jsonify(ok=True); response.delete_cookie('upk_session'); return response
 @app.post('/api/assistant')
 def assistant():
  rate('assistant',40); message=text_field(payload(),'text',2,4000); answer=classify(message)
  db=get_db(); db.execute('INSERT INTO ai_runs(kind,category_id,created_at) VALUES(?,?,?)',(answer['kind'],answer['category'],now())); db.commit()
  return jsonify(answer)
 @app.post('/api/photo-analysis')
 def photo_analysis():
  rate('photo',5); data=payload(); photo=normalized_photo(data.get('photo'))
  if not all(os.getenv(x) for x in ('VISION_API_URL','VISION_API_KEY','VISION_MODEL')):
   return jsonify(mode='demo',description='',message='Фото принято, но демонстрационный помощник не распознаёт его содержимое. Опишите проблему текстом; сотрудник увидит фотографию в заявке.')
  from integrations import analyze_photo
  return jsonify(analyze_photo(photo))
 @app.post('/api/similar')
 def similar():
  rate('similar',30); data=payload(); location=integer(data.get('location_id')); cat=text_field(data,'category_id',1,60)
  # Public response only contains aggregate counts, never other people's descriptions or identifiers.
  count=get_db().execute("SELECT count(*) FROM tickets WHERE location_id=? AND category_id=? AND status NOT IN ('resolved','rejected')",(location,cat)).fetchone()[0]
  return jsonify(count=count,message=f'В этом помещении уже есть открытые обращения этой категории: {count}. Если это другая проблема или есть новые детали, отправьте свою заявку.' if count else '')
 @app.post('/api/tickets')
 def create_ticket():
  rate('create',12); data=payload(); key=text_field(data,'request_key',32,128)
  if not re.fullmatch(r'[a-zA-Z0-9_-]+',key): raise Problem('Некорректный ключ отправки.')
  token=base64.urlsafe_b64encode(hmac.new(app.config['SECRET_KEY'].encode(),key.encode(),hashlib.sha256).digest()).decode().rstrip('=')
  db=get_db()
  existing=db.execute('SELECT id,public_number FROM tickets WHERE request_key_hash=?',(digest(key),)).fetchone()
  if existing: return jsonify(id=existing['id'],public_number=existing['public_number'],token=token,replayed=True)
  title=text_field(data,'title',5,150); description=text_field(data,'description',10,4000); cat=text_field(data,'category_id',1,60); loc=integer(data.get('location_id'))
  requester=text_field(data,'requester_name',0,100); email=text_field(data,'email',0,200); telegram=text_field(data,'telegram_chat_id',0,40)
  if email and not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',email): raise Problem('Проверьте адрес электронной почты.')
  if telegram and not re.fullmatch(r'-?\d{5,20}',telegram): raise Problem('Telegram chat ID должен быть числовым, не @username.')
  if (email or telegram) and data.get('notification_consent') is not True: raise Problem('Подтвердите согласие на уведомления или удалите контакты.')
  category=db.execute('SELECT * FROM categories WHERE id=?',(cat,)).fetchone()
  if not category or not db.execute('SELECT id FROM locations WHERE id=?',(loc,)).fetchone(): raise Problem('Выберите существующую категорию и помещение.')
  analysis=classify(description); photo=normalized_photo(data['photo']) if data.get('photo') else None
  storage_key=secrets.token_hex(24)+'.jpg' if photo else None
  # Serialize idempotency check + write so simultaneous submissions cannot duplicate.
  try:
   db.execute('BEGIN IMMEDIATE')
   existing=db.execute('SELECT id,public_number FROM tickets WHERE request_key_hash=?',(digest(key),)).fetchone()
   if existing:
    db.rollback(); return jsonify(id=existing['id'],public_number=existing['public_number'],token=token,replayed=True)
   assignee=category['assignee_id']
   if assignee and not db.execute('SELECT id FROM users WHERE id=? AND active=1',(assignee,)).fetchone(): assignee=None
   stamp=now()
   cur=db.execute('INSERT INTO tickets(title,description,category_id,location_id,priority,created_at,updated_at,assignee_id,tracking_token_hash,request_key_hash,requester_name,email,telegram_chat_id,ai_kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(title,description,cat,loc,analysis['priority'],stamp,stamp,assignee,digest(token),digest(key),requester,email,telegram,analysis['kind']))
   tid=cur.lastrowid; number=f'УПК-{tid:05d}'
   db.execute('UPDATE tickets SET public_number=? WHERE id=?',(number,tid)); history(tid,'created',new='new')
   if assignee: history(tid,'assignment',note='Автоматическая маршрутизация по категории')
   if photo:
    (directory/'uploads'/storage_key).write_bytes(photo)
    db.execute('INSERT INTO attachments VALUES(?,?,?,?,?,?)',(secrets.token_hex(16),tid,storage_key,'image/jpeg',len(photo),stamp))
   notifications(tid,'Заявка зарегистрирована. Статус: Новая.'); db.commit()
  except Exception:
   db.rollback()
   if storage_key: (directory/'uploads'/storage_key).unlink(missing_ok=True)
   raise
  return jsonify(id=tid,public_number=number,token=token),201
 @app.get('/api/track')
 def track():
  rate('track',90); token=request.headers.get('X-Tracking-Token','')
  row=row_ticket('t.tracking_token_hash=?',(digest(token),)) if token else None
  if not row: raise Problem('Ссылка недействительна. Проверьте полный код доступа.',404)
  return jsonify(serialize_ticket(row,True,True))
 @app.get('/api/tickets/<int:tid>')
 @protected()
 def ticket_detail(tid): return jsonify(serialize_ticket(access_ticket(tid),True))
 @app.get('/api/attachments/<attachment_id>')
 def attachment(attachment_id):
  row=get_db().execute('SELECT * FROM attachments WHERE id=?',(attachment_id,)).fetchone()
  if not row: raise Problem('Фотография недоступна.',404)
  access_ticket(row['ticket_id'])
  return send_file(directory/'uploads'/row['storage_key'],mimetype='image/jpeg',download_name='attachment.jpg')
 @app.post('/api/tickets/<int:tid>/comments')
 def comment(tid):
  rate('comment',20); access_ticket(tid); data=payload(); body=text_field(data,'body',1,3000); visibility=data.get('visibility','public')
  if visibility not in ('public','internal') or (not g.user and visibility!='public'): raise Problem('Недопустимая видимость комментария.',403)
  db=get_db(); db.execute('INSERT INTO comments(ticket_id,author_id,author_name,body,visibility,created_at) VALUES(?,?,?,?,?,?)',(tid,g.user['id'] if g.user else None,g.user['name'] if g.user else 'Заявитель',body,visibility,now()))
  history(tid,'comment',note='Внутренняя заметка' if visibility=='internal' else 'Публичный комментарий')
  if g.user and visibility=='public': notifications(tid,'Сотрудник добавил комментарий. Откройте сохранённую ссылку отслеживания.')
  db.commit(); return jsonify(ok=True)
 @app.post('/api/tickets/<int:tid>/rating')
 def rating(tid):
  row=access_ticket(tid); data=payload(); value=integer(data.get('rating'),'Оценка'); comment=text_field(data,'comment',0,1000)
  if row['status']!='resolved': raise Problem('Оценить можно только решённую заявку.')
  # Staff must also possess applicant's secret token to rate; roles do not grant this right.
  if not hmac.compare_digest(digest(request.headers.get('X-Tracking-Token','')),row['tracking_token_hash']): raise Problem('Оценка доступна только по ссылке заявителя.',403)
  if value not in range(1,6): raise Problem('Выберите оценку от 1 до 5.')
  db=get_db(); db.execute('UPDATE tickets SET rating=?,rating_comment=? WHERE id=?',(value,comment,tid)); db.commit(); return jsonify(ok=True)
 @app.patch('/api/tickets/<int:tid>')
 @protected()
 def update_ticket(tid):
  data=payload(); db=get_db(); db.execute('BEGIN IMMEDIATE'); row=access_ticket(tid)
  if integer(data.get('version'))!=row['version']: raise Problem('Заявку уже изменили. Обновите карточку и повторите действие.',409)
  status=data.get('status',row['status']); priority=data.get('priority',row['priority']); cat=data.get('category_id',row['category_id'])
  assignee=data.get('assignee_id',row['assignee_id']); assignee=integer(assignee) if assignee not in ('',None) else None
  note=text_field(data,'note',0,3000)
  if status not in STATUSES or priority not in PRIORITIES: raise Problem('Неизвестный статус или приоритет.')
  if status!=row['status'] and status not in TRANSITIONS[row['status']]: raise Problem('Недопустимый переход статуса.')
  if status!=row['status'] and status in ('resolved','rejected') and not note: raise Problem('Укажите результат выполнения или причину отклонения.')
  if assignee and not db.execute('SELECT id FROM users WHERE id=? AND active=1',(assignee,)).fetchone(): raise Problem('Выберите действующего сотрудника.')
  if status in ('assigned','in_progress') and not assignee: raise Problem('Для этого статуса назначьте исполнителя.')
  if not db.execute('SELECT id FROM categories WHERE id=?',(cat,)).fetchone(): raise Problem('Категория не найдена.')
  resolved=(row['resolved_at'] or now()) if status=='resolved' else None
  db.execute('UPDATE tickets SET status=?,priority=?,category_id=?,assignee_id=?,updated_at=?,resolved_at=?,version=version+1,rating=CASE WHEN ? THEN NULL ELSE rating END,rating_comment=CASE WHEN ? THEN \'\' ELSE rating_comment END WHERE id=?',(status,priority,cat,assignee,now(),resolved,status!='resolved',status!='resolved',tid))
  if status!=row['status']:
   history(tid,'status',row['status'],status,note); notifications(tid,'Статус изменён: '+STATUSES[status]+'.')
  if assignee!=row['assignee_id']: history(tid,'assignment',note=f'Исполнитель: {assignee or "не назначен"}')
  if priority!=row['priority'] or cat!=row['category_id']: history(tid,'details',note='Обновлены категория или приоритет')
  if note: db.execute('INSERT INTO comments(ticket_id,author_id,author_name,body,visibility,created_at) VALUES(?,?,?,?,?,?)',(tid,g.user['id'],g.user['name'],note,'public',now()))
  db.commit(); return jsonify(serialize_ticket(access_ticket(tid),True))

 def filters():
  conditions=['1=1']; values=[]
  for field in ('status','category_id','location_id','assignee_id'):
   val=request.args.get(field)
   if val: conditions.append('t.'+field+'=?'); values.append(val)
  query=request.args.get('q','').strip()[:150]
  if query:
   conditions.append('(CASEFOLD(t.public_number) LIKE ? OR CASEFOLD(t.title) LIKE ? OR CASEFOLD(t.description) LIKE ?)')
   values.extend(['%'+query.casefold()+'%']*3)
  for key,op in [('date_from','>='),('date_to','<=')]:
   val=request.args.get(key)
   if val:
    try: datetime.strptime(val,'%Y-%m-%d')
    except ValueError: raise Problem('Дата должна быть в формате ГГГГ-ММ-ДД.')
    conditions.append('substr(t.created_at,1,10)'+op+'?'); values.append(val)
  return ' AND '.join(conditions),values
 @app.get('/api/tickets')
 @protected()
 def tickets():
  where,args=filters(); page=max(1,integer(request.args.get('page','1'))); db=get_db()
  total=db.execute('SELECT count(*) FROM tickets t WHERE '+where,args).fetchone()[0]
  rows=db.execute('SELECT t.id,t.public_number,t.title,t.status,t.priority,t.created_at,t.category_id,t.location_id,c.name category_name,c.icon category_icon,l.room,l.building,u.name assignee_name FROM tickets t JOIN categories c ON c.id=t.category_id JOIN locations l ON l.id=t.location_id LEFT JOIN users u ON u.id=t.assignee_id WHERE '+where+" ORDER BY CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,t.created_at DESC,t.id DESC LIMIT 20 OFFSET ?",args+[(page-1)*20]).fetchall()
  return jsonify(items=[dict(x) for x in rows],total=total,page=page,pages=max(1,(total+19)//20))
 @app.get('/api/export.csv')
 @protected()
 def export():
  where,args=filters(); db=get_db(); output=io.StringIO(); writer=csv.writer(output,delimiter=';'); writer.writerow(['Номер','Тема','Описание','Категория','Помещение','Статус','Приоритет','Исполнитель','Создана','Решена'])
  def safe(value):
   value=str(value or '')
   return "'"+value if value.lstrip().startswith(('=','+','-','@')) or value.startswith(('\t','\r','\n')) else value
  rows=db.execute('SELECT t.*,c.name category_name,l.room,u.name assignee_name FROM tickets t JOIN categories c ON c.id=t.category_id JOIN locations l ON l.id=t.location_id LEFT JOIN users u ON u.id=t.assignee_id WHERE '+where+' ORDER BY t.id DESC',args)
  for r in rows: writer.writerow([safe(x) for x in [r['public_number'],r['title'],r['description'],r['category_name'],r['room'],STATUSES[r['status']],PRIORITIES[r['priority']],r['assignee_name'],r['created_at'],r['resolved_at']]])
  return send_file(io.BytesIO(('\ufeff'+output.getvalue()).encode()),mimetype='text/csv; charset=utf-8',download_name='upk-report.csv',as_attachment=True)
 @app.get('/api/stats')
 @protected()
 def stats():
  db=get_db(); totals=dict(db.execute("SELECT count(*) total,coalesce(sum(status NOT IN ('resolved','rejected')),0) open,coalesce(sum(status='resolved'),0) resolved,coalesce(sum(status='new'),0) new,coalesce(sum(priority='urgent' AND status NOT IN ('resolved','rejected')),0) urgent,round(avg(CASE WHEN status='resolved' THEN (julianday(resolved_at)-julianday(created_at))*24 END),1) average_hours,round(avg(rating),1) rating FROM tickets").fetchone())
  categories=[dict(x) for x in db.execute('SELECT c.id,c.name,c.icon,count(t.id) count FROM categories c LEFT JOIN tickets t ON t.category_id=c.id GROUP BY c.id ORDER BY count DESC')]
  locations=[dict(x) for x in db.execute("SELECT l.*,count(t.id) total,coalesce(sum(t.status NOT IN ('resolved','rejected')),0) open FROM locations l LEFT JOIN tickets t ON t.location_id=l.id GROUP BY l.id ORDER BY total DESC")]
  repeats=[dict(x) for x in db.execute('SELECT l.room,c.name category,count(*) count FROM tickets t JOIN locations l ON l.id=t.location_id JOIN categories c ON c.id=t.category_id GROUP BY location_id,category_id HAVING count(*)>1 ORDER BY count DESC LIMIT 10')]
  days=[dict(x) for x in db.execute("SELECT substr(created_at,1,10) day,count(*) count FROM tickets WHERE created_at>=? GROUP BY day ORDER BY day",((datetime.now(timezone.utc)-timedelta(days=13)).date().isoformat(),))]
  ai=[dict(x) for x in db.execute('SELECT kind,count(*) count FROM ai_runs GROUP BY kind')]
  return jsonify(totals=totals,categories=categories,locations=locations,repeats=repeats,days=days,ai=ai)
 @app.get('/api/users')
 @protected()
 def users(): return jsonify([dict(x) for x in get_db().execute('SELECT id,name,login,role,active FROM users ORDER BY id')])
 @app.post('/api/users')
 @protected(admin=True)
 def add_user():
  data=payload(); name=text_field(data,'name',2,100); login=text_field(data,'login',3,50); password=text_field(data,'password',12,200); role=data.get('role','staff')
  if role not in ('staff','admin') or not re.fullmatch(r'[a-zA-Z0-9_.-]+',login): raise Problem('Проверьте роль и логин (латиница, цифры, . _ -).')
  db=get_db()
  try: db.execute('INSERT INTO users(name,login,password_hash,role) VALUES(?,?,?,?)',(name,login,generate_password_hash(password),role))
  except sqlite3.IntegrityError: raise Problem('Этот логин уже занят.')
  db.execute('INSERT INTO audit_log(actor_id,action,created_at) VALUES(?,?,?)',(g.user['id'],'Создан пользователь '+login,now())); db.commit(); return jsonify(ok=True),201
 @app.patch('/api/users/<int:uid>')
 @protected(admin=True)
 def edit_user(uid):
  data=payload(); db=get_db(); user=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
  if not user: raise Problem('Пользователь не найден.',404)
  if uid==g.user['id'] and ('role' in data or 'active' in data): raise Problem('Нельзя отключить свою учётную запись или изменить собственную роль.')
  role=data.get('role',user['role']); active=data.get('active',bool(user['active']))
  if role not in ('admin','staff') or not isinstance(active,bool): raise Problem('Некорректная роль или состояние.')
  db.execute('UPDATE users SET role=?,active=? WHERE id=?',(role,int(active),uid))
  if data.get('password'):
   password=text_field(data,'password',12,200); db.execute('UPDATE users SET password_hash=? WHERE id=?',(generate_password_hash(password),uid))
  db.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
  if not active: db.execute('UPDATE categories SET assignee_id=NULL WHERE assignee_id=?',(uid,))
  db.execute('INSERT INTO audit_log(actor_id,action,created_at) VALUES(?,?,?)',(g.user['id'],f'Изменена учётная запись {uid}',now())); db.commit(); return jsonify(ok=True)
 @app.post('/api/locations')
 @protected(admin=True)
 def save_location():
  data=payload(); building=text_field(data,'building',2,100); room=text_field(data,'room',1,60); zone=text_field(data,'zone_name',2,100); key=text_field(data,'qr_key',3,80); floor=integer(data.get('floor'),'Этаж')
  if not re.fullmatch(r'[a-zA-Z0-9_-]+',key) or not -3<=floor<=50: raise Problem('QR-ключ: латиница, цифры, дефис. Этаж: от −3 до 50.')
  db=get_db()
  try:
   if data.get('id'):
    uid=integer(data['id'])
    if not db.execute('SELECT id FROM locations WHERE id=?',(uid,)).fetchone(): raise Problem('Помещение не найдено.',404)
    db.execute('UPDATE locations SET building=?,floor=?,room=?,zone_name=?,qr_key=? WHERE id=?',(building,floor,room,zone,key,uid))
   else: db.execute('INSERT INTO locations(building,floor,room,zone_name,qr_key) VALUES(?,?,?,?,?)',(building,floor,room,zone,key))
  except sqlite3.IntegrityError: raise Problem('Такой QR-ключ уже существует.')
  db.execute('INSERT INTO audit_log(actor_id,action,created_at) VALUES(?,?,?)',(g.user['id'],'Сохранено помещение '+room,now())); db.commit(); return jsonify(ok=True)
 @app.post('/api/categories')
 @protected(admin=True)
 def save_category():
  data=payload(); cid=text_field(data,'id',2,60); name=text_field(data,'name',2,80); assignee=data.get('assignee_id'); assignee=integer(assignee) if assignee else None
  if not re.fullmatch(r'[a-z0-9_-]+',cid): raise Problem('Ключ категории: строчные латинские буквы, цифры, дефис.')
  db=get_db()
  if assignee and not db.execute('SELECT id FROM users WHERE id=? AND active=1',(assignee,)).fetchone(): raise Problem('Сотрудник не найден.')
  db.execute('INSERT INTO categories(id,name,assignee_id) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,assignee_id=excluded.assignee_id',(cid,name,assignee))
  db.execute('INSERT INTO audit_log(actor_id,action,created_at) VALUES(?,?,?)',(g.user['id'],'Изменена категория '+cid,now())); db.commit(); return jsonify(ok=True)
 @app.get('/api/settings')
 @protected(admin=True)
 def settings():
  db=get_db(); return jsonify(categories=[dict(x) for x in db.execute('SELECT * FROM categories')],audit=[dict(x) for x in db.execute('SELECT a.action,a.created_at,u.name FROM audit_log a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 40')])
 @app.get('/api/notifications')
 @protected()
 def notification_log():
  rows=get_db().execute('SELECT n.id,n.channel,n.status,n.body,n.error,n.created_at,n.sent_at,t.public_number FROM notifications n JOIN tickets t ON t.id=n.ticket_id ORDER BY n.id DESC LIMIT 100').fetchall()
  return jsonify([dict(x) for x in rows])
 @app.get('/api/qr/<key>.png')
 def qr(key):
  if not get_db().execute('SELECT id FROM locations WHERE qr_key=?',(key,)).fetchone(): raise Problem('Неизвестное помещение.',404)
  base=public_base_url()
  if urlparse(base).scheme not in ('http','https'): raise Problem('PUBLIC_BASE_URL должен начинаться с http:// или https://.')
  img=qrcode.make(base+'/#/new?location='+key); output=io.BytesIO(); img.save(output,format='PNG'); output.seek(0)
  return send_file(output,mimetype='image/png',download_name='qr-'+key+'.png',as_attachment=request.args.get('download')=='1')
 return app

if __name__=='__main__':
 app=create_app()
 print('УПК • http://localhost:'+os.getenv('PORT','8000')+'\nДемо-аккаунты: .data/demo-accounts.txt\n',flush=True)
 app.run(host=os.getenv('HOST','127.0.0.1'),port=int(os.getenv('PORT','8000')),debug=False)
