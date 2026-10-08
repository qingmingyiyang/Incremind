"""Node's actual TLS HTTP2 server; independent sessions survive model abort."""
import ipaddress
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from threading import Event, Thread

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tests.backend.unit.llm.test_owned_model_transport import real_owned_call


NODE = r'''
const http2 = require('node:http2');
const fs = require('node:fs');
const server = http2.createSecureServer({key:fs.readFileSync(process.argv[2]),
  cert:fs.readFileSync(process.argv[3])});
let calls = 0, unrelated;
const body = JSON.stringify({id:'synthetic',object:'chat.completion',model:'gpt-5.4-mini',
  choices:[{index:0,finish_reason:'stop',message:{role:'assistant',content:'{"answer":"done"}'}}],
  usage:{prompt_tokens:4,completion_tokens:2,total_tokens:6}});
server.on('session', session => {
  session.on('error',()=>{});
  session.on('close',()=>console.log(JSON.stringify({closed:true})));
});
server.on('stream',(stream,headers)=>{
  stream.on('error',()=>{});
  if(headers[':path']==='/unrelated'){
    unrelated=stream; stream.respond({':status':200}); stream.write('first');
    console.log(JSON.stringify({unrelated:true})); return;
  }
  if(headers[':path']==='/release'){
    if(unrelated && !unrelated.destroyed) unrelated.end('second');
    stream.respond({':status':200}); stream.end('released'); return;
  }
  stream.on('data',()=>{});
  stream.on('end',()=>{
    calls++; console.log(JSON.stringify({call:calls}));
    stream.respond({':status':200,'content-type':'application/json'});
    if(calls===1){stream.write(body.slice(0,1));
      setTimeout(()=>{if(!stream.destroyed) stream.end(body.slice(1));},180);
    }else stream.end(body);
  });
});
server.listen(0,'127.0.0.1',()=>console.log(JSON.stringify({port:server.address().port})));
'''


class H2Provider:
    mode = 'idle'

    def __init__(self, root):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'synthetic localhost')])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),
                x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
        key_path, cert_path, script = root / 'synthetic-key.pem', root / 'synthetic-cert.pem', root / 'provider.cjs'
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        script.write_text(NODE, encoding='utf-8')
        self.certificate = str(cert_path)
        self.calls, self.closed = [], []
        self.ready, self.unrelated = Event(), Event()
        self.process = subprocess.Popen([shutil.which('node'), str(script), str(key_path), str(cert_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

        def read():
            for line in self.process.stdout:
                value = json.loads(line)
                if 'port' in value:
                    self.base = f"https://127.0.0.1:{value['port']}/v1"
                    self.ready.set()
                if 'call' in value:
                    self.calls.append(value['call'])
                if 'closed' in value:
                    self.closed.append(True)
                if 'unrelated' in value:
                    self.unrelated.set()

        self.reader = Thread(target=read, daemon=True)
        self.reader.start()
        assert self.ready.wait(5), 'local Node HTTP2 server did not start'

    def close(self):
        self.process.terminate()
        self.process.wait(timeout=5)
        self.reader.join(timeout=5)


def test_actual_h2_owned_abort_leaves_borrowed_other_session_alive(tmp_path, monkeypatch):
    import litellm
    provider = H2Provider(tmp_path)
    clients, unrelated, failures = [], [], []
    shared = httpx.Client(http2=True, verify=provider.certificate)
    monkeypatch.setattr(litellm, 'client_session', shared)

    def read_unrelated():
        try:
            response = shared.get(provider.base.replace('/v1', '/unrelated'), timeout=5)
            unrelated.append((response.http_version, response.text))
        except BaseException as error:
            failures.append(type(error).__name__)

    other = Thread(target=read_unrelated, daemon=True)
    other.start()
    assert provider.unrelated.wait(5), 'independent HTTP2 stream did not start'
    original_hooks = {kind: list(values) for kind, values in shared.event_hooks.items()}

    def factory():
        client = httpx.Client(http2=True, verify=provider.certificate)
        clients.append(client)
        return client

    try:
        receipt, retries, output, terminals, _ = real_owned_call(tmp_path, provider, factory=factory)
        assert receipt.status == 'completed' and output == ['done']
        assert len(retries) == 1 and retries[0]['budget'] == 'before_output'
        assert len(clients) == len(provider.calls) == len(terminals) == 2
        assert all(client.is_closed for client in clients)
        assert [row['status'] for row in terminals] == ['failed_transport', 'succeeded']
        assert shared.is_closed is False and shared.event_hooks == original_hooks
        assert other.is_alive() and unrelated == failures == []
        # The unrelated stream resumes on its original session after model abort.
        with httpx.Client(http2=True, verify=provider.certificate) as control:
            assert control.get(provider.base.replace('/v1', '/release')).http_version == 'HTTP/2'
        other.join(timeout=5)
        assert unrelated == [('HTTP/2', 'firstsecond')] and failures == []
    finally:
        if other.is_alive():
            with httpx.Client(http2=True, verify=provider.certificate) as control:
                control.get(provider.base.replace('/v1', '/release'))
        other.join(timeout=5)
        shared.close()
        for client in clients:
            client.close()
        provider.close()
