import http.client
import io
import importlib.util
import os
import re
import tempfile
import time
import unittest
from urllib.parse import urlencode
from unittest.mock import patch


class ShareallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'DATA_DIR': cls.temp.name, 'ADMIN_USER': 'tester', 'ADMIN_PASSWORD': 'test-secret'}):
            spec = importlib.util.spec_from_file_location('shareall_test_app', 'app.py')
            cls.app = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        with self.app.db() as con:
            con.execute('DELETE FROM shares')
        self.app.sessions.clear()
        self.app.attempts.clear()
        self.cookie = ''

    def request(self, method, path, data=None, content_type='application/x-www-form-urlencoded', auth=True):
        if isinstance(data, dict):
            data = urlencode(data).encode()
        data = data or b''
        headers = {'Content-Type': content_type, 'Content-Length': str(len(data)), 'Host': 'localhost'}
        if auth and self.cookie:
            headers['Cookie'] = self.cookie
        raw = f'{method} {path} HTTP/1.0\r\n'.encode()
        raw += ''.join(f'{key}: {value}\r\n' for key, value in headers.items()).encode() + b'\r\n' + data

        class Connection:
            def __init__(self):
                self.output = io.BytesIO()
            def makefile(self, *args):
                return io.BytesIO(raw)
            def sendall(self, value):
                self.output.write(value)

        connection = Connection()
        self.app.Handler(connection, ('127.0.0.1', 12345), None)

        class ResponseSocket:
            def makefile(self, *args):
                return io.BytesIO(connection.output.getvalue())

        response = http.client.HTTPResponse(ResponseSocket())
        response.begin()
        return response.status, dict(response.getheaders()), response.read()

    def login(self):
        status, headers, _ = self.request('POST', '/login', {'username': 'tester', 'password': 'test-secret'})
        self.assertEqual(status, 303)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        status, _, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        return re.search(rb'name="csrf" value="([^"]+)"', body)[1].decode()

    def create(self, kind='text', content='contenido', csrf=None):
        csrf = csrf or self.login()
        self.assertEqual(self.request('POST', '/share', {'csrf': csrf, 'kind': kind, 'title': 'Prueba <script>', 'content': content})[0], 303)
        with self.app.db() as con:
            return dict(con.execute('SELECT * FROM shares ORDER BY created DESC').fetchone())

    def test_login_access_and_csrf(self):
        self.assertIn(b'Bienvenido', self.request('GET', '/')[2])
        self.assertEqual(self.request('POST', '/share', {'kind': 'text'})[0], 403)
        self.assertEqual(self.request('POST', '/login', {'username': 'tester', 'password': 'wrong'})[1]['Location'], '/login?error=1')
        csrf = self.login()
        self.assertIn(b'Nuevo compartido', self.request('GET', '/')[2])
        self.assertEqual(self.request('POST', '/share', {'csrf': 'bad'})[0], 403)
        self.assertEqual(self.request('POST', '/logout', {'csrf': csrf})[0], 303)
        self.assertIn(b'Bienvenido', self.request('GET', '/')[2])

    def test_text_listing_public_access_and_exact_expiry(self):
        row = self.create(content='<script>alert(1)</script>')
        self.assertEqual(row['expires'] - row['created'], 3600)
        self.assertIn(row['id'].encode(), self.request('GET', '/')[2])
        status, _, body = self.request('GET', '/s/' + row['id'], auth=False)
        self.assertEqual(status, 200)
        self.assertIn(b'&lt;script&gt;', body)
        self.assertNotIn(b'<script>alert', body)
        with patch.object(self.app.time, 'time', return_value=row['expires'] - .01):
            self.assertEqual(self.request('GET', '/s/' + row['id'], auth=False)[0], 200)
        with patch.object(self.app.time, 'time', return_value=row['expires']):
            self.assertEqual(self.request('GET', '/s/' + row['id'], auth=False)[0], 404)
        with self.app.db() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM shares').fetchone()[0], 0)

    def test_links_and_invalid_scheme(self):
        csrf = self.login()
        self.assertEqual(self.request('POST', '/share', {'csrf': csrf, 'kind': 'link', 'title': 'bad', 'content': 'javascript:alert(1)'})[0], 400)
        row = self.create('link', 'https://example.com/?a=1&b=2', csrf)
        self.assertIn(b'href="https://example.com/?a=1&amp;b=2"', self.request('GET', '/s/' + row['id'], auth=False)[2])

    def upload(self, title="Documento"):
        csrf = self.login()
        boundary = 'shareall-test-boundary'
        parts = []
        for key, value in {'csrf': csrf, 'kind': 'file', 'title': title}.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        payload = b'\x00documento\xff\n'
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="documento.pdf"\r\nContent-Type: application/pdf\r\n\r\n'.encode() + payload + b'\r\n')
        parts.append(f'--{boundary}--\r\n'.encode())
        self.assertEqual(self.request('POST', '/share', b''.join(parts), 'multipart/form-data; boundary=' + boundary)[0], 303)
        with self.app.db() as con:
            row = dict(con.execute('SELECT * FROM shares').fetchone())
        return csrf, row, payload

    def test_file_download_expiry_and_physical_cleanup(self):
        _, row, payload = self.upload()
        status, headers, body = self.request('GET', '/s/' + row['id'] + '?download=1', auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        self.assertIn('attachment', headers['Content-Disposition'])
        with patch.object(self.app.time, 'time', return_value=row['expires']):
            self.assertEqual(self.request('GET', '/s/' + row['id'] + '?download=1', auth=False)[0], 404)
        self.assertFalse((self.app.FILES / row['id']).exists())

    def test_background_cleanup_without_requests_and_manual_delete(self):
        csrf, row, _ = self.upload()
        with self.app.db() as con:
            con.execute('UPDATE shares SET expires=?', (time.time() - 1,))
        self.app.cleanup()
        self.assertFalse((self.app.FILES / row['id']).exists())
        row = self.create(csrf=csrf)
        self.assertEqual(self.request('POST', '/delete', {'csrf': csrf, 'id': row['id']})[0], 303)
        self.assertEqual(self.request('GET', '/s/' + row['id'], auth=False)[0], 404)

    def test_janitor_runs_without_http_traffic(self):
        _, row, _ = self.upload()
        with self.app.db() as con:
            con.execute('UPDATE shares SET expires=?', (time.time() - 1,))
        # Stop after the first real cleanup iteration, without waiting ten seconds.
        with patch.object(self.app.time, 'sleep', side_effect=InterruptedError):
            with self.assertRaises(InterruptedError):
                self.app.janitor()
        self.assertFalse((self.app.FILES / row['id']).exists())
        with self.app.db() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM shares').fetchone()[0], 0)

    def test_persistent_data_survives_module_reload(self):
        _, row, payload = self.upload()
        with patch.dict(os.environ, {'DATA_DIR': self.temp.name}):
            spec = importlib.util.spec_from_file_location('restarted_shareall', 'app.py')
            restarted = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(restarted)
        with restarted.db() as con:
            saved = con.execute('SELECT * FROM shares WHERE id=?', (row['id'],)).fetchone()
        self.assertEqual(saved['filename'], 'documento.pdf')
        self.assertEqual((restarted.FILES / row['id']).read_bytes(), payload)
        self.assertEqual(restarted.sessions, {})

    def test_optional_title(self):
        csrf = self.login()
        form = self.request('GET', '/')[2]
        title_input = re.search(rb'<input name="title"[^>]*>', form)[0]
        self.assertNotIn(b'required', title_input)
        for kind, content, expected in (
            ('text', 'Un texto', 'Texto sin título'),
            ('link', 'https://example.com', 'Enlace sin título'),
        ):
            for title in (None, '', '   '):
                fields = {'csrf': csrf, 'kind': kind, 'content': content}
                if title is not None:
                    fields['title'] = title
                self.assertEqual(self.request('POST', '/share', fields)[0], 303)
                with self.app.db() as con:
                    row = con.execute('SELECT * FROM shares ORDER BY created DESC').fetchone()
                self.assertEqual(row['title'], expected)
                self.assertIn(expected.encode(), self.request('GET', '/s/' + row['id'], auth=False)[2])
        with self.app.db() as con:
            con.execute('DELETE FROM shares')
        _, row, _ = self.upload(title='')
        self.assertEqual(row['title'], 'documento.pdf')

    def test_upload_limit(self):
        with patch.object(self.app, 'MAX_SIZE', 1):
            self.assertEqual(self.request('POST', '/share', b'x' * 65538)[0], 413)


if __name__ == '__main__':
    unittest.main()
