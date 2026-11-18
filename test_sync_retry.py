import io,json,socket,ssl
from unittest import TestCase
from unittest.mock import patch,Mock
from urllib.error import HTTPError,URLError
import studio_sync as sync


class Response(io.BytesIO):
    def __init__(self,data):super().__init__(data);self.headers={'Content-Length':str(len(data))}
    def __enter__(self):return self
    def __exit__(self,*_):self.close()


class SyncRetry(TestCase):
    def test_tls_eof_retries_at_connection_and_body_read(self):
        class Partial(Response):
            def read1(self,*_):
                if not self.tell():self.seek(1);return b'x'
                raise ssl.SSLEOFError(8,'UNEXPECTED_EOF_WHILE_READING')
        opener=Mock()
        opener.open.side_effect=[URLError(ssl.SSLEOFError(8,'UNEXPECTED_EOF_WHILE_READING')),
                                 Partial(b'xx'),Response(b'complete')]
        logs=[]
        with patch.object(sync.request,'build_opener',return_value=opener),patch.object(sync,'backoff'):
            self.assertEqual(sync.fetch(sync.WEB,log=logs.append),b'complete')
        self.assertEqual(opener.open.call_count,3)
        self.assertEqual(sum('TLS 连接提前关闭' in s and '自动重试' in s for s in logs),2)

    def test_certificate_error_fails_without_retry_or_insecure_fallback(self):
        opener=Mock()
        opener.open.side_effect=URLError(ssl.SSLCertVerificationError(1,'certificate verify failed'))
        with patch.object(sync.request,'build_opener',return_value=opener):
            with self.assertRaisesRegex(sync.SyncNetworkError,'certificate verify failed'):
                sync.fetch(sync.WEB)
        self.assertEqual(opener.open.call_count,1)

    def test_timeout_retries_and_discards_partial_response(self):
        class Partial(Response):
            def read1(self,*_):
                if not self.tell():self.seek(1);return b'x'
                raise socket.timeout('The read operation timed out')
        opener=Mock();opener.open.side_effect=[Partial(b'xx'),Response(b'complete')]
        logs=[]
        with patch.object(sync.request,'build_opener',return_value=opener),patch.object(sync,'backoff'):
            self.assertEqual(sync.fetch(sync.WEB,log=logs.append),b'complete')
        self.assertEqual(opener.open.call_count,2)
        self.assertEqual([c.kwargs['timeout'] for c in opener.open.call_args_list],[20,30])
        self.assertTrue(any('自动重试' in s for s in logs))

    def test_exhaustion_has_actionable_error_and_cancel_does_not_retry(self):
        opener=Mock();opener.open.side_effect=socket.timeout()
        with patch.object(sync.request,'build_opener',return_value=opener),patch.object(sync,'backoff'):
            with self.assertRaisesRegex(sync.SyncNetworkError,'继续搜索'):
                sync.fetch(sync.WEB)
        self.assertEqual(opener.open.call_count,3)
        opener.reset_mock();opener.open.side_effect=InterruptedError('paused')
        with patch.object(sync.request,'build_opener',return_value=opener):
            with self.assertRaises(InterruptedError):sync.fetch(sync.WEB)
        self.assertEqual(opener.open.call_count,1)

    def test_api_quota_uses_git_fallback_and_success_uses_api(self):
        sha='a'*40;calls=[]
        def limited(url):
            calls.append(url)
            if 'api.github.com' in url:raise HTTPError(url,403,'quota',{},None)
            return (sha+' refs/heads/main\n').encode()
        self.assertEqual(sync.main_head(limited),sha)
        self.assertEqual(len(calls),2)
        get=Mock(return_value=json.dumps({'ref':'refs/heads/main','object':{'type':'commit','sha':sha}}).encode())
        self.assertEqual(sync.main_head(get),sha)
        self.assertEqual(get.call_count,1)

