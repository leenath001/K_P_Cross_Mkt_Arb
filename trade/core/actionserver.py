"""
trade/core/actionserver.py — loopback-only HTTP side channel between a custom Streamlit component and a Python object.

The component POSTs actions and GETs fresh snapshots straight to this server, so editing a card never triggers a Streamlit
rerun (which greyed out the page and waited on the owner's lock). Token-gated and bound to 127.0.0.1; the component falls
back to Streamlit's own setComponentValue path when it can't reach it.
"""
import json, math, threading, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def clean(o):
    """JSON-safe: NaN / inf become null."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


class ActionServer:
    def __init__(self, snapshot_fn, action_fn):
        self.token = uuid.uuid4().hex
        self.port = None
        self._httpd = None
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b'{}'):
                self.send_response(code)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.split('?token=')[-1] != srv.token:
                    return self._send(403)
                try:
                    self._send(200, json.dumps(clean(snapshot_fn()), default=str).encode())
                except Exception:
                    self._send(500)

            def do_POST(self):
                try:
                    act = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0)) or 0) or b'{}')
                except ValueError:
                    return self._send(400)
                if act.pop('token', None) != srv.token:
                    return self._send(403)
                threading.Thread(target=action_fn, args=(act,), daemon=True).start()   # never make the browser wait
                self._send(200, b'{"ok":true}')

        try:
            self._httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
            self._httpd.daemon_threads = True
            self.port = self._httpd.server_address[1]
            threading.Thread(target=self._httpd.serve_forever, name='board-actions', daemon=True).start()
        except OSError:
            self._httpd = None

    def info(self):
        return {'port': self.port, 'token': self.token}

    def stop(self):
        if self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
