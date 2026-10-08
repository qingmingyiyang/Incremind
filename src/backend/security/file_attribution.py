"""Fixed, non-secret facts stored with each server configuration replacement."""
import json
from pathlib import Path
import re
from backend.shared.server_resources import RESOURCE_POOL
from .user_context import USER_ACCESS


_FIELDS = {'by','actor_user_id','target_user_id','device_id','namespace_id','owner_id','revision'}
_OWNERS = {'providers','settings','environment','team-profile','secrets'}
_SEGMENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z')
_REVISION_COMMENT = '# chriptmas-server-owner-revision:'
_ATTRIBUTION_COMMENT = '# chriptmas-server-attribution:'


class FileMutationAttribution:
    def __init__(self,root,owner_id):
        self.root,self.owner_id=Path(root).resolve(),owner_id

    def _valid(self,value,revision):
        return (isinstance(value,dict) and set(value)==_FIELDS and value['by']=='admin'
            and value['namespace_id']=='configuration' and value['owner_id']==self.owner_id
            and type(value['revision']) is int and value['revision']==revision
            and value['target_user_id']==self.root.name
            and all(isinstance(value[key],str) and _SEGMENT.fullmatch(value[key])
                for key in ('actor_user_id','target_user_id','device_id')))

    def _next(self,previous):
        revision=previous.get('server_owner_revision',0)
        if type(revision) is not int or revision<0:
            raise ValueError('server_attribution_invalid')
        prior=previous.get('server_admin_attribution')
        if prior is not None and not self._valid(prior,revision):
            raise ValueError('server_attribution_invalid')
        access=USER_ACCESS.get()
        if access is not None and access.target_user_id!=self.root.name:
            raise ValueError('admin_target_mismatch')
        revision+=1
        value=None
        if access is not None and access.by=='admin':
            value={'by':'admin','actor_user_id':access.caller.user_id,'target_user_id':access.target_user_id,
                'device_id':access.caller.device_id,'namespace_id':'configuration','owner_id':self.owner_id,'revision':revision}
            if not self._valid(value,revision):
                raise ValueError('server_attribution_invalid')
        return revision,value

    def json(self,value,previous):
        revision,binding=self._next(previous)
        result={key:item for key,item in value.items() if key not in {'server_owner_revision','server_admin_attribution'}}
        result['server_owner_revision']=revision
        if binding is not None:result['server_admin_attribution']=binding
        return result

    def text(self,value,previous):
        old={}
        try:
            for line in previous.splitlines():
                if line.startswith(_REVISION_COMMENT):old['server_owner_revision']=json.loads(line[len(_REVISION_COMMENT):])
                if line.startswith(_ATTRIBUTION_COMMENT):old['server_admin_attribution']=json.loads(line[len(_ATTRIBUTION_COMMENT):])
        except ValueError:
            raise ValueError('server_attribution_invalid') from None
        revision,binding=self._next(old)
        lines=[line for line in value.splitlines() if not line.startswith((_REVISION_COMMENT,_ATTRIBUTION_COMMENT))]
        lines.append(_REVISION_COMMENT+str(revision))
        if binding is not None:lines.append(_ATTRIBUTION_COMMENT+json.dumps(binding,ensure_ascii=True,separators=(',',':')))
        return '\n'.join(lines)+'\n'


def file_attribution(root,owner_id):
    resources=RESOURCE_POOL.get()
    if resources is None:return None
    root=Path(root).resolve()
    if owner_id not in _OWNERS or root.parent!=(resources.server_root/'users').resolve():
        raise ValueError('admin_target_mismatch')
    return FileMutationAttribution(root,owner_id)
