"""Validate planning links, task dependencies/coverage and the supplied JSON example.

The small schema checker below supports only the keywords used by this plan's
schema. It is not the production validator and does not test the application.
"""
import json
import re
import uuid
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]

def validate(value, schema):
    kind = schema.get('type')
    types = {'object': dict, 'array': list, 'string': str, 'integer': int}
    if kind:
        assert type(value) is types[kind], (kind, type(value).__name__)
    if 'const' in schema:
        assert value == schema['const']
    if 'enum' in schema:
        assert value in schema['enum']
    if kind == 'object':
        assert set(schema.get('required', [])) <= set(value)
        properties = schema.get('properties', {})
        if schema.get('additionalProperties') is False:
            assert set(value) <= set(properties)
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key])
    if kind == 'array':
        assert schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 10**9)
        if schema.get('uniqueItems'):
            assert len({json.dumps(item, sort_keys=True) for item in value}) == len(value)
        for item in value:
            validate(item, schema['items'])
    if kind == 'string':
        assert schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 10**9)
        if schema.get('format') == 'uuid':
            assert str(uuid.UUID(value)) == value.lower()
    if kind == 'integer':
        assert schema.get('minimum', -10**12) <= value <= schema.get('maximum', 10**12)

def main():
    tasks = json.loads((ROOT/'planning/tasks.json').read_text())['tasks']
    by_id = {t['id']: t for t in tasks}
    assert len(by_id) == len(tasks) == 72
    assert all(t['status'] in {'pending', 'in_progress', 'done', 'blocked'} for t in tasks), 'Unknown task status'
    seen, stack = set(), set()
    def visit(tid):
        assert tid in by_id, tid
        assert tid not in stack, 'Cycle: ' + tid
        if tid in seen:
            return
        stack.add(tid)
        for dep in by_id[tid]['dependencies']:
            if by_id[tid]['tier'] == 'D0':
                assert by_id[dep]['tier'] == 'D0', tid
            visit(dep)
        stack.remove(tid)
        seen.add(tid)
    for tid in by_id:
        visit(tid)
    covered = {r for t in tasks for r in t['requirements']}
    assert {'FR%02d' % i for i in range(1,31)} <= covered
    assert {'NF%02d' % i for i in range(1,13)} <= covered
    links = 0
    # Application dependencies contain unrelated Markdown. Only inspect the
    # original planning documentation; app verification is a separate suite.
    docs = sorted((ROOT/'docs').rglob('*.md'))
    for path in docs:
        content = path.read_text()
        assert content.count('```') % 2 == 0, path
        for tid in re.findall(r'\bT\d{3}\b', content):
            assert tid in by_id, (path, tid)
        for target in re.findall(r'(?<!!)\[[^\]\n]+\]\(([^)\n]+)\)', content):
            target = target.strip().strip('<>')
            if re.match(r'[a-z]+:', target):
                continue
            filepart, _, fragment = target.partition('#')
            dest = (path.parent/unquote(filepart)).resolve() if filepart else path
            assert dest.exists(), (path.name, target)
            if fragment and re.fullmatch(r't\d{3}', fragment):
                assert '<a id="'+fragment+'"></a>' in dest.read_text(), target
            links += 1
    schema = json.loads((ROOT/'contracts/step-draft.schema.json').read_text())
    sample = json.loads((ROOT/'contracts/step-draft.example.json').read_text())
    validate(sample, schema)
    transcript = json.loads((ROOT/'contracts/transcript.example.json').read_text())
    segments = {s['id']:s for s in transcript['segments']}
    for step in sample['steps']:
        evidence = [segments[sid] for sid in step['evidence_segment_ids']]
        assert 0 <= step['start_ms'] < step['end_ms'] <= transcript['duration_ms']
        assert step['start_ms'] == min(s['start_ms'] for s in evidence)
        assert step['end_ms'] == max(s['end_ms'] for s in evidence)
    totals = Counter()
    for t in tasks:
        totals[t['tier']] += t['hours']
    assert totals['D0'] == 308 and totals['D1'] == 99 and totals['D2'] == 7
    print(json.dumps({'result':'PASS', 'scope':'planning artifacts only',
                      'markdown_files':len(docs), 'local_links':links,
                      'tasks':len(tasks), 'requirements':len(covered),
                      'cycles':0, 'hours':dict(totals),
                      'schema_example':'valid for all used keywords; evidence/time checked'},
                     ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
