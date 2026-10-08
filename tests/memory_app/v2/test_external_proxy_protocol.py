"""合成协议请求的用户文本与无提权注入，不访问模型。"""
from copy import deepcopy
import importlib

import pytest


def owner():
    return importlib.import_module('backend.memory_app.v2.external_proxy_protocol')


@pytest.mark.parametrize('protocol,key,block_type',[
    ('chat_completions','messages','text'),('messages','messages','text'),
    ('responses','input','input_text')])
def test_last_user_multimodal_and_separate_context_preserve_all_fields(protocol,key,block_type):
    rows=[{'role':'system','content':'原指令'},
        {'role':'user','content':'旧问题'},
        {'role':'assistant','content':[{'type':'thinking','thinking':'合成思考','signature':'合成签名'}],
         'tool_calls':[{'id':'tool-one','function':{'arguments':'合成工具参数'}}]},
        {'role':'user','content':[{'type':'tool_result','content':'不选工具正文'},
          {'type':block_type,'text':'问题甲'}, {'type':'image','source':{'data':'合成图片'}},
          {'type':block_type,'text':'问题乙'}]},
        {'role':'tool','content':'最后工具正文','tool_call_id':'tool-one'}]
    payload={key:rows,'model':'synthetic-model','system':'顶层原指令','instructions':'原instructions',
        'tools':[{'name':'synthetic'}],'stream':True,'previous_response_id':'response-one',
        'conversation':{'id':'conversation-one'},'unknown':{'nested':[1,2]}}
    before=deepcopy(payload)
    assert owner().last_user_text(protocol,payload)=='问题甲\n问题乙'
    result=owner().insert_context(protocol,payload,'已标注的合成上下文')
    assert payload==before and result is not payload
    assert result[key]==rows[:3]+[{'role':'user','content':'已标注的合成上下文'}]+rows[3:]
    assert {k:v for k,v in result.items() if k!=key}=={k:v for k,v in before.items() if k!=key}
    result[key][0]['content']='只改副本'
    result['unknown']['nested'].append(3)
    assert payload==before


def test_responses_string_becomes_two_user_items_without_fabricated_history():
    payload={'input':'  保留原用户空白\n','instructions':'原指令','previous_response_id':'previous',
             'conversation':'existing-conversation','metadata':{'synthetic':'kept'}}
    assert owner().last_user_text('responses',payload)==payload['input']
    result=owner().insert_context('responses',payload,'标注上下文')
    assert result==dict(payload,input=[{'role':'user','content':'标注上下文'},
        {'role':'user','content':payload['input']}])
    assert payload['input']=='  保留原用户空白\n'


@pytest.mark.parametrize('protocol,key',[('chat_completions','messages'),('messages','messages'),('responses','input')])
@pytest.mark.parametrize('content',[[],None,42,'  ',[{'type':'image_url','image_url':{'url':'synthetic'}}],
    [{'type':'tool_result','content':'工具文本'}],[{'type':'text','text':42}],
    [{'type':'thinking','text':'不能当用户文本'}]])
def test_unreadable_last_user_never_reuses_old_question(protocol,key,content):
    payload={key:[{'role':'user','content':'旧问题'},{'role':'user','content':content}]}
    before=deepcopy(payload)
    assert owner().last_user_text(protocol,payload) is None
    with pytest.raises(ValueError,match='^external_proxy_body_unsupported$'):
        owner().insert_context(protocol,payload,'合成上下文')
    assert payload==before


@pytest.mark.parametrize('protocol,payload',[
    ('unknown',{'messages':[{'role':'user','content':'问题'}]}),
    ('messages',None),('chat_completions',[]),('messages',{'messages':'问题'}),
    ('responses',{'input':{'role':'user','content':'问题'}}),('responses',{'input':[]}),
    ('chat_completions',{'messages':[{'role':'assistant','content':'问题'}]}),
    ('responses',{'input':[{'type':'function_call_output','role':'user','content':'工具'}]}),
    ('messages',{'messages':[False,{'role':'user','content':'问题'}]})])
def test_unsupported_body_is_fixed_failure_without_mutation(protocol,payload):
    before=deepcopy(payload)
    assert owner().last_user_text(protocol,payload) is None
    with pytest.raises(ValueError,match='^external_proxy_body_unsupported$'):
        owner().insert_context(protocol,payload,'上下文')
    assert payload==before


@pytest.mark.parametrize('context',[None,{},42,'','  '])
def test_context_must_be_nonempty_plain_text(context):
    with pytest.raises(ValueError,match='^external_proxy_body_unsupported$'):
        owner().insert_context('messages',{'messages':[{'role':'user','content':'问题'}]},context)


def test_responses_text_type_is_input_only_and_message_type_is_preserved():
    payload={'input':[{'type':'message','role':'user','content':[
        {'type':'output_text','text':'不能选'}, {'type':'input_text','text':'真实输入'}]}]}
    assert owner().last_user_text('responses',payload)=='真实输入'
    result=owner().insert_context('responses',payload,'上下文')
    assert result['input'][1]==payload['input'][0]
