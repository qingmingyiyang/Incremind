"""Real local routes/generation with only Torch/Transformers SDK boundaries replaced."""
from contextlib import nullcontext
from types import SimpleNamespace
from threading import Event,Lock,Thread
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.memory_app.local_model import install_local_model_routes
from backend.memory_app.server_runtime import create_server_application
from backend.shared.deployment import DeploymentLayout


def test_two_actual_user_routes_load_one_installed_qwen_and_serialize_distinct_inputs(tmp_path,monkeypatch):
    entered,release=Event(),Event();loads=[];inputs=[];live=[0,0];lock=Lock()
    class Token:
        def item(self):return 9
    class Tokens(list):
        def __init__(self,text):super().__init__([Token()]);self.text=text
    class Encoded:
        shape=(1,3)
        def __init__(self,text):self.text=text
    class Output:
        def __init__(self,text):self.text=text
        def __getitem__(self,key):return Tokens(self.text)
    class Tokenizer:
        eos_token_id=9
        def apply_chat_template(self,messages,**options):return Encoded(messages[-1]['content'])
        def decode(self,tokens,**options):return tokens.text
    class Engine:
        def eval(self):return self
        def generate(self,encoded,**options):
            with lock:
                live[0]+=1;live[1]=max(live);inputs.append(encoded.text)
            try:
                if encoded.text=='alpha':entered.set();assert release.wait(5)
                return Output(encoded.text)
            finally:
                with lock:live[0]-=1
    def load_tokenizer(path,**options):
        assert options=={'local_files_only':True};loads.append(('tokenizer',path));return Tokenizer()
    def load_model(path,**options):
        assert options=={'local_files_only':True,'dtype':'float32'};loads.append(('model',path));return Engine()
    monkeypatch.setitem(sys.modules,'torch',SimpleNamespace(float32='float32',set_num_threads=lambda n:None,inference_mode=nullcontext))
    monkeypatch.setitem(sys.modules,'transformers',SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=load_tokenizer),AutoModelForCausalLM=SimpleNamespace(from_pretrained=load_model)))
    model=tmp_path/'models/qwen2.5-1.5b-instruct/model.safetensors'
    model.parent.mkdir(parents=True);model.write_bytes(b'synthetic installed model')
    roots=[]
    def factory(root,user_id,context):
        roots.append(root);child=FastAPI()
        child.state.deployment=DeploymentLayout('server',root,tmp_path)
        child.state.device_registry=context.registry
        install_local_model_routes(child,runtime_root=root,generation_allowed=lambda:True)
        return child
    app=create_server_application(DeploymentLayout('server',tmp_path/'users/local-user',tmp_path),child_factory=factory)
    registry,users=app.state.device_registry,app.state.server_users
    pair=registry.exchange(registry.issue_pairing(user_id='local-user',actor='install')['code'],name='admin')
    admin=registry.authenticate(pair['key']);user=users.create(admin,name='second')
    paired=registry.exchange(registry.issue_pairing(user_id=user['user_id'],actor=admin.device_id)['code'],name='second device')
    with TestClient(app) as client:
        def request(key,text):
            return client.post('/local-model/v1/chat/completions',headers={'Authorization':'Bearer '+key},json={
                'model':'qwen2.5-1.5b-instruct','messages':[{'role':'user','content':text}],'max_tokens':4})
        results=[]
        first=Thread(target=lambda:results.append(request(pair['key'],'alpha')))
        second=Thread(target=lambda:results.append(request(paired['key'],'beta')))
        first.start()
        try:
            if not entered.wait(2):
                first.join(2)
                assert results and results[0].status_code==200
            second.start()
            release.set();first.join(3);second.join(3)
            assert not first.is_alive() and not second.is_alive()
            assert all(result.status_code==200 for result in results)
            assert {result.json()['choices'][0]['message']['content'] for result in results}=={'alpha','beta'}
            assert live[1]==1 and inputs==['alpha','beta']
            assert loads==[('tokenizer',model.parent),('model',model.parent)]
            assert len(roots)==2 and roots[0]!=roots[1]
            assert all(not (root/'data/models').exists() for root in roots)
        finally:
            release.set();first.join(3)
            if second.ident is not None:second.join(3)
