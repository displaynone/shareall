# Shareall

Comparte textos, enlaces y archivos mediante enlaces públicos aleatorios. Panel privado con usuario definido en `.env`, formulario y listado de compartidos activos. Interfaz en español y adaptable al móvil. Python 3.14 y SQLite, sin dependencias externas.

## Arranque

```sh
cp .env.example .env
# Edita .env: configura ADMIN_USER y una ADMIN_PASSWORD larga y única.
docker compose up -d --build
```

Con Podman y su proveedor Compose instalado:

```sh
podman compose up -d --build
```

También funciona sin Compose:

```sh
podman build -t shareall .
podman volume create shareall-data
podman run -d --name shareall --env-file .env -e PORT=8000 -p 8000:8000 -v shareall-data:/data shareall
```

Abre http://localhost:8000. `PORT` en `.env` cambia el puerto publicado con Compose. Para ejecución nativa: `python3 app.py` (lee `.env` automáticamente).

## Funcionamiento

- Cada compartido caduca exactamente 3600 segundos después de crearse. A partir de entonces su enlace devuelve 404. El proceso elimina registros y archivos caducados cada 10 segundos, también al arrancar y al recibir peticiones. Si el servicio está detenido, la limpieza se ejecuta en el siguiente arranque.
- El listado contiene los compartidos activos; al caducar se borran, sin conservar un historial del contenido. Puedes eliminar un compartido antes de tiempo.
- Los enlaces públicos permiten acceder sin login. Cualquier persona que posea el enlace puede leerlo hasta su caducidad.
- Los archivos se descargan como adjuntos. Límite por archivo configurable mediante `MAX_UPLOAD_MB` (25 por defecto).
- SQLite y archivos viven en el volumen `/data`. No elimines el volumen si quieres conservar compartidos entre reinicios. Las sesiones duran 12 horas y se invalidan al reiniciar.
- Hay protección CSRF y límite de intentos de login (10 fallos por IP cada 5 minutos). El usuario y la contraseña se toman de `.env` al arrancar; reinicia tras cambiarlos.
- Para publicarlo en Internet, coloca un proxy HTTPS delante y configura `COOKIE_SECURE=true`. La aplicación debe ejecutarse como una única instancia con almacenamiento local persistente. El reloj del sistema debe estar sincronizado. No se promete borrado forense de copias de seguridad ni del soporte físico.

## Comprobación

```sh
python3 -m unittest discover -s tests -v
```

`/health` devuelve `ok` para comprobar la disponibilidad del proceso.

Las pruebas ejercitan el manejador HTTP con peticiones y respuestas completas en memoria, sin abrir puertos. Cubren login, CSRF, los tres tipos de compartido, persistencia, descarga, caducidad exacta y limpieza sin peticiones. La construcción y el arranque real del contenedor deben verificarse en un entorno que permita ejecutar Docker o Podman.
