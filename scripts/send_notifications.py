"""Explicit worker: python scripts/send_notifications.py. Does not send demo rows."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import create_app
from db import get_db,now
from integrations import deliver
app=create_app()
with app.app_context():
 db=get_db()
 for row in db.execute("SELECT * FROM notifications WHERE status='pending' ORDER BY id LIMIT 100").fetchall():
  # Claim once. Unknown outcome is not retried automatically (avoid duplicate sends).
  db.execute("UPDATE notifications SET status='sending' WHERE id=? AND status='pending'",(row['id'],))
  if db.execute('SELECT changes()').fetchone()[0]!=1: db.rollback(); continue
  db.commit()
  try:
   deliver(row['channel'],row['destination'],row['body'])
   db.execute("UPDATE notifications SET status='sent',sent_at=? WHERE id=?",(now(),row['id']))
  except Exception:
   db.execute("UPDATE notifications SET status='failed',error='Отправка не подтверждена. Проверьте конфигурацию и состояние провайдера.' WHERE id=?",(row['id'],))
  db.commit()
 print('Очередь обработана. Результаты — в журнале уведомлений.')
