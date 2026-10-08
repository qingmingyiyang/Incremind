"""Body-free source authority at read-provider and model admission boundaries."""
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from urllib.parse import unquote, urlsplit
from backend.recognition import WorkScope, RecognitionConflict
from .original_sources import source_store, original, ORIGINAL_TYPES
from .source_egress import SourceEgressService, _frozen_packet_authority, _refs
from .source_graph import LIMIT, identifier, revision, validate_graph
from .privacy_state import is_private_project

from .source_evidence_refs import READS, _source_refs, evidence_refs, roots_for
from .research_reads import COLLECTION, scope_for, read_dependencies, validate_reads
from backend.shared.llm.litellm_gateway import _is_consumer_cancellation




class ReadProvider:
    def __init__(self, inner, capability, records):
        self.inner,self.capability,self.records=inner,capability,records
    def invoke(self, request):
        scope=scope_for(request)
        # Capture versions before the provider reads its body; dynamic selection
        # can only bind material which was already present at this boundary.
        baseline={}
        from .original_sources import all_originals
        originals=all_originals(self.records)
        with self.records.begin() as reader:
            for kind,identity,body in originals:
                own=WorkScope('local-user',body.get('project_id','default'))
                row=original(reader,own,kind,identity)
                baseline[(kind,identity)]=(row.revision,row.payload.get('_original_incarnation'))
        documents={row.object_id:row.revision for row in self.records.list('documents')
                   if row.payload.get('project_id')==scope.project_id}
        store=source_store(self.records)
        configurations=[]
        if self.capability in ('source.evidence.read','project_skill.evidence.read'):
            for collection in ('memory_persona','project_skills'):
                for body in store.list(collection):
                    if collection=='memory_persona' and body.get('scope')!='global':
                        continue
                    if collection=='project_skills' and body.get('project_id')!=scope.project_id:
                        continue
                    identity=body.get('id')
                    if isinstance(identity,str):
                        configurations.append((collection,identity,store.revision(collection,identity),body))
        output=self.inner.invoke(request)
        for collection,identity,revision,body in configurations:
            if store.revision(collection,identity)!=revision or store.read(collection,identity)!=body:
                raise RecognitionConflict('research configuration changed during read')
        refs=evidence_refs(self.capability,output)
        result=output.get('result')
        if self.capability=='source.evidence.read' and result.get('project_skill_ref'):
            from core.aggregate_repository_factory import AggregateRepositoryFactory
            skill=AggregateRepositoryFactory(runtime_root=store.root.parent,namespace_id='default',json_store=store).project_skill_repository().load(scope.project_id)
            if skill is None or skill.get('revision')!=result['project_skill_ref'].get('revision'):
                raise RecognitionConflict('research project style changed')
            refs.extend(_source_refs(skill.get('source_refs',[])))
            for rule in skill.get('output_rules',[]):
                if not isinstance(rule,Mapping): raise RecognitionConflict('research style rule is invalid')
                refs.extend(_source_refs(rule.get('source_refs',[])))
        document_versions=[]
        for ref in refs:
            url=urlsplit(ref)
            identity=url.netloc if url.scheme=='document' else None
            if url.scheme=='crp' and url.netloc=='default' and url.path.startswith('/documents/'):
                identity=unquote(url.path.removeprefix('/documents/')).removesuffix('.json')
            if identity is not None:
                row=self.records.read('documents',identity)
                if row is None or documents.get(identity)!=row.revision:
                    raise RecognitionConflict('research document changed during read')
                document_versions.append({'id':identity,'revision':row.revision})
        result=output.get('result')
        if self.capability=='source.evidence.read':
            bound=result.get('document_baseline')
            if bound:
                from core.document_engine import SQLiteDocumentRepository
                doc=SQLiteDocumentRepository(self.records).get(bound['document_id'])
                if doc is None or bound.get('document_revision')!=doc['revision']:
                    raise RecognitionConflict('research document baseline is stale')
            native=source_store(self.records).revision('sources',result['source_id'])
            if result.get('source_revision')!=native:
                raise RecognitionConflict('research source read revision is stale')
        extra=[]
        if self.capability=='source.evidence.read' and result.get('style_prefix'):
            store=source_store(self.records)
            from core.product_core.persona import ObjectStorePersonaRepository
            persona=ObjectStorePersonaRepository(object_store=store).get('global')
            persona_refs=[]
            if persona:
                for ref in persona.get('evidence_refs',[]):
                    persona_refs.extend(_source_refs(ref.get('source_refs',[])))
            for ref in persona_refs:
                # Global style can reference other projects. Bind each typed
                # original to its real owner, retaining that owner's privacy.
                url=urlsplit(ref)
                identity=url.netloc if url.scheme=='source' else None
                if identity is None:
                    raise RecognitionConflict('style original evidence is unresolved')
                body=store.read('sources',identity)
                if body is None: raise RecognitionConflict('style original is unavailable')
                own=WorkScope('local-user',body.get('project_id','default'))
                with self.records.begin() as reader:
                    selected=roots_for(reader,own,[ref])
                for root in selected:
                    with self.records.begin() as reader:
                        row=original(reader,own,root['type'],root['id'])
                        if baseline.get((root['type'],root['id']))!=(row.revision,row.payload.get('_original_incarnation')):
                            raise RecognitionConflict('style original changed during read')
                snap=SourceEgressService(self.records).snapshot(own,selected)
                if request.get('privacy',{}).get('allow_remote'):
                    SourceEgressService(self.records).require(snap,'generation')
                extra.append(snap)
        with self.records.begin() as reader:
            roots=roots_for(reader,scope,refs)
            for root in roots:
                if root['type'] in ORIGINAL_TYPES:
                    row=original(reader,scope,root['type'],root['id'])
                    if baseline.get((root['type'],root['id']))!=(row.revision,row.payload.get('_original_incarnation')):
                        raise RecognitionConflict('research original changed during read')
        authority=SourceEgressService(self.records)
        snapshot=authority.snapshot(scope,roots) if roots else None
        if snapshot is not None:
            authority.validate_snapshot(scope,snapshot)
            if request.get('privacy',{}).get('allow_remote'):
                authority.require(snapshot,'generation')
        elif is_private_project(self.records,scope.project_id) and request.get('privacy',{}).get('allow_remote'):
            raise RecognitionConflict('private_project_remote_blocked')
        identity=request.get('tool_call_id')
        if not isinstance(identity,str) or not identity:
            raise RecognitionConflict('research tool identity is unavailable')
        proof={'turn_id':request['turn_id'],'tool_call_id':identity,'project_id':scope.project_id,
               'capability_id':self.capability,'source_egress':snapshot,
               'documents':sorted(document_versions,key=lambda row:row['id']), 'style_sources':extra,
               'configurations':[{'collection':collection,'id':identity,'revision':revision}
                   for collection,identity,revision,_ in configurations]}
        with self.records.begin() as reader:
            existing=reader.read(COLLECTION,identity)
            if existing is not None and existing.payload!=proof:
                raise RecognitionConflict('research read authority cannot be rebound')
            if existing is None:
                reader.put(COLLECTION,identity,proof,expected_revision=0)
                reader.commit()
        return output


