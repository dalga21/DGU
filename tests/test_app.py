import base64
import io
import os
import re
import secrets
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from PIL import Image
from app import create_app
from assistant import classify
from db import get_db

HEADERS={'X-UPK-Request':'1'}

class AppTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory()
  self.app=create_app({'TESTING':True,'SEED_DEMO':False,'DATA_DIR':self.temp.name,'DATABASE':self.temp.name+'/test.sqlite3'})
  self.client=self.app.test_client()
  with open(self.temp.name+'/demo-accounts.txt') as f: self.accounts=f.read()
 def tearDown(self): self.temp.cleanup()
 def login(self,role='admin',client=None):
  password=re.search(r'логин '+role+r' \| пароль (\S+)',self.accounts)[1]
  response=(client or self.client).post('/api/login',json={'login':role,'password':password},headers=HEADERS)
  self.assertEqual(response.status_code,200,response.json)
  return response
 def create(self,**changes):
  data={'request_key':secrets.token_hex(32),'title':'Не работает проектор','description':'В кабинете 214 не работает проектор','category_id':'equipment','location_id':1}
  data.update(changes);r=self.client.post('/api/tickets',json=data,headers=HEADERS)
  self.assertIn(r.status_code,(200,201),r.json);return r.json
 def public(self,t): return self.client.get('/api/track',headers={'X-Tracking-Token':t['token']})
 def update(self,t,**changes):
  row=self.client.get('/api/tickets/'+str(t['id'])).json
  return self.client.patch('/api/tickets/'+str(t['id']),json={'version':row['version'],**changes},headers=HEADERS)
 def test_at01_qr_location_and_real_png(self):
  meta=self.client.get('/api/meta').json;self.assertGreaterEqual(len(meta['locations']),3)
  qr=self.client.get('/api/qr/room-214.png');self.assertEqual(qr.status_code,200);self.assertEqual(Image.open(io.BytesIO(qr.data)).format,'PNG');qr.close()
  self.assertEqual(self.client.get('/api/qr/does-not-exist.png').status_code,404)
 def test_at02_persistence_unique_tracking_hash(self):
  t=self.create();t2=self.create();self.assertNotEqual(t['public_number'],t2['public_number']);self.assertEqual(len(t['token']),43)
  public=self.public(t).json;self.assertEqual(public['status'],'new');self.assertNotIn('tracking_token_hash',public);self.assertNotIn('email',public)
  with self.app.app_context():
   stored=get_db().execute('SELECT tracking_token_hash FROM tickets WHERE id=?',(t['id'],)).fetchone()[0]
   self.assertNotEqual(stored,t['token'])
  new_app=create_app({'TESTING':True,'SEED_DEMO':False,'DATA_DIR':self.temp.name,'DATABASE':self.temp.name+'/test.sqlite3'})
  self.assertEqual(new_app.test_client().get('/api/track',headers={'X-Tracking-Token':t['token']}).json['id'],t['id'])
 def test_idempotent_submission(self):
  key=secrets.token_hex(32);a=self.create(request_key=key);b=self.create(request_key=key)
  self.assertEqual(a['id'],b['id']);self.assertEqual(a['token'],b['token']);self.assertTrue(b['replayed'])
  with self.app.app_context(): self.assertEqual(get_db().execute('SELECT count(*) FROM tickets').fetchone()[0],1)
 def test_at03_04_filters(self):
  t=self.create();self.login();r=self.client.get('/api/tickets?q='+t['public_number']);self.assertEqual(r.json['total'],1)
  self.assertEqual(self.client.get('/api/tickets?status=resolved').json['total'],0)
  self.assertEqual(self.client.get('/api/tickets?location_id=1&category_id=equipment').json['total'],1)
  self.assertEqual(self.client.get('/api/tickets?date_from=2020-99-99').status_code,400)
 def test_at05_06_12_end_to_end_and_audit(self):
  t=self.create();self.login();r=self.update(t,status='in_progress',assignee_id=2,note='Проверяем подключение');self.assertEqual(r.status_code,200,r.json)
  r=self.update(t,status='resolved',note='Заменён кабель, изображение восстановлено.');self.assertEqual(r.status_code,200,r.json)
  self.client.post('/api/logout',json={},headers=HEADERS)
  public=self.public(t).json;self.assertEqual(public['status'],'resolved');self.assertEqual(public['comments'][-1]['body'],'Заменён кабель, изображение восстановлено.')
  self.assertEqual([h['new_status'] for h in public['history']],['new','in_progress','resolved'])
  rate=self.client.post(f"/api/tickets/{t['id']}/rating",json={'rating':5,'comment':'Спасибо'},headers={**HEADERS,'X-Tracking-Token':t['token']})
  self.assertEqual(rate.status_code,200);self.assertEqual(self.public(t).json['rating'],5)
 def test_no_automatic_closure(self):
  t=self.create();self.client.post('/api/assistant',json={'text':'Как подключить проектор?'},headers=HEADERS);self.assertEqual(self.public(t).json['status'],'new')
 def test_terminal_result_required_and_reopen(self):
  t=self.create();self.login();self.assertEqual(self.update(t,status='rejected').status_code,400)
  self.assertEqual(self.update(t,status='resolved',note='Готово').status_code,400) # New -> resolved is not allowed.
  self.assertEqual(self.update(t,status='review').status_code,200)
  self.assertEqual(self.update(t,status='resolved').status_code,400)
  self.assertEqual(self.update(t,status='resolved',note='Проверено').status_code,200)
  self.assertEqual(self.update(t,status='review',note='Повторная проверка').status_code,200)
  self.assertIsNone(self.client.get('/api/tickets/'+str(t['id'])).json['resolved_at'])
 def test_at11_unauthorized_operations(self):
  t=self.create()
  for url in ['/api/tickets','/api/tickets/'+str(t['id']),'/api/stats','/api/users','/api/settings','/api/notifications','/api/export.csv']:
   self.assertEqual(self.client.get(url).status_code,401,url)
  self.assertEqual(self.client.patch('/api/tickets/'+str(t['id']),json={'version':1,'status':'resolved'},headers=HEADERS).status_code,401)
  self.assertEqual(self.client.get('/api/track',headers={'X-Tracking-Token':secrets.token_urlsafe(32)}).status_code,404)
 def test_cross_ticket_token_and_private_notes(self):
  a=self.create();b=self.create();self.login()
  self.client.post(f"/api/tickets/{a['id']}/comments",json={'body':'Не показывать заявителю','visibility':'internal'},headers=HEADERS)
  self.client.post('/api/logout',json={},headers=HEADERS)
  self.assertEqual(self.public(a).json['comments'],[])
  bad=self.client.post(f"/api/tickets/{b['id']}/comments",json={'body':'Подмена'},headers={**HEADERS,'X-Tracking-Token':a['token']})
  self.assertEqual(bad.status_code,404)
  bad=self.client.post(f"/api/tickets/{a['id']}/comments",json={'body':'Подмена','visibility':'internal'},headers={**HEADERS,'X-Tracking-Token':a['token']})
  self.assertEqual(bad.status_code,403)
 def test_staff_cannot_administrate(self):
  self.login('staff')
  self.assertEqual(self.client.get('/api/settings').status_code,403)
  self.assertEqual(self.client.post('/api/users',json={},headers=HEADERS).status_code,403)
  self.assertEqual(self.client.post('/api/locations',json={},headers=HEADERS).status_code,403)
 def test_optimistic_lock_prevents_lost_update(self):
  t=self.create();self.login();r=self.update(t,status='review');self.assertEqual(r.status_code,200)
  r=self.client.patch(f"/api/tickets/{t['id']}",json={'version':1,'status':'assigned','assignee_id':2},headers=HEADERS);self.assertEqual(r.status_code,409)
 def test_photo_is_decoded_normalized_and_private(self):
  output=io.BytesIO();Image.new('RGB',(20,20),'blue').save(output,format='PNG')
  t=self.create(photo=base64.b64encode(output.getvalue()).decode());public=self.public(t).json;aid=public['attachments'][0]['id']
  self.assertEqual(self.client.get('/api/attachments/'+aid).status_code,404)
  r=self.client.get('/api/attachments/'+aid,headers={'X-Tracking-Token':t['token']});self.assertEqual(r.status_code,200);self.assertEqual(Image.open(io.BytesIO(r.data)).format,'JPEG');r.close()
 def test_invalid_photo_rejected_no_false_success(self):
  r=self.client.post('/api/tickets',json={'request_key':secrets.token_hex(32),'title':'Некорректное фото','description':'Описание проблемы тестовое','category_id':'other','location_id':1,'photo':base64.b64encode(b'<svg><script>alert(1)</script></svg>').decode()},headers=HEADERS)
  self.assertEqual(r.status_code,400)
  with self.app.app_context(): self.assertEqual(get_db().execute('SELECT count(*) FROM tickets').fetchone()[0],0)
 def test_csrf_and_headers(self):
  self.assertEqual(self.client.post('/api/login',json={'login':'admin','password':'test'}).status_code,403)
  self.assertEqual(self.client.post('/api/logout',json={},headers={**HEADERS,'Origin':'https://other.test'}).status_code,403)
  r=self.client.get('/api/meta');self.assertEqual(r.headers['Cache-Control'],'no-store');self.assertIn("script-src 'self'",r.headers['Content-Security-Policy'])
 def test_auth_logout_revokes_session(self):
  self.login();self.assertEqual(self.client.get('/api/tickets').status_code,200)
  self.client.post('/api/logout',json={},headers=HEADERS);self.assertEqual(self.client.get('/api/tickets').status_code,401)
 def test_statistics_and_csv_formula_escape(self):
  self.create(title='=HYPERLINK("danger")');self.login();stats=self.client.get('/api/stats').json
  self.assertEqual(stats['totals']['total'],1);self.assertEqual(stats['totals']['open'],1)
  self.assertIsNone(stats['totals']['average_hours']);self.assertEqual(sum(x['count'] for x in stats['categories']),1)
  r=self.client.get('/api/export.csv');self.assertIn("'=HYPERLINK",r.data.decode('utf-8-sig'));r.close()
 def test_duplicate_warning_does_not_leak(self):
  self.create(description='Секретное описание заявителя')
  r=self.client.post('/api/similar',json={'location_id':1,'category_id':'equipment'},headers=HEADERS)
  self.assertEqual(r.json['count'],1);self.assertNotIn('Секретное',r.get_data(as_text=True));self.assertEqual(set(r.json),{'count','message'})
 def test_notifications_demo_and_consent(self):
  t=self.create(email='student@example.org',notification_consent=True)
  self.login();rows=self.client.get('/api/notifications').json;self.assertEqual(rows[0]['status'],'demo');self.assertNotIn('destination',rows[0])
  self.assertEqual(self.update(t,status='review').status_code,200);self.assertEqual(len(self.client.get('/api/notifications').json),2)
 def test_location_rebinding_and_new_user(self):
  self.login();r=self.client.post('/api/locations',json={'id':1,'building':'Новый корпус','floor':1,'room':'101','zone_name':'Аудитория','qr_key':'room-214'},headers=HEADERS)
  self.assertEqual(r.status_code,200);self.assertEqual(next(x for x in self.client.get('/api/meta').json['locations'] if x['qr_key']=='room-214')['room'],'101')
  r=self.client.post('/api/users',json={'name':'Тестовый сотрудник','login':'new_staff','password':'long-test-password','role':'staff'},headers=HEADERS);self.assertEqual(r.status_code,201)
  other=self.app.test_client();self.assertEqual(other.post('/api/login',json={'login':'new_staff','password':'long-test-password'},headers=HEADERS).status_code,200)
  uid=next(x for x in self.client.get('/api/users').json if x['login']=='new_staff')['id']
  self.client.patch('/api/users/'+str(uid),json={'active':False},headers=HEADERS);self.assertEqual(other.get('/api/tickets').status_code,401)
 def test_photo_demo_is_honest(self):
  out=io.BytesIO();Image.new('RGB',(10,10)).save(out,format='PNG')
  with patch.dict(os.environ,{'VISION_API_URL':'','VISION_API_KEY':'','VISION_MODEL':''}):
   r=self.client.post('/api/photo-analysis',json={'photo':base64.b64encode(out.getvalue()).decode()},headers=HEADERS)
  self.assertEqual(r.json['mode'],'demo');self.assertEqual(r.json['description'],'')

 def test_parallel_idempotency(self):
  data={'request_key':secrets.token_hex(32),'title':'Параллельная отправка','description':'В кабинете не работает проектор','category_id':'equipment','location_id':1}
  def send(_):
   with self.app.test_client() as client:
    response=client.post('/api/tickets',json=data,headers=HEADERS)
    return response.status_code,response.json
  with ThreadPoolExecutor(max_workers=3) as executor: results=list(executor.map(send,range(3)))
  self.assertEqual(sorted(r[0] for r in results),[200,200,201])
  self.assertEqual(len({r[1]['id'] for r in results}),1)
  self.assertEqual(len({r[1]['token'] for r in results}),1)
 def test_russian_case_insensitive_search(self):
  self.create();self.login()
  self.assertEqual(self.client.get('/api/tickets?q=ПРОЕКТОР').json['total'],1)
 def test_codespaces_origin_and_base_url(self):
  with patch.dict(os.environ,{'PUBLIC_BASE_URL':'','CODESPACE_NAME':'test-space','GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN':'app.github.dev','PORT':'8000'}):
   self.assertEqual(self.client.get('/api/meta').json['public_base_url'],'https://test-space-8000.app.github.dev')
   self.assertEqual(self.client.post('/api/logout',json={},headers={**HEADERS,'Origin':'https://test-space-8000.app.github.dev'}).status_code,200)
   self.assertEqual(self.client.post('/api/logout',json={},headers={**HEADERS,'Origin':'https://attacker.example'}).status_code,403)
 def test_category_assignment(self):
  self.login()
  r=self.client.post('/api/categories',json={'id':'new-category','name':'Новая категория','assignee_id':3},headers=HEADERS)
  self.assertEqual(r.status_code,200)
  t=self.create(category_id='new-category')
  detail=self.client.get('/api/tickets/'+str(t['id'])).json
  self.assertEqual(detail['assignee_id'],3);self.assertEqual(detail['status'],'new')

