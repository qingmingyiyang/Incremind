from dataclasses import dataclass

from .continuation import _topic, SCORE_THRESHOLD
from .insight_time import instant


@dataclass(frozen=True)
class StalePolicy:
    fresh_threshold: float = SCORE_THRESHOLD

    def __call__(self, frozen, current, *, reference):
        return self.changed(frozen,current,reference=reference)

    def changed(self, frozen, current, *, reference):
        if current is None or current.get('available') is not True:
            return {'verified':False,'reasons':[]}
        if any(frozen.get(key)!=current.get(key) for key in ('kind','id','project_id')):
            raise ValueError('outcome_source_identity_changed')
        old,new=frozen.get('revision'),current.get('revision')
        incarnation_changed=frozen.get('incarnation')!=current.get('incarnation')
        if type(old) is not int or type(new) is not int or min(old,new)<1 or new<old and not incarnation_changed:
            raise ValueError('invalid_outcome_source_revision')
        if 'content' not in frozen or 'content' not in current:
            return {'verified':False,'reasons':[]}
        reasons=[]
        if incarnation_changed:
            reasons.append('source_replaced')
        if frozen['content']!=current['content']:
            reasons.append('edited')
        if current.get('replaced_by'):
            reasons.append('superseded')
        if current.get('manual_forgotten') is True:
            reasons.append('forgotten')
        expires=instant(current.get('expires_at'))
        clock=instant(reference)
        if clock is None:
            raise ValueError('invalid_outcome_reference')
        if expires is not None and expires<=clock:
            reasons.append('expired')
        return {'verified':True,'reasons':reasons,'from_revision':old,'to_revision':new}

    def fresh(self, task, candidates, *, score_text):
        if not callable(score_text):
            raise ValueError('outcome_local_matcher_required')
        born=instant(task['created_at'])
        if born is None:
            raise ValueError('invalid_outcome_birth_time')
        selected=[]
        for candidate in candidates:
            created=instant(candidate.get('created_at'))
            if (candidate.get('available') is not True or created is None or created<=born
                    or candidate.get('project_id')!=task['project_id']
                    or candidate.get('scene')!=task.get('scene')
                    or candidate['id'] in task.get('excluded_ids',())):
                continue
            score=score_text(_topic(task['text']),candidate['text'])
            if score>=self.fresh_threshold:
                selected.append({'document_id':candidate['id'],'title':candidate['title'],
                    'revision':candidate['revision'],'score':score})
        return sorted(selected,key=lambda row:(-row['score'],row['document_id']))


v1=StalePolicy()