class ReadRegistry:
    def __init__(self, inner, records): self.inner,self.records=inner,records
    def register(self, definition, provider):
        if definition.capability_id in READS:
            provider=ReadProvider(provider,definition.capability_id,self.records)
        return self.inner.register(definition,provider)
    register_core=register




class _WireLocks:
    """Collect lock identities only; source owners still decide authorization."""
    def __init__(self, records, user):
        self.records, self.user = records, user
        self.identities, self.budget, self.proofs = set(), set(), set()
        self.tables = 0

    def configuration(self, proof):
        configurations = proof.get('configurations', [])
        if not isinstance(configurations, list) or len(configurations) > LIMIT:
            raise RecognitionConflict('research configuration locks are invalid')
        for config in configurations:
            if (not isinstance(config, Mapping) or set(config) != {'collection', 'id', 'revision'}
                    or config['collection'] not in ('memory_persona', 'project_skills')):
                raise RecognitionConflict('research configuration locks are invalid')
            identifier(config['id']); revision(config['revision'])
            self.add(config['collection'], config['id'])

    def add(self, collection, identity):
        self.identities.add((collection, identity))
        if len(self.identities) > LIMIT:
            raise RecognitionConflict('research source locks are too large')

    def snapshot(self, snapshot, *, depth=0):
        self.tables += 1
        if (self.tables > LIMIT or depth >= LIMIT or not isinstance(snapshot, Mapping)
                or not isinstance(snapshot.get('scope'), Mapping)):
            raise RecognitionConflict('research source locks are invalid')
        raw_scope = snapshot['scope']
        if set(raw_scope) != {'user_id', 'project_id'} or raw_scope['user_id'] != self.user:
            raise RecognitionConflict('research source lock scope is invalid')
        own = WorkScope(**raw_scope)
        parsed = _frozen_packet_authority(own, {'source_egress': snapshot},
            _refs(snapshot.get('roots')), _nodes=self.budget)
        for node in parsed['nodes']:
            if node['type'] == 'original_source':
                self.add('sources', node['id'])
            dependency = node.get('dependency_revisions', {})
            if 'source_snapshot' in dependency:
                self.snapshot(dependency['source_snapshot'], depth=depth + 1)
            if 'current_source_graph' in dependency:
                graph = validate_graph(dependency['current_source_graph'], self.user, budget=self.budget)
                for entry in graph['nodes']:
                    if entry['kind'] == 'material' and entry['type'] == 'original_source':
                        self.add('sources', entry['id'])
                    elif entry['kind'] == 'read_proof':
                        identity = entry['tool_call_id']
                        if identity in self.proofs:
                            continue
                        row = self.records.read(COLLECTION, identity)
                        if (row is None or row.object_id != identity or row.revision != 1
                                or not isinstance(row.payload, Mapping)
                                or row.payload.get('tool_call_id') != identity
                                or row.payload.get('turn_id') != entry['turn_id']
                                or row.payload.get('project_id') != entry['scope']['project_id']
                                or row.payload.get('capability_id') != entry['capability_id']):
                            raise RecognitionConflict('research source lock proof changed')
                        self.proofs.add(identity)
                        self.proof(row.payload, depth=depth + 1)
            styles = node.get('research_style_sources', [])
            if len(styles) > LIMIT:
                raise RecognitionConflict('research source locks are too large')
            for nested in styles:
                self.snapshot(nested, depth=depth + 1)

    def proof(self, proof, *, depth=0):
        self.configuration(proof)
        snapshots = [proof['source_egress']] if proof.get('source_egress') is not None else []
        styles = proof.get('style_sources', [])
        if not isinstance(styles, list) or len(styles) > LIMIT:
            raise RecognitionConflict('research source locks are invalid')
        for snapshot in [*snapshots, *styles]:
            self.snapshot(snapshot, depth=depth)


