import hashlib
import hmac
import html
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from email.parser import BytesParser
from email.policy import default
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit


def load_env():
    if Path('.env').exists():
        for line in Path('.env').read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('\"\''))


load_env()
DATA = Path(os.getenv('DATA_DIR', 'data'))
DATA.mkdir(parents=True, exist_ok=True)
FILES = DATA / 'files'
FILES.mkdir(exist_ok=True)
USER = os.getenv('ADMIN_USER', 'admin')
PASSWORD = os.getenv('ADMIN_PASSWORD', '')
MAX_SIZE = int(os.getenv('MAX_UPLOAD_MB', '25')) * 1024 * 1024
TTL = 3600
sessions = {}
attempts = {}
lock = threading.RLock()


@contextmanager
def db():
    con = sqlite3.connect(DATA / 'shareall.db')
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


with db() as con:
    con.execute('CREATE TABLE IF NOT EXISTS shares (id TEXT PRIMARY KEY, kind TEXT, title TEXT, content TEXT, filename TEXT, created REAL, expires REAL)')


def cleanup():
    with lock, db() as con:
        expired = con.execute('SELECT id FROM shares WHERE expires <= ?', (time.time(),)).fetchall()
        for row in expired:
            (FILES / row['id']).unlink(missing_ok=True)
        con.execute('DELETE FROM shares WHERE expires <= ?', (time.time(),))
        now = time.time()
        for token in list(sessions):
            if sessions[token]['expires'] <= now:
                del sessions[token]
        for ip in list(attempts):
            if attempts[ip][1] <= now:
                del attempts[ip]


def janitor():
    while True:
        cleanup()
        time.sleep(10)


def esc(value):
    return html.escape(str(value), quote=True)


