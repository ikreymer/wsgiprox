import base64
import six


# ============================================================================
class FixedResolver(object):
    def __init__(self, fixed_prefix='/'):
        self.fixed_prefix = fixed_prefix

    def __call__(self, url, env):
        return self.fixed_prefix + url


# ============================================================================
class ProxyAuthResolver(object):
    DEFAULT_MSG = 'Please enter prefix path'

    def __init__(self, auth_msg=None):
        self.auth_msg = auth_msg or self.DEFAULT_MSG

    def __call__(self, url, env):
        proxy_auth = env.get('HTTP_PROXY_AUTHORIZATION')

        user_pass = self.read_basic_auth(proxy_auth)

        return '/' + user_pass.split(':')[0] + '/' + url

    def require_auth(self, env):
        proxy_auth = env.get('HTTP_PROXY_AUTHORIZATION')

        if not proxy_auth:
            return self.auth_msg

        return None

    def read_basic_auth(self, value):
        user_pass = ''
        parts = value.split(' ', 1)

        if parts[0].lower() == 'basic' and len(parts) == 2:
            user_pass = base64.b64decode(parts[1].encode('utf-8'))

            if six.PY3:  #pragma: no cover
                user_pass = user_pass.decode('utf-8')

        return user_pass