class _SourceWireAttempt:
    """只补来源发送接点，执行与回执仍委托原内核 handle。"""
    def __init__(self, inner, control):
        self.inner, self.control = inner, control

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def invoke_wire(self, handler):
        def qualified_handler():
            try:
                with self.control._qualified_sources():
                    pass
            except BaseException as error:
                # 此处已由原 executor claim；先写原终态，避免拒发留下 INFLIGHT。
                if _is_consumer_cancellation(error):
                    self.inner.consumer_cancelled()
                else:
                    self.inner.failed_transport(error_code='ai.source_egress_before_request_failed')
                raise
            # 最后复验在短事务内完成；发送期变化仍由调用方事后复验拒绝结果。
            return handler()

        return self.inner.invoke_wire(qualified_handler)


class ReadControl:
    def __init__(self, inner, records, turns, agents, request, *, validator=None):
        self.inner,self.records,self.turns,self.agents,self.request=inner,records,turns,agents,request
        self.validator = validator
    def with_validator(self, validator):
        return ReadControl(self.inner, self.records, self.turns, self.agents, self.request, validator=validator)
    def __getattr__(self,name): return getattr(self.inner,name)
    def validate(self):
        scope=scope_for(self.request)
        proofs=read_dependencies(self.records,self.turns,self.agents,self.request['turn_id'],scope.project_id)
        validate_reads(self.records,scope,proofs,authority=SourceEgressService(self.records),remote=self.request.get('privacy',{}).get('allow_remote') is True)
        if self.validator is not None:
            self.validator(self.records, self.request)
    def checkpoint(self):
        self.validate()
        return self.inner.checkpoint()
    @contextmanager
    def _qualified_sources(self):
        from .transaction_records import TransactionRecords
        scope=scope_for(self.request)
        with self.records.begin() as reader, ExitStack() as locks:
            records=TransactionRecords(reader)
            proofs=read_dependencies(records,self.turns,self.agents,self.request['turn_id'],scope.project_id)
            collector = _WireLocks(records, scope.user_id)
            snapshots = self.request.get('privacy', {}).get('source_snapshots', [])
            if not isinstance(snapshots, list) or len(snapshots) > LIMIT:
                raise RecognitionConflict('research request source locks are invalid')
            for snapshot in snapshots:
                collector.snapshot(snapshot)
            for proof in proofs:
                collector.proof(proof)
            for collection,identity in sorted(collector.identities):
                locks.enter_context(source_store(reader).locked(collection,identity))
            authority = SourceEgressService(records)
            remote = self.request.get('privacy', {}).get('allow_remote') is True
            for snapshot in snapshots:
                own = WorkScope(**snapshot['scope'])
                if own.project_id not in {scope.project_id, 'me'}:
                    raise RecognitionConflict('turn material scope conflicted')
                authority.validate_snapshot(own, snapshot)
                if remote:
                    authority.require(snapshot, 'generation')
            validate_reads(records,scope,proofs,authority=authority,remote=remote)
            if self.validator is not None:
                self.validator(records, self.request)
            yield

    def begin_model_wire_attempt(self,*args,**kwargs):
        with self._qualified_sources():
            handle = self.inner.begin_model_wire_attempt(*args,**kwargs)
        return _SourceWireAttempt(handle, self)


class ReadPlanner:
    def __init__(self, inner, records, turns, agents):
        self.inner,self.records,self.turns,self.agents=inner,records,turns,agents
    def __getattr__(self,name): return getattr(self.inner,name)
    def plan(self, request, events, capabilities, payloads, execution_control=None):
        if execution_control is None or request.get('scope',{}).get('project_id') is None:
            return self.inner.plan(request,events,capabilities,payloads,execution_control=execution_control)
        control=ReadControl(execution_control,self.records,self.turns,self.agents,request)
        control.validate()
        return self.inner.plan(request,events,capabilities,payloads,execution_control=control)
