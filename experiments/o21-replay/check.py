"""Reviewer prototype of the R-METHOD evidence check (not product code)."""
import json, re, sys

norm = lambda s: re.sub(r'\s+', ' ', s).strip()

def sections(p):
    fx = p[p.index('## Fixture'):p.index('## Diff')]
    diff = p[p.index('## Diff'):p.index('## Snapshot')]
    files = p[p.index('## Touched file contents'):]
    added = [l[1:] for l in diff.splitlines() if l.startswith('+') and not l.startswith('+++') and l[1:].strip()]
    unchanged = '\n'.join(l for l in files.split('\n') if not l.startswith('<<CHANGED>>'))
    return fx, unchanged, added

def literal_ok(a, quote):
    q = quote.lower()
    t = a.get('type')
    if t == 'status':
        return str(a.get('equals')) in quote
    if t == 'db':
        return str(a.get('count')) in quote
    if t == 'body' and 'json_path' in a:
        v = a['json_path'].get('equals')
    else:
        v = a.get('equals', a.get('contains'))
        if 'matches' in a:
            return re.search(a['matches'], quote) is not None
    if isinstance(v, bool):
        w = 'true' if v else 'false'; o = 'false' if v else 'true'
        if re.search(r'\b' + o + r'\b', q):
            return 'evidence_ambiguous'
        return re.search(r'\b' + w + r'\b', q) is not None
    if v is None:
        return True
    return str(v).lower() in q

def check(prompt_path, reply_path):
    p = open(prompt_path).read(); fx, unchanged, added = sections(p)
    blocks = re.findall(r'```json\s*(.*?)```', open(reply_path).read(), re.S)
    res = []
    for i, b in enumerate(blocks):
        try:
            pr = json.loads(b)
        except Exception as e:
            res.append((i, '-', 'invalid_json')); continue
        for st in pr.get('setup', []):
            if 'capture' in st and not st['capture']:
                res.append((i, 'setup', 'empty_capture'))
        for a in pr.get('assert', []):
            ev = a.get('evidence')
            tag = a.get('type')
            if not ev or not ev.get('quote'):
                res.append((i, tag, 'evidence_missing')); continue
            src, q = ev.get('source'), ev['quote']
            text = fx if src == 'fixture' else unchanged
            if any(norm(q) in norm(x) or norm(x) in norm(q) for x in added) and norm(q) not in norm(text):
                res.append((i, tag, 'evidence_self_justifying')); continue
            if norm(q) not in norm(text):
                res.append((i, tag, 'evidence_not_found')); continue
            lo = literal_ok(a, q)
            if lo == 'evidence_ambiguous':
                res.append((i, tag, 'evidence_ambiguous')); continue
            if not lo:
                res.append((i, tag, 'evidence_direction_mismatch')); continue
            res.append((i, tag, 'ok'))
    return blocks, res

if __name__ == '__main__':
    blocks, res = check(sys.argv[1], sys.argv[2])
    bad = [r for r in res if r[2] != 'ok']
    print(f'{sys.argv[2].split("/")[-1]}: {len(blocks)} probes, {len(res)} checks, {len(bad)} rejected')
    for r in bad: print('   ', r)


def correction_message(res):
    """The one follow-up the harness sends; lists each rejected assertion."""
    lines = [f"- probe {i + 1}, {tag} assertion: {reason}" for i, tag, reason in res if reason != 'ok']
    if not lines:
        return None
    return ("Some assertions were rejected by the evidence check:\n" + "\n".join(lines) +
            "\nReasons: evidence_missing = no evidence object; evidence_not_found = the quote is not in the "
            "named source as shown; evidence_self_justifying = the quote comes from a changed line; "
            "evidence_direction_mismatch = the quote does not contain the expected value; evidence_ambiguous = "
            "the quote contains both true and false; invalid_json/empty_capture = format error.\n"
            "Return the complete corrected set of probes, in the same format. This is your only correction.")


def surviving(prompt_path, reply_text):
    """Probes that pass every check (a probe with any rejected assertion is dropped)."""
    import tempfile, os
    fd, tmp = tempfile.mkstemp(); os.write(fd, reply_text.encode()); os.close(fd)
    blocks, res = check(prompt_path, tmp); os.unlink(tmp)
    bad = {i for i, _, r in res if r != 'ok'}
    out = []
    for i, b in enumerate(blocks):
        if i in bad:
            continue
        try:
            out.append(json.loads(b))
        except Exception:
            pass
    return out, res