def page(title, body):
    return f'''<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · Shareall</title><link rel="stylesheet" href="/style.css"><body><header><a href="/">shareall<span> / comparte lo que importa</span></a><small>EFÍMERO POR DISEÑO</small></header><main>{body}</main><footer>Un enlace. Una hora. Sin dejarlo para siempre.</footer><script src="/app.js" defer></script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Do not log private share tokens or login data.
        pass

    def send(self, status, body=b'', content_type='text/html; charset=utf-8', headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, path, headers=None):
        self.send(303, headers={'Location': path, **(headers or {})})

    def session(self):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            token = cookie['session'].value if 'session' in cookie else ''
            session = sessions.get(token)
            return session if session and session['expires'] > time.time() else None
        except Exception:
            return None

    def do_GET(self):
        cleanup()
        path = urlsplit(self.path).path
        if path in ('/style.css', '/app.js'):
            self.send(200, Path('static' + path).read_bytes(), 'text/css' if path.endswith('css') else 'text/javascript')
            return
        if path == '/health':
            self.send(200, 'ok', 'text/plain')
            return
        if path.startswith('/s/'):
            token = path[3:]
            with db() as con:
                row = con.execute('SELECT * FROM shares WHERE id=? AND expires>?', (token, time.time())).fetchone()
            if not row:
                self.send(404, page('No disponible', '<section class="card"><h1>Este compartido ya no está disponible.</h1><p>El enlace ha caducado o no existe.</p></section>'))
                return
            if row['kind'] == 'file' and urlsplit(self.path).query == 'download=1':
                with lock:
                    if row['expires'] <= time.time() or not (FILES / token).exists():
                        self.send(404)
                        return
                    content = (FILES / token).read_bytes()
                self.send(200, content, 'application/octet-stream', {'Content-Disposition': "attachment; filename*=UTF-8''" + quote(row['filename'], safe='')})
                return
            content = ('<pre>' + esc(row['content']) + '</pre>') if row['kind'] == 'text' else (f'<a class="button" rel="noopener noreferrer" href="{esc(row["content"])}">Abrir enlace ↗</a>' if row['kind'] == 'link' else f'<p>{esc(row["filename"])}</p><a class="button" href="?download=1">Descargar archivo ↓</a>')
            self.send(200, page(row['title'], f'<p class="eyebrow">COMPARTIDO CONTIGO · CADUCA EN UNA HORA</p><section class="card"><h1>{esc(row["title"])}</h1>{content}<p class="muted">Disponible hasta {time.strftime("%H:%M UTC", time.gmtime(row["expires"]))}</p></section>'))
            return
        if path not in ('/', '/login'):
            self.send(404)
            return
        session = self.session()
        if not session:
            error = '<p class="error">Credenciales incorrectas. Inténtalo de nuevo.</p>' if urlsplit(self.path).query else ''
            self.send(200, page('Entrar', f'<section class="login"><p class="eyebrow">TU ESPACIO PARA COMPARTIR</p><h1>Comparte ahora.<br>Desaparece después.</h1><p class="muted">Enlaces, textos y archivos disponibles durante una hora.</p><form class="card" method="post" action="/login"><h2>Bienvenido de nuevo</h2>{error}<label>Usuario<input name="username" autocomplete="username" required></label><label>Contraseña<input type="password" name="password" autocomplete="current-password" required></label><button>Entrar en mi espacio →</button></form></section>'))
            return
        with db() as con:
            rows = con.execute('SELECT * FROM shares ORDER BY created DESC').fetchall()
        csrf = f'<input type="hidden" name="csrf" value="{session["csrf"]}">'
        items = ''
        for row in rows:
            items += f'<article class="item"><div><span class="badge">{esc({"text":"TEXTO", "link":"ENLACE", "file":"ARCHIVO"}[row["kind"]])}</span><h3>{esc(row["title"])}</h3><small class="muted" data-expires="{row["expires"]}">Caduca en {max(0, int((row["expires"]-time.time())/60))} min</small></div><div class="actions"><a href="/s/{row["id"]}">Ver ↗</a><button type="button" class="secondary copy" data-path="/s/{row["id"]}">Copiar enlace</button><form method="post" action="/delete">{csrf}<input type="hidden" name="id" value="{row["id"]}"><button class="danger">Eliminar</button></form></div></article>'
        self.send(200, page('Mi espacio', f'<div class="top"><p class="eyebrow">MI ESPACIO / {esc(USER)}</p><form action="/logout" method="post">{csrf}<button class="secondary">Cerrar sesión</button></form></div><h1>Algo que compartir.</h1><p class="muted">Compártelo con un enlace. Nosotros nos encargamos del reloj.</p><section class="card"><div class="top"><h2>Nuevo compartido</h2><span class="badge">◷ DURACIÓN: 1 HORA</span></div><form method="post" action="/share" enctype="multipart/form-data">{csrf}<label>¿Qué vas a compartir?<select name="kind" id="kind"><option value="text">Texto</option><option value="link">Enlace</option><option value="file">Archivo</option></select></label><label>Título (opcional)<input name="title" maxlength="160" placeholder="Ponle un nombre para encontrarlo fácilmente"></label><label id="content-field"><span id="content-label">Texto</span><textarea name="content" rows="5" placeholder="Escribe o pega aquí el contenido"></textarea></label><label id="file-field" hidden>Archivo (máximo {MAX_SIZE // 1024 // 1024} MB)<input type="file" name="file"></label><div class="top"><small class="muted">Cualquiera con el enlace podrá acceder hasta que caduque.</small><button>Crear enlace →</button></div></form></section><div class="top list-title"><h2>Compartidos recientes</h2><span class="muted">{len(rows)} activos</span></div><section class="card list">{items or "<p class=\"muted empty\">Tu próximo compartido empieza arriba. Todavía no hay elementos activos.</p>"}</section><p id="notice" role="status"></p>'))

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > MAX_SIZE + 65536:
                self.send(413, page('Archivo demasiado grande', '<h1>La solicitud supera el tamaño permitido.</h1><a href="/">Volver</a>'))
                return
            raw = self.rfile.read(length)
            if self.headers.get('Content-Type', '').startswith('multipart/form-data'):
                msg = BytesParser(policy=default).parsebytes(b'Content-Type: ' + self.headers['Content-Type'].encode() + b'\r\nMIME-Version: 1.0\r\n\r\n' + raw)
                fields, upload = {}, None
                for part in msg.iter_parts():
                    name = part.get_param('name', header='content-disposition')
                    if name == 'file':
                        upload = (part.get_filename(), part.get_payload(decode=True))
                    else:
                        fields[name] = part.get_content()
            else:
                fields = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                upload = None
            path = urlsplit(self.path).path
            if path == '/login':
                ip = self.client_address[0]
                with lock:
                    count, until = attempts.get(ip, (0, time.time() + 300))
                    if until <= time.time():
                        count, until = 0, time.time() + 300
                    if count >= 10:
                        self.send(429, 'Demasiados intentos. Vuelve a intentarlo en 5 minutos.', 'text/plain; charset=utf-8')
                        return
                    valid = hmac.compare_digest(fields.get('username', '').encode(), USER.encode()) and hmac.compare_digest(hashlib.sha256(fields.get('password', '').encode()).digest(), hashlib.sha256(PASSWORD.encode()).digest())
                    if not valid:
                        attempts[ip] = (count + 1, until)
                        self.redirect('/login?error=1')
                        return
                    attempts.pop(ip, None)
                    token = secrets.token_urlsafe(32)
                    sessions[token] = {'csrf': secrets.token_urlsafe(32), 'expires': time.time() + 43200}
                secure = '; Secure' if os.getenv('COOKIE_SECURE', 'false').lower() == 'true' else ''
                self.redirect('/', {'Set-Cookie': f'session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200{secure}'})
                return
            session = self.session()
            if not session or not hmac.compare_digest(fields.get('csrf', ''), session['csrf']):
                self.send(403, 'Sesión no válida. Vuelve a entrar.', 'text/plain; charset=utf-8')
                return
            if path == '/logout':
                with lock:
                    for token in list(sessions):
                        if sessions[token] is session:
                            del sessions[token]
                self.redirect('/login', {'Set-Cookie': 'session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict'})
            elif path == '/delete':
                with lock, db() as con:
                    token = fields.get('id', '')
                    row = con.execute('SELECT id FROM shares WHERE id=?', (token,)).fetchone()
                    if row:
                        (FILES / row['id']).unlink(missing_ok=True)
                        con.execute('DELETE FROM shares WHERE id=?', (token,))
                self.redirect('/')
            elif path == '/share':
                kind, title, content = fields.get('kind'), fields.get('title', '').strip(), fields.get('content', '').strip()
                filename = ''
                if kind not in ('text', 'link', 'file'):
                    raise ValueError('Indica un tipo válido.')
                if len(title) > 160:
                    raise ValueError('El título no puede superar los 160 caracteres.')
                if kind == 'link':
                    url = urlsplit(content)
                    if url.scheme not in ('https', 'http') or not url.hostname or any(ord(c) < 32 for c in content):
                        raise ValueError('El enlace debe ser una URL http o https válida.')
                if kind in ('text', 'link') and not content:
                    raise ValueError('El contenido no puede estar vacío.')
                if kind == 'file':
                    if not upload or not upload[0] or len(upload[1]) > MAX_SIZE:
                        raise ValueError('Selecciona un archivo dentro del límite permitido.')
                    filename = Path(upload[0].replace('\\', '/')).name
                    content = ''
                if not title:
                    title = filename[:160] if kind == 'file' else {'text': 'Texto sin título', 'link': 'Enlace sin título'}[kind]
                token, now = secrets.token_urlsafe(24), time.time()
                with lock:
                    try:
                        if kind == 'file':
                            (FILES / token).write_bytes(upload[1])
                        with db() as con:
                            con.execute('INSERT INTO shares VALUES (?,?,?,?,?,?,?)', (token, kind, title, content, filename, now, now + TTL))
                    except Exception:
                        (FILES / token).unlink(missing_ok=True)
                        raise
                self.redirect('/')
            else:
                self.send(404)
        except (ValueError, UnicodeError) as exc:
            self.send(400, page('Revisa el formulario', f'<section class="card"><h1>Revisa el formulario</h1><p>{esc(exc)}</p><a href="/">Volver</a></section>'))


if __name__ == '__main__':
    if not PASSWORD:
        raise SystemExit('Configura ADMIN_PASSWORD en .env antes de arrancar.')
    cleanup()
    threading.Thread(target=janitor, daemon=True).start()
    server = ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '8000'))), Handler)
    print('Shareall disponible en el puerto', server.server_port, flush=True)
    server.serve_forever()
