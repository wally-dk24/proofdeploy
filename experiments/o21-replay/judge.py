"""Pre-registered offline judge for the O21 replay (golden bug units only; known answer).
A slot is CAUGHT when at least one surviving probe, authenticated with ${AUTH_TOKEN} (seeduser),
GETs /notes/1 asserting done == false or /notes/2 asserting done == true, and NO surviving probe
asserts the bug's values (/notes/1 done == true or /notes/2 done == false). Otherwise MISSED."""
import json, sys
import os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check import surviving

def authed_as_seed(p):
    """v2 (D240): the request is made as seeduser, either with the provided ${AUTH_TOKEN}
    or with a token captured from a setup POST /login (or /register) as seeduser/seedpass."""
    auth = json.dumps(p.get('act', {}).get('headers', {}))
    if 'Bearer ${AUTH_TOKEN}' in auth:
        return True
    for s in p.get('setup', []):
        sa = s.get('act', {})
        body = sa.get('body') or {}
        if (sa.get('method') == 'POST' and sa.get('path') in ('/login', '/register')
                and isinstance(body, dict) and body.get('username') == 'seeduser'
                and body.get('password') == 'seedpass'):
            for name in (s.get('capture') or {}):
                if 'Bearer ${' + name + '}' in auth:
                    return True
    return False


def judge(prompt_path, final_reply):
    probes, res = surviving(prompt_path, final_reply)
    right = wrong = 0
    for p in probes:
        a = p.get('act', {})
        if a.get('method') != 'GET' or not authed_as_seed(p):
            continue
        for x in p.get('assert', []):
            jp = x.get('json_path') or {}
            if jp.get('path') != 'done':
                continue
            v = jp.get('equals')
            if (a['path'], v) in (('/notes/1', False), ('/notes/2', True)):
                right += 1
            if (a['path'], v) in (('/notes/1', True), ('/notes/2', False)):
                wrong += 1
    verdict = 'CAUGHT' if right and not wrong else 'MISSED'
    return verdict, {'surviving_probes': len(probes), 'right': right, 'wrong': wrong,
                     'rejections': [r for r in res if r[2] != 'ok']}

if __name__ == '__main__':
    v, d = judge(sys.argv[1], open(sys.argv[2]).read())
    print(v, json.dumps(d))