class AssistantTests(unittest.TestCase):
 def test_all_ten_case_scenarios(self):
  cases=[('Как подключить ноутбук к проектору?','question','projector'),('В кабинете 214 не работает проектор','fault',None),('Сломалась ручка окна в кабинете 305','fault',None),('В розетке искрит','danger',None),('Где посмотреть статус заявки?','question','tracking'),('Когда починят проектор?','unknown','tracking'),('У меня проблема','clarify',None),('Какой пароль от Wi-Fi сотрудников?','unknown',None),('В кабинете грязно','fault',None),('Как заменить проводку самому?','danger',None)]
  for text,kind,article in cases:
   with self.subTest(text=text):
    r=classify(text);self.assertEqual(r['kind'],kind);self.assertEqual(r['knowledge_id'],article);self.assertEqual(r['mode'],'demo')
 def test_unsafe_request_overrides_instruction(self):
  r=classify('Как подключить проектор, из него идёт дым?');self.assertEqual(r['kind'],'danger');self.assertIsNone(r['knowledge_id']);self.assertEqual(r['priority'],'urgent')
 def test_unknown_and_fault_are_not_false_answers(self):
  self.assertEqual(classify('Кто выиграл чемпионат мира по шахматам?')['kind'],'unknown')
  self.assertEqual(classify('Как подключить проектор, если он не работает?')['kind'],'fault')

if __name__=='__main__': unittest.main()
