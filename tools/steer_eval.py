import argparse
from collections import Counter
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from backend.memory_app.v2.policies import get, parse_overrides


def evaluate(path, *, policy_version='@1'):
    policy=get('steer',version=policy_version)
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    cases=data.get('cases')
    labels={'correction','future','addition','other'}
    if data.get('synthetic') is not True or not isinstance(cases,list) or len(cases)<40:
        raise ValueError('expected_explicit_synthetic_steer_cases')
    counts=Counter(row['label'] for row in cases)
    if set(counts)!=labels or min(counts.values())<8 or sum(row.get('ambiguous') is True for row in cases)<8:
        raise ValueError('insufficient_steer_category_coverage')
    ids=[row['id'] for row in cases]
    if len(ids)!=len(set(ids)):
        raise ValueError('duplicate_steer_case')
    observed=[(row['label'],policy(row['text'])['category']) for row in cases]
    metrics={}
    for label in sorted(labels):
        correct=sum(expected==predicted==label for expected,predicted in observed)
        predicted=sum(actual==label for _,actual in observed)
        metrics[label]={'precision':correct/predicted if predicted else None,'recall':correct/counts[label],
                        'correct':correct,'expected':counts[label],'predicted':predicted}
    return {'cases':len(cases),'synthetic':True,'policy':'steer'+policy_version,'accuracy':sum(left==right for left,right in observed)/len(observed),
            'categories':metrics,'failures':[row['id'] for row,(left,right) in zip(cases,observed) if left!=right]}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--cases',type=Path,default=Path(__file__).resolve().parents[1]/'tests/fixtures/steer/cases.json')
    parser.add_argument('--policy',default='steer=@1',metavar='steer=VERSION')
    args=parser.parse_args()
    selections=parse_overrides([args.policy])
    if set(selections)!={'steer'}:
        raise ValueError('expected_steer_policy_selection')
    result=evaluate(args.cases,policy_version=selections['steer'])
    print(json.dumps(result,ensure_ascii=False))
    return 0 if all(result['categories'][label]['precision'] is not None and result['categories'][label]['precision']>=.9
                    for label in ('correction','future')) else 1


if __name__=='__main__':
    raise SystemExit(main())
