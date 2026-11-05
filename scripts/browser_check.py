"""Optional end-to-end browser QA on an isolated temporary database."""
import io
import logging
import os
import re
import sys
import tempfile
import threading
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import create_app
from PIL import Image
from playwright.sync_api import sync_playwright,expect
from werkzeug.serving import make_server

ROOT=Path(__file__).resolve().parents[1]

def main():
 with tempfile.TemporaryDirectory(prefix='upk-browser-') as tmp:
  logging.getLogger('werkzeug').setLevel(logging.ERROR)
  app=create_app({'TESTING':True,'DATA_DIR':tmp,'DATABASE':tmp+'/test.sqlite3'})
  password=re.search(r'логин admin \| пароль (\S+)',Path(tmp,'demo-accounts.txt').read_text())[1]
  server=make_server('127.0.0.1',0,app,threaded=True)
  thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base=f'http://127.0.0.1:{server.server_port}'
  screenshots=Path(os.getenv('UPK_SCREENSHOT_DIR',str(ROOT/'docs/screenshots')));screenshots.mkdir(parents=True,exist_ok=True)
  try:
   with sync_playwright() as p:
    options={'headless':True}
    chrome=os.getenv('CHROME_EXECUTABLE','/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    if Path(chrome).exists(): options['executable_path']=chrome
    browser=p.chromium.launch(**options)
    context=browser.new_context(viewport={'width':1440,'height':1100},locale='ru-RU',timezone_id='Europe/Moscow')
    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(base);expect(page.get_by_role('heading',name='Рады видеть вас 👋')).to_be_visible()
    page.screenshot(path=str(screenshots/'home-desktop.png'),full_page=True,animations="disabled")
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),'Desktop overflow'
    page.goto(base+'/#/new?location=room-214');expect(page.get_by_role('heading',name='Новое обращение',exact=True)).to_be_visible()
    expect(page.locator('select[name=location_id]')).to_have_value('1')
    page.locator('textarea[name=description]').fill('В кабинете 214 не работает проектор, нет изображения.')
    page.get_by_role('button',name='Подсказать категорию').click();expect(page.locator('#draft-analysis')).to_contain_text('Нужна помощь сотрудника')
    expect(page.locator('select[name=category_id]')).to_have_value('equipment')
    page.locator('input[name=title]').fill('Тест браузера: проектор 214')
    img=io.BytesIO();Image.new('RGB',(120,80),'blue').save(img,format='PNG')
    page.locator('#photo-input').set_input_files({'name':'equipment.png','mimeType':'image/png','buffer':img.getvalue()})
    expect(page.locator('#photo-preview img')).to_be_visible()
    page.get_by_role('button',name='Анализ фото',exact=True).click();expect(page.locator('#photo-analysis-result')).to_contain_text('не распознаёт')
    page.get_by_text('Получать уведомления по email или Telegram').click()
    page.locator('input[name=email]').fill('qa@example.org');page.locator('input[name=notification_consent]').check()
    page.get_by_role('button',name='Отправить обращение',exact=True).click();expect(page.get_by_role('heading',name='Обращение зарегистрировано')).to_be_visible()
    page.get_by_role('link',name='Отследить заявку').click();expect(page.get_by_role('heading',name='Тест браузера: проектор 214')).to_be_visible();tracking_url=page.url
    expect(page.locator('.attachment-img')).to_be_visible();ticket_number=page.locator('h1').inner_text()
    page.locator('textarea[name=body]').fill('Дополнение от заявителя: кабель цел.')
    page.locator('#comment-form button[type=submit]').click();expect(page.locator('.comment p')).to_contain_text('Дополнение от заявителя')
    page.goto(base+'/#/login');page.locator('input[name=login]').fill('admin');page.locator('input[name=password]').fill(password);page.locator('#login-form button[type=submit]').click()
    expect(page.get_by_role('heading',name='Обзор обращений')).to_be_visible();page.locator('#toast').evaluate('(el)=>el.classList.remove("visible")');page.screenshot(path=str(screenshots/'dashboard-desktop.png'),full_page=True,animations="disabled")
    page.goto(base+'/#/tickets');expect(page.get_by_role('heading',name='Все заявки',exact=True)).to_be_visible();page.locator('#ticket-search').fill(ticket_number);page.locator('#filters-form button[type=submit]').click()
    expect(page.locator('.ticket-subject')).to_have_count(1);page.locator('.ticket-subject').click();expect(page.get_by_role('heading',name='Управление заявкой')).to_be_visible()
    page.locator('select[name=status]').select_option('in_progress');page.locator('select[name=assignee_id]').select_option('2');page.locator('textarea[name=note]').fill('Принято в работу.');page.locator('#update-ticket-form button').click();expect(page.locator('.comment p').last).to_have_text('Принято в работу.')
    page.locator('textarea[name=body]').fill('Внутренняя заметка проверки');page.locator('select[name=visibility]').select_option('internal');page.locator('#comment-form button[type=submit]').click();expect(page.locator('.comment.internal')).to_be_visible()
    page.locator('select[name=status]').select_option('resolved');page.locator('textarea[name=note]').fill('Заменён кабель HDMI. Изображение восстановлено.');page.locator('#update-ticket-form button').click();expect(page.locator('.badge.resolved')).to_be_visible()
    for route,heading in [('/analytics','Аналитика'),('/locations','Помещения и QR-коды'),('/notifications','Журнал уведомлений'),('/settings','Настройки')]:
     page.goto(base+'/#'+route);expect(page.get_by_role('heading',name=heading,exact=True)).to_be_visible();assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),route+' overflow'
    page.get_by_role('button',name='Добавить категорию').click();page.locator('#category-form input[name=id]').fill('qa-category');page.locator('#category-form input[name=name]').fill('Тестовая категория');page.locator('#category-form button').click();expect(page.get_by_role('heading',name='Тестовая категория')).to_be_visible()
    page.goto(base+'/#/locations');expect(page.locator('.qr-card img').first).to_be_visible();assert page.locator('.qr-card img').first.evaluate('(img)=>img.complete && img.naturalWidth>0')
    page.get_by_role('button',name='Помещение',exact=True).click();page.locator('#modal input[name=room]').fill('402');page.locator('#modal input[name=zone_name]').fill('Тестовая аудитория');page.locator('#modal input[name=qr_key]').fill('qa-402');page.locator('#location-form button').click();expect(page.get_by_role('heading',name='402 · Тестовая аудитория')).to_be_visible()
    page.locator('[data-action=logout]').click();expect(page.get_by_role('heading',name='Рады видеть вас 👋')).to_be_visible()
    page.goto(tracking_url);expect(page.locator('.badge.resolved')).to_be_visible();expect(page.locator('.comment.internal')).to_have_count(0);expect(page.locator('.comment p').last).to_contain_text('Изображение восстановлено')
    page.locator('input[name=rating][value="5"]').check(force=True);page.locator('#rating-form input[name=comment]').fill('Всё работает!');page.locator('#rating-form button').click();expect(page.get_by_role('button',name='Обновить оценку')).to_be_visible()
    page.goto(base+'/#/assistant');page.locator('#chat-input').fill('В розетке искрит');page.locator('#chat-form button').click();expect(page.locator('.chat-messages')).to_contain_text('Возможная опасность')
    page.locator('#chat-input').fill('Кто выиграл чемпионат мира по шахматам?');page.locator('#chat-form button').click();expect(page.locator('.chat-messages')).to_contain_text('В проверенной базе недостаточно информации')
    page.goto(base+'/#/knowledge');page.locator('#kb-search').fill('проектор');expect(page.locator('.knowledge-card')).to_have_count(2);page.locator('.knowledge-card a[href="#/knowledge/projector"]').click();expect(page.locator('.article-content')).to_contain_text('Win + P')
    page.set_viewport_size({'width':390,'height':844});page.goto(base);expect(page.get_by_role('heading',name='Рады видеть вас 👋')).to_be_visible();page.screenshot(path=str(screenshots/'home-mobile.png'),full_page=True,animations="disabled")
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),'Mobile home overflow'
    page.locator('.menu-toggle').click();expect(page.locator('.sidebar')).to_have_class(re.compile(r'open'));page.locator('.sidebar a[href="#/new"]').click();expect(page.get_by_role('heading',name='Новое обращение',exact=True)).to_be_visible();assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),'Mobile form overflow'
    for route in ['/mine','/assistant','/knowledge','/locations']:
     page.goto(base+'/#'+route);page.locator('.loading').wait_for(state='detached');assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),route+' mobile overflow'
    assert not errors,'Browser errors: '+repr(errors)
    browser.close();print('Browser QA passed: applicant → staff → resolution → rating; privacy, QR, assistant, desktop/mobile. No JS errors.')
  finally: server.shutdown()

if __name__=='__main__': main()
