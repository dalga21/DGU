"""Browser contract checks for the existing chat, with a mocked Groq API."""
import json
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from groq import Groq
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from app import create_app
from groq_chat import GroqChat


def main():
    requests = []
    control = {'status': 200}
    entered, release = threading.Event(), threading.Event()

    def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        if control['status'] != 200:
            return httpx.Response(control['status'], headers={'Retry-After': '2'}, json={'error': {'message': 'Unavailable'}}, request=request)
        entered.set()
        if not release.wait(5):
            raise httpx.ReadTimeout('Mock response timeout', request=request)
        return httpx.Response(200, json={
            'id': 'mock', 'created': 1, 'model': 'openai/gpt-oss-120b', 'object': 'chat.completion',
            'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant',
             'content': '**Проверка Groq**\nНажмите Win + P.\n<script>window.compromised=true</script>'}}]}, request=request)

    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    with tempfile.TemporaryDirectory(prefix='upk-groq-browser-') as temp:
        app = create_app({'TESTING': True, 'DATA_DIR': temp, 'DATABASE': temp+'/db.sqlite3'})
        sdk = Groq(api_key='test-only', base_url='https://api.groq.com', max_retries=0,
                   http_client=httpx.Client(transport=httpx.MockTransport(provider)))
        chat = GroqChat('test-only', client=sdk)
        app.extensions['groq_chat'] = chat
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as p:
                options = {'headless': True}
                chrome = os.getenv('CHROME_EXECUTABLE', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
                if Path(chrome).exists(): options['executable_path'] = chrome
                browser = p.chromium.launch(**options)
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base+'/#/assistant')
                expect(page.locator('.chat-header')).to_contain_text('ИИ · Groq')
                page.locator('#chat-input').fill('Как подключить проектор?')
                page.locator('#chat-form button').click()
                expect(page.locator('#chat-messages')).to_contain_text('Помощник готовит ответ')
                expect(page.locator('#chat-input')).to_be_disabled()
                assert entered.wait(2)
                release.set()
                expect(page.locator('.assistant-answer')).to_contain_text('Проверка Groq')
                expect(page.locator('.assistant-answer strong')).to_have_text('Проверка Groq')
                assert page.evaluate('window.compromised') is None
                expect(page.locator('#chat-input')).to_be_enabled()
                page.locator('#chat-input').fill('Что значит Дублировать?')
                page.locator('#chat-form button').click()
                expect(page.locator('.assistant-answer')).to_have_count(2)
                messages = requests[-1]['messages']
                assert any(m['role']=='user' and m['content']=='Как подключить проектор?' for m in messages[:-1])
                assert any(m['role']=='assistant' for m in messages)
                control['status'] = 503
                page.locator('#chat-input').fill('Что делать с принтером?')
                page.locator('#chat-form button').click()
                expect(page.locator('#chat-messages')).to_contain_text('Сервис ИИ временно недоступен')
                expect(page.locator('#chat-messages')).to_contain_text('резервный режим')
                control['status'] = 429
                page.locator('#chat-input').fill('Что такое база данных?')
                page.locator('#chat-form button').click()
                expect(page.locator('#chat-messages')).to_contain_text('Достигнут лимит')
                count = len(requests)
                page.locator('#chat-input').fill('В розетке искрит')
                page.locator('#chat-form button').click()
                expect(page.locator('#chat-messages')).to_contain_text('Возможная опасность')
                assert len(requests) == count
                page.set_viewport_size({'width':390, 'height':844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                output = os.getenv('UPK_SCREENSHOT_DIR')
                if output:
                    Path(output).mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(Path(output)/'groq-chat-mobile.png'), full_page=True, animations='disabled')
                assert not errors, errors
                browser.close()
                print('Groq browser QA passed: existing chat, pending state, history, escaped output, API errors, 429, safety, mobile.')
        finally:
            release.set(); server.shutdown(); chat.close()


if __name__ == '__main__': main()
