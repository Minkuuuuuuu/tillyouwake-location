"""No network or real cookies: check the installed library's failure/redirect path."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests
from pyicloud import PyiCloudService
import collector


class ResumeTransport(unittest.TestCase):
    def test_failure_never_redirects_or_retries_token_post(self):
        for status in (307, 308, 401, 421, 450, 'timeout'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                calls = []
                made = []

                class Adapter(requests.adapters.HTTPAdapter):
                    def send(self, request, **kwargs):
                        calls.append((request.method, request.url, kwargs))
                        if status == 'timeout':
                            raise requests.exceptions.ReadTimeout('offline timeout')
                        response = requests.Response()
                        response.status_code = status
                        response.request = request
                        response.url = request.url
                        response.headers['Content-Type'] = 'application/json'
                        response.headers['Location'] = 'https://redirect.invalid/accountLogin'
                        response._content = b'{"success":false}'
                        return response

                def factory(*args, **kwargs):
                    api = PyiCloudService(*args, **kwargs)
                    api.session.data.update(session_token='offline-token', trust_token='offline-trust')
                    api.session.mount('https://', Adapter(max_retries=0))
                    made.append(api)
                    return api

                with patch('pyicloud.PyiCloudService', factory), patch.object(collector, 'STATE_DIR', Path(directory)), \
                     patch.dict(os.environ, {'ICLOUD_APPLE_ID': 'offline@example.invalid'}), \
                     contextlib.redirect_stdout(io.StringIO()):
                    result = collector.resume({'icloud_region': 'cn'})
                self.assertNotEqual(result, 0)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][:2], ('POST', 'https://setup.icloud.com.cn/setup/ws/1/accountLogin'))
                self.assertEqual(calls[0][2]['timeout'], collector.HTTP_TIMEOUT)
                self.assertIs(calls[0][2]['verify'], True)
                self.assertIs(made[0].session.trust_env, False)


if __name__ == '__main__':
    unittest.main()
