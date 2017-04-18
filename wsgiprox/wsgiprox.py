from certauth.certauth import CertificateAuthority

import socket
import ssl

from six.moves.urllib.parse import quote, urlsplit
import six

from geventwebsocket.handler import WebSocketHandler
import logging


# ============================================================================
class WrappedWebSockHandler(WebSocketHandler):
    def __init__(self, sock, environ, start_response, reader):
        self.environ = environ
        self.start_response = start_response
        self.request_version = 'HTTP/1.1'
        self._logger = logging.getLogger(__file__)

        self.socket = sock
        self.rfile = reader

    @property
    def logger(self):
        return self._logger


# ============================================================================
class WSGIProxMiddleware(object):
    DEF_MAGIC_NAME = 'wsgiprox'

    CERT_DL_PEM = '/wsgiprox-ca.pem'
    CERT_DL_P12 = '/wsgiprox-ca.p12'

    CA_ROOT_FILE = './ca/pywb-ca.pem'
    CA_ROOT_NAME = 'wsgiprox https proxy replay CA'
    CA_CERTS_DIR = './ca/certs/'

    def __init__(self, wsgi, prefix_resolver, proxy_options=None):
        self.wsgi = wsgi
        self.prefix_resolver = prefix_resolver

        # HTTPS Only Options
        proxy_options = proxy_options or {}

        ca_file = proxy_options.get('root_ca_file', self.CA_ROOT_FILE)

        # attempt to create the root_ca_file if doesn't exist
        # (generally recommended to create this seperately)
        ca_name = proxy_options.get('root_ca_name', self.CA_ROOT_NAME)

        certs_dir = proxy_options.get('certs_dir', self.CA_CERTS_DIR)
        self.ca = CertificateAuthority(ca_file=ca_file,
                                       certs_dir=certs_dir,
                                       ca_name=ca_name)

        self.use_wildcard = proxy_options.get('use_wildcard_certs', True)

    def __call__(self, env, start_response):
        if env['REQUEST_METHOD'] == 'CONNECT':
            return self.handle_connect(env, start_response)
        else:
            if env['PATH_INFO'].startswith('http://'):
                self.conv_http_env(env)

            return self.wsgi(env, start_response)

    def handle_connect(self, env, start_response):
        curr_sock = self.get_raw_socket(env)
        if not curr_sock:
            start_response('405 HTTPS Proxy Not Supported',
                           [('Content-Length', '0')], exc_info)
            return []

        ssl_sock = None

        def ssl_start_response(statusline, headers, exc_info=None):
            status_line = 'HTTP/1.1 ' + statusline + '\r\n'
            ssl_sock.write(status_line.encode('iso-8859-1'))

            for name, value in headers:
                line = name + ': ' + value + '\r\n'
                ssl_sock.write(line.encode('iso-8859-1'))

        ssl_sock = self.wrap_socket(env['PATH_INFO'], curr_sock)

        #buffreader = BufferedReader(ssl_sock, BUFF_SIZE)
        buffreader = ssl_sock.makefile('rb', -1)

        self.conv_https_env(env, buffreader)

        # add websocket
        if env.get('HTTP_UPGRADE', '') == 'websocket':
            ws = WrappedWebSockHandler(ssl_sock, env, ssl_start_response, buffreader)
            result = ws.upgrade_websocket()
            ssl_sock.write(b'\r\n')
            resp_iter = self.wsgi(env, ssl_start_response)
            return []

        resp_iter = self.wsgi(env, ssl_start_response)
        ssl_sock.write(b'\r\n')

        for obj in resp_iter:
            if obj:
                ssl_sock.write(obj)

        buffreader.close()
        ssl_sock.close()

        return []

    def wrap_socket(self, host_port, sock):
        #sock.send(b'HTTP/1.1 200 Connection Established\r\n')
        #sock.send(b'Proxy-Connection: keep-alive\r\n')
        sock.send(b'HTTP/1.0 200 Connection Established\r\n')
        sock.send(b'Proxy-Connection: close\r\n')
        sock.send(b'Server: wsgiprox\r\n')
        sock.send(b'\r\n')

        hostname, port = host_port.split(':')

        if not self.use_wildcard:
            certfile = self.ca.cert_for_host(hostname)
        else:
            certfile = self.ca.get_wildcard_cert(hostname)

        ssl_sock = ssl.wrap_socket(sock,
                                   server_side=True,
                                   certfile=certfile,
                                   suppress_ragged_eofs=False,
                                   ssl_version=ssl.PROTOCOL_SSLv23
                                   )

        return ssl_sock

    def resolve(self, url, env):
        env['REQUEST_URI'] = self.prefix_resolver(url, env)

        queryparts = env['REQUEST_URI'].split('?', 1)

        env['PATH_INFO'] = queryparts[0]

        env['QUERY_STRING'] = queryparts[1] if len(queryparts) > 1 else ''

    def conv_http_env(self, env):
        if 'REQUEST_URI' in env:
            full_uri = env['REQUEST_URI']
        else:
            full_uri = env['PATH_INFO']
            if env.get('QUERY_STRING'):
                full_uri += '?' + env['QUERY_STRING']

        self.resolve(full_uri, env)

    def conv_https_env(self, env, buffreader):
        statusline = buffreader.readline().rstrip()
        if six.PY3:
            statusline = statusline.decode('iso-8859-1')

        statusparts = statusline.split(' ')

        if len(statusparts) < 3:
            raise Exception('Invalid Proxy Request: ' + statusline)

        hostname, port = env['PATH_INFO'].split(':', 1)

        env['wsgi.url_scheme'] = 'https'
        env['wsgiprox.proxy_scheme'] = 'https'

        env['wsgiprox.proxy_host'] = hostname
        env['wsgiprox.proxy_port'] = port

        env['REQUEST_METHOD'] = statusparts[0]

        env['SERVER_PROTOCOL'] = statusparts[2].strip()

        full_uri = 'https://' + hostname + statusparts[1]

        self.resolve(full_uri, env)

        while True:
            line = buffreader.readline()
            if line:
                line = line.rstrip()
                if six.PY3:
                    line = line.decode('iso-8859-1')

            if not line:
                break

            parts = line.split(':', 1)
            if len(parts) < 2:
                continue

            name = parts[0].strip()
            value = parts[1].strip()

            name = name.replace('-', '_').upper()

            if name not in ('CONTENT_LENGTH', 'CONTENT_TYPE'):
                name = 'HTTP_' + name

            env[name] = value

        env['wsgi.input'] = buffreader

    def get_raw_socket(self, env):
        if not self.ca:
            return None

        sock = None

        if env.get('uwsgi.version'):  # pragma: no cover
            try:
                import uwsgi
                fd = uwsgi.connection_fd()
                conn = socket.fromfd(fd, socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock = socket.socket(_sock=conn)
                except:
                    sock = conn
            except Exception as e:
                pass
        elif env.get('gunicorn.socket'):  # pragma: no cover
            sock = env['gunicorn.socket']

        if not sock:
            # attempt to find socket from wsgi.input
            input_ = env.get('wsgi.input')
            if input_:
                if hasattr(input_, '_sock'):  # pragma: no cover
                    raw = input_._sock
                    sock = socket.socket(_sock=raw)  # pragma: no cover
                elif hasattr(input_, 'raw'):
                    sock = input_.raw._sock
                elif hasattr(input_, 'rfile'):
                    sock = input_.rfile.raw._sock

        return sock


# ============================================================================
class FixedResolver(object):
    def __init__(self, fixed_prefix, identity_hosts):
        self.identity_hosts = identity_hosts
        self.fixed_prefix = fixed_prefix

    def __call__(self, url, env):
        parts = urlsplit(url)
        if parts.netloc in self.identity_hosts:
            full = parts.path
            if parts.query:
                full += '?' + parts.query
            return full
        else:
            return self.fixed_prefix + url


