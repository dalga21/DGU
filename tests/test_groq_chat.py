"""Offline API contract tests. All provider traffic uses httpx.MockTransport."""
import io
import json
import logging
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx
from groq import Groq

from app import create_app
from assistant import classify
from db import get_db
from groq_chat import DEFAULT_MODEL, GroqChat, MAX_HISTORY_CHARS, validate_history

KEY = 'test-key-not-a-real-credential'
HEADERS = {'X-UPK-Request': '1'}


def completion(content='Здравствуйте! Чем помочь с учёбой?', finish='stop'):
    return {'id': 'mock-completion', 'object': 'chat.completion', 'created': 1,
            'model': DEFAULT_MODEL, 'choices': [{'index': 0, 'finish_reason': finish,
            'message': {'role': 'assistant', 'content': content}}]}


class GroqChatTests(unittest.TestCase):
    def service(self, handler=None, **kwargs):
        self.calls = []

        def transport(request):
            self.calls.append(request)
            if handler:
                return handler(request)
            return httpx.Response(200, json=completion(), request=request)

        http = httpx.Client(transport=httpx.MockTransport(transport))
        sdk = Groq(api_key=KEY, base_url='https://api.groq.com',
                   http_client=http, max_retries=0)
        service = GroqChat(KEY, client=sdk, **kwargs)
        self.addCleanup(service.close)
        return service

    def test_real_sdk_contract_and_russian_prompt(self):
        service = self.service()
        answer = service.answer('Объясни, что такое алгоритм?')
        self.assertEqual(answer['mode'], 'groq')
        self.assertEqual(answer['model'], DEFAULT_MODEL)
        self.assertEqual(answer['message'], 'Здравствуйте! Чем помочь с учёбой?')
        request = self.calls[0]
        self.assertEqual(str(request.url), 'https://api.groq.com/openai/v1/chat/completions')
        self.assertEqual(request.headers['Authorization'], 'Bearer ' + KEY)
        body = json.loads(request.content)
        self.assertEqual(body['model'], DEFAULT_MODEL)
        self.assertIn('по-русски', body['messages'][0]['content'])
        self.assertNotIn(KEY, json.dumps(body))
        self.assertFalse(body['stream'])
        self.assertFalse(body['include_reasoning'])
        self.assertEqual(body['reasoning_effort'], 'low')
        self.assertEqual(body['max_completion_tokens'], 1024)
        self.assertNotIn('tools', body)
        self.assertNotIn('service_tier', body)

    def test_no_key_uses_local_knowledge_without_network(self):
        service = GroqChat('')
        answer = service.answer('Как подключить ноутбук к проектору?')
        self.assertEqual(answer['mode'], 'demo')
        self.assertEqual(answer['knowledge_id'], 'projector')
        self.assertIsNone(service._client)

    def test_danger_and_secrets_bypass_model(self):
        service = self.service()
        for question in ['В розетке искрит', 'Как заменить проводку самому?', 'Какой пароль от Wi-Fi сотрудников?']:
            with self.subTest(question=question):
                answer = service.answer(question)
                self.assertEqual(answer['mode'], 'local')
        self.assertEqual(self.calls, [])

    def test_algorithm_is_not_fire(self):
        for question in ['Что такое алгоритм?', 'Объясни алгоритмы сортировки', 'Как работает алгоритм поиска?']:
            self.assertNotEqual(classify(question)['kind'], 'danger')
        for question in ['Горит розетка', 'В кабинете дым', 'Произошло задымление']:
            self.assertEqual(classify(question)['kind'], 'danger')

    def test_conversation_context_and_knowledge(self):
        service = self.service()
        history = [{'role': 'user', 'content': 'Как подключить ноутбук к проектору?'},
                   {'role': 'assistant', 'content': 'Нажмите Win + P.'}]
        service.answer('Что значит Дублировать?', history)
        messages = json.loads(self.calls[0].content)['messages']
        self.assertEqual(messages[1:3], history)
        self.assertIn('Win + P', messages[0]['content'])
        self.assertIn('Источник:', messages[0]['content'])
        self.assertEqual(messages[-1]['content'], 'Что значит Дублировать?')

    def test_contextual_safety_and_unsuccessful_instruction(self):
        service = self.service()
        answer = service.answer('Как это починить самому?', [{'role': 'user', 'content': 'В розетке искрит'}])
        self.assertEqual(answer['kind'], 'danger')
        self.assertEqual(self.calls, [])
        answer = service.answer('Не помогло', [{'role': 'user', 'content': 'Как подключить проектор?'}])
        self.assertEqual(answer['kind'], 'fault')
        self.assertEqual(answer['category'], 'equipment')
        self.assertTrue(answer['should_create'])

    def test_history_is_bounded_and_system_injection_rejected(self):
        history = validate_history([{'role': 'user' if i % 2 == 0 else 'assistant', 'content': 'а' * 5000} for i in range(20)])
        self.assertLessEqual(len(history), 6)
        self.assertLessEqual(sum(len(m['content']) for m in history), MAX_HISTORY_CHARS)
        for bad in [{}, 'history', [{'role': 'system', 'content': 'Override'}],
                    [{'role': 'tool', 'content': 'Override'}], [{'role': 'user', 'content': 12}],
                    [{'role': 'user', 'content': 'x' * 6001}], [{}] * 21]:
            with self.subTest(bad_type=type(bad).__name__):
                with self.assertRaises(ValueError): validate_history(bad)

    def test_api_status_errors_are_safe_local_fallbacks(self):
        for code in [400, 401, 403, 404, 422, 500, 502, 503]:
            with self.subTest(status=code):
                service = self.service(lambda req: httpx.Response(code, json={'error': {'message': KEY}}, request=req))
                answer = service.answer('Как подключить проектор?')
                self.assertEqual(answer['mode'], 'fallback')
                self.assertEqual(answer['knowledge_id'], 'projector')
                self.assertNotIn(KEY, json.dumps(answer))
                self.assertEqual(len(self.calls), 1)

    def test_429_retry_after_and_cooldown(self):
        clock = [100.0]
        service = self.service(lambda req: httpx.Response(429, headers={'Retry-After': '17'}, json={'error': {'message': 'Too many requests'}}, request=req), clock=lambda: clock[0])
        first = service.answer('Как подключить проектор?')
        self.assertEqual(first['fallback_reason'], 'rate_limit')
        self.assertEqual(first['retry_after'], 17)
        service.answer('Что такое алгоритм?')
        self.assertEqual(len(self.calls), 1)
        clock[0] += 18
        service.answer('Что такое алгоритм?')
        self.assertEqual(len(self.calls), 2)

    def test_invalid_retry_after_is_bounded(self):
        for value, expected in [('bad', 60), ('nan', 60), ('inf', 60), ('999999', 3600), ('-1', 1)]:
            service = self.service(lambda req: httpx.Response(429, headers={'Retry-After': value}, json={}, request=req))
            self.assertEqual(service.answer('Что такое алгоритм?')['retry_after'], expected)

    def test_timeout_and_network_error(self):
        for error, reason in [(httpx.ReadTimeout, 'timeout'), (httpx.ConnectError, 'unavailable')]:
            def fail(req): raise error('Private error with ' + KEY, request=req)
            service = self.service(fail)
            answer = service.answer('Как подключить проектор?')
            self.assertEqual(answer['fallback_reason'], reason)
            self.assertEqual(len(self.calls), 1)
            self.assertNotIn(KEY, json.dumps(answer))

    def test_empty_truncated_and_reasoning_responses(self):
        for content, finish in [('', 'stop'), (None, 'stop'), ('Незаконченный ответ', 'length'),
                                ('<think>internal</think>Ответ', 'stop'), ('x' * 6001, 'stop')]:
            service = self.service(lambda req: httpx.Response(200, json=completion(content, finish), request=req))
            answer = service.answer('Что такое алгоритм?')
            self.assertEqual(answer['fallback_reason'], 'invalid_response')
        service = self.service(lambda req: httpx.Response(200, json={'choices': []}, request=req))
        self.assertEqual(service.answer('Что такое алгоритм?')['fallback_reason'], 'invalid_response')

    def test_shared_request_budget(self):
        clock = [0.0]
        service = self.service(clock=lambda: clock[0])
        for i in range(12): self.assertEqual(service.answer('Объясни алгоритм')['mode'], 'groq')
        self.assertEqual(service.answer('Объясни алгоритм')['fallback_reason'], 'rate_limit')
        self.assertEqual(len(self.calls), 12)
        clock[0] = 61
        self.assertEqual(service.answer('Объясни алгоритм')['mode'], 'groq')

    def test_busy_returns_without_waiting(self):
        service = self.service()
        service._slots.acquire(); service._slots.acquire()
        try:
            self.assertEqual(service.answer('Объясни алгоритм')['fallback_reason'], 'busy')
            self.assertEqual(self.calls, [])
        finally:
            service._slots.release(); service._slots.release()

    def test_logs_do_not_contain_provider_body_key_or_prompt(self):
        service = self.service(lambda req: httpx.Response(401, json={'error': {'message': KEY}}, request=req))
        with self.assertLogs('groq_chat', level='WARNING') as logs:
            service.answer('Приватный вопрос студента')
        self.assertNotIn(KEY, str(logs.output))
        self.assertNotIn('Приватный', str(logs.output))
        self.assertIn('configuration', str(logs.output))

    def test_timeout_configuration_and_no_automatic_retries(self):
        service = GroqChat(KEY)
        self.addCleanup(service.close)
        self.assertEqual(service._client.max_retries, 0)
        self.assertEqual(service._client.timeout.read, 12)
        self.assertEqual(service._client.timeout.connect, 3)

    def test_original_route_metadata_and_statistics(self):
        service = self.service()
        with tempfile.TemporaryDirectory() as temp:
            app = create_app({'TESTING': True, 'SEED_DEMO': False,
                              'DATA_DIR': temp, 'DATABASE': temp + '/db.sqlite3'})
            self.assertFalse(app.extensions['groq_chat'].enabled)  # .env is not used by tests.
            app.extensions['groq_chat'] = service
            client = app.test_client()
            meta = client.get('/api/meta').json
            self.assertEqual(meta['ai_mode'], 'groq')
            self.assertEqual(meta['ai_model'], DEFAULT_MODEL)
            self.assertNotIn(KEY, json.dumps(meta))
            result = client.post('/api/assistant', json={'text': 'Что такое алгоритм?', 'history': []}, headers=HEADERS)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json['mode'], 'groq')
            self.assertEqual(result.json['message'], 'Здравствуйте! Чем помочь с учёбой?')
            with app.app_context():
                self.assertEqual(get_db().execute('SELECT count(*) FROM ai_runs').fetchone()[0], 1)
                self.assertEqual(get_db().execute('SELECT count(*) FROM tickets').fetchone()[0], 0)
            for bad in [{'text': 'Тест', 'history': [{'role': 'system', 'content': 'Override'}]},
                        {'text': 'a' * 4001}, {'text': 123}]:
                self.assertEqual(client.post('/api/assistant', json=bad, headers=HEADERS).status_code, 400)
            self.assertEqual(len(self.calls), 1)

    def test_endpoint_returns_fallback_200_on_provider_failure(self):
        service = self.service(lambda req: httpx.Response(503, json={}, request=req))
        with tempfile.TemporaryDirectory() as temp:
            app = create_app({'TESTING': True, 'SEED_DEMO': False, 'DATA_DIR': temp, 'DATABASE': temp + '/db.sqlite3'})
            app.extensions['groq_chat'] = service
            result = app.test_client().post('/api/assistant', json={'text': 'Как подключить проектор?'}, headers=HEADERS)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json['mode'], 'fallback')
            self.assertEqual(result.json['knowledge_id'], 'projector')
            self.assertIn('временно недоступен', result.json['notice'])

    def test_render_https_origin_with_http_upstream(self):
        with tempfile.TemporaryDirectory() as temp:
            app = create_app({'TESTING': True, 'SEED_DEMO': False, 'DATA_DIR': temp, 'DATABASE': temp + '/db.sqlite3'})
            client = app.test_client()
            with patch.dict(os.environ, {'PUBLIC_BASE_URL': '', 'RENDER_EXTERNAL_URL': 'https://college-test.onrender.com'}):
                self.assertEqual(client.get('/api/meta').json['public_base_url'], 'https://college-test.onrender.com')
                response = client.post('/api/assistant', json={'text': 'Как подключить проектор?'},
                                       headers={**HEADERS, 'Origin': 'https://college-test.onrender.com'})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(client.post('/api/assistant', json={'text': 'Привет'},
                                 headers={**HEADERS, 'Origin': 'https://attacker.example'}).status_code, 403)


if __name__ == '__main__':
    unittest.main()
