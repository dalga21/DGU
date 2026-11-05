import hashlib
import secrets
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from flask import current_app, g
from werkzeug.security import generate_password_hash

STATUSES={'new':'Новая','review':'На рассмотрении','assigned':'Назначен исполнитель','in_progress':'В работе','needs_info':'Нужны уточнения','resolved':'Решена','rejected':'Отклонена'}
PRIORITIES={'normal':'Обычный','high':'Высокий','urgent':'Срочный'}
TRANSITIONS={'new':['review','assigned','in_progress','needs_info','rejected'],'review':['assigned','in_progress','needs_info','resolved','rejected'],'assigned':['review','in_progress','needs_info','resolved','rejected'],'in_progress':['assigned','needs_info','resolved','rejected'],'needs_info':['review','assigned','in_progress','resolved','rejected'],'resolved':['review'],'rejected':['review']}
def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def digest(value): return hashlib.sha256(value.encode()).hexdigest()
def get_db():
 if 'db' not in g:
  g.db=sqlite3.connect(current_app.config['DATABASE'],timeout=15)
  g.db.row_factory=sqlite3.Row
  g.db.create_function('CASEFOLD',1,lambda value: str(value or '').casefold(),deterministic=True)
  g.db.execute('PRAGMA foreign_keys=ON')
  g.db.execute('PRAGMA journal_mode=WAL')
 return g.db

def close_db(_=None):
 db=g.pop('db',None)
 if db is not None: db.close()

def initialize(app):
 with app.app_context():
  db=get_db(); db.executescript(Path(__file__).with_name('schema.sql').read_text())
  if db.execute('SELECT count(*) FROM users').fetchone()[0]: return
  accounts=[]
  for name,login,role in [('Администратор УПК','admin','admin'),('Али Магомедов','staff','staff'),('Марина Алиева','technician','staff')]:
   password=secrets.token_urlsafe(12)
   db.execute('INSERT INTO users(name,login,password_hash,role) VALUES(?,?,?,?)',(name,login,generate_password_hash(password),role))
   accounts.append(f'{name}: логин {login} | пароль {password}')
  db.executemany('INSERT INTO categories VALUES(?,?,?,?)', [('equipment','Оборудование','monitor',2),('network','Интернет и связь','wifi',2),('electric','Электрика','bolt',3),('property','Имущество','chair',3),('cleaning','Уборка','sparkles',3),('plumbing','Сантехника','drop',3),('other','Другое','grid',None)])
  db.executemany('INSERT INTO locations(building,floor,room,zone_name,qr_key,x,y) VALUES(?,?,?,?,?,?,?)', [('Главный корпус',2,'214','Учебная аудитория','room-214',0,0),('Главный корпус',3,'305','Компьютерная лаборатория','lab-305',1,0),('Главный корпус',1,'Холл','Общая зона','hall-1',2,0),('Главный корпус',2,'208','Библиотека','library-208',0,1),('Главный корпус',1,'112','Учебная аудитория','room-112',1,1)])
  if app.config['SEED_DEMO']:
   demos=[('Не работает проектор','При включении проектор не показывает изображение. Кабель внешне цел.','equipment',1,'new','normal',2),('Неустойчивое подключение к Wi-Fi','На нескольких компьютерах периодически пропадает интернет.','network',2,'in_progress','high',2),('Сломана ручка окна','Ручка окна прокручивается, окно не закрывается.','property',2,'assigned','normal',3),('Не горит свет в холле','Два светильника не включаются.','electric',3,'review','normal',3),('Требуется уборка','В общей зоне рассыпана земля из цветочного горшка.','cleaning',3,'resolved','normal',3),('Принтер не захватывает бумагу','Бумага есть, принтер сообщает об ошибке.','equipment',4,'needs_info','normal',2),('Протекает кран','Под краном собирается вода.','plumbing',5,'new','high',3),('Замена повреждённого стула','Повреждена ножка стула, использовать его небезопасно.','property',1,'resolved','normal',3),('Нет изображения на проекторе','Проектор включается, но изображение не появляется.','equipment',1,'resolved','normal',2),('Пропадает сигнал проектора','Изображение периодически исчезает во время работы.','equipment',1,'resolved','normal',2)]
   for i,(title,description,cat,loc,status,priority,assignee) in enumerate(demos):
    created=(datetime.now(timezone.utc)-timedelta(days=i,hours=2)).isoformat(timespec='seconds')
    finished=(datetime.fromisoformat(created)+timedelta(hours=1+i)).isoformat(timespec='seconds') if status=='resolved' else None
    cur=db.execute('INSERT INTO tickets(title,description,category_id,location_id,status,priority,created_at,updated_at,resolved_at,assignee_id,tracking_token_hash,request_key_hash,requester_name,ai_kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(title,description,cat,loc,status,priority,created,finished or now(),finished,assignee,digest(secrets.token_urlsafe(32)),digest(secrets.token_urlsafe(32)),'Демонстрационный заявитель','fault'))
    tid=cur.lastrowid
    db.execute('UPDATE tickets SET public_number=? WHERE id=?',(f'УПК-{tid:05d}',tid))
    db.execute('INSERT INTO ticket_history(ticket_id,actor_name,event,new_status,created_at) VALUES(?,?,?,?,?)',(tid,'Система','created','new',created))
    if status!='new': db.execute('INSERT INTO ticket_history(ticket_id,actor_id,actor_name,event,old_status,new_status,note,created_at) VALUES(?,?,?,?,?,?,?,?)',(tid,1,'Администратор УПК','status','new',status,'Демонстрационная запись',finished or now()))
    if status=='resolved':
     db.execute('INSERT INTO comments(ticket_id,author_id,author_name,body,visibility,created_at) VALUES(?,?,?,?,?,?)',(tid,1,'Администратор УПК','Демонстрация: неисправность устранена, работоспособность проверена.','public',finished))
     db.execute('UPDATE tickets SET rating=5 WHERE id=?',(tid,))
  db.commit()
  path=Path(app.config['DATA_DIR'])/'demo-accounts.txt'
  path.write_text('Учётные записи этой локальной установки. Не публикуйте файл.\n\n'+'\n'.join(accounts)+'\n'); path.chmod(0o600)
  if not app.config['TESTING']: print('\nУчётные записи первой установки:\n'+'\n'.join(accounts)+'\nСохранены в .data/demo-accounts.txt\n',flush=True)
