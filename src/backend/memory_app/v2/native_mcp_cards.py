from pathlib import Path
from urllib.parse import urlencode

from mcp import types
from mcp.server.lowlevel.helper_types import ReadResourceContents


URI = 'ui://incremind/recall-review-v1.html'
MIME = 'text/html;profile=mcp-app'


def install_cards(native):
    @native.server.list_resources()
    async def resources():
        native._invocation().lease.validate()
        return [types.Resource(uri=URI, name='记忆与待确认认识', mimeType=MIME,
            description='查看召回证据，进入主应用审核待确认认识')]

    @native.server.read_resource()
    async def resource(uri):
        invocation = native._invocation()
        invocation.lease.validate()
        if str(uri) != URI:
            raise ValueError('native_mcp_resource_invalid')
        folder = Path(__file__).resolve().parents[3] / 'frontend' / 'dist' / 'mcp-app'
        try:
            script = (folder / 'widget.js').read_text(encoding='utf-8').replace('</script', '<\\/script')
            css = (folder / 'widget.css').read_text(encoding='utf-8')
        except OSError:
            raise ValueError('native_mcp_card_build_required') from None
        text = '<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>' + css + '</style></head><body><div id="root"></div><script>' + script + '</script></body></html>'
        invocation.lease.validate()
        return [ReadResourceContents(content=text, mime_type=MIME, meta={'ui': {
            'csp': {'connectDomains': [], 'resourceDomains': []}}})]

    for name in ('recall', 'methods', 'read', 'propose_insight'):
        native.tools[name].meta = {'ui': {'resourceUri': URI}}


def card_metadata(native, invocation, name, data):
    if invocation.lease.restriction.client != 'chatgpt':
        return None
    meta = {'target_user_id': invocation.lease.restriction.target_user_id}
    if name == 'propose_insight':
        result = data['result']
        route = {'view': 'library', 'project_id': result['project_id']}
        if result.get('scope_kind') == 'shared':
            route.update(scope_kind='shared', layer='insight', insight_id=result['candidate_id'], filter='pending_memory')
        else:
            route.update({'native-mcp-review': result['candidate_id'],
                'native-mcp-target': invocation.lease.restriction.target_user_id})
        meta['review_url'] = native.origin + '/#' + urlencode(route)
    return {'native_mcp': meta}
