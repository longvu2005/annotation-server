"""Standalone RCR annotation server. Python standard library only."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
IMAGES = DATA / 'images'
REVIEW_IN = DATA / 'review_input.jsonl'
REVIEW_OUT = DATA / 'reviewed.jsonl'
POSITIVE_IN = DATA / 'positive_set_input.jsonl'
POSITIVE_OUT = DATA / 'positive_sets.jsonl'
CASES = ('INDIVIDUAL', 'GROUP', 'DUAL', 'RELATIONAL')
ONE_SUBJECT = {'INDIVIDUAL', 'GROUP'}
EDITABLE = ('INDIVIDUAL', 'DUAL', 'RELATIONAL')
SUBJECTS = {'INDIVIDUAL': [1], 'GROUP': [1], 'DUAL': [1, 2], 'RELATIONAL': [1, 2]}
SUBJECT_RE = re.compile(r'\bSubject\s+(\d+)\b')


def load(path):
    if not path.is_file():
        return []
    with path.open(encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


class InputIndex:
    """Small in-memory index of JSONL records, never retaining candidate lists.

    Byte offsets are stable while the input file remains unchanged. Replace the
    input files only after stopping the service, then restart to rebuild index.
    """

    def __init__(self, path, kind):
        self.path = path
        self.entries = []
        self.by_id = {}
        self.signature = None
        if not path.is_file():
            return
        self.signature = self._signature()
        with path.open('rb') as file:
            while True:
                offset = file.tell()
                line = file.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                row = json.loads(line)
                sample = row['sample_id']
                if sample in self.by_id:
                    raise ValueError(f'{path}: duplicate sample_id: {sample}')
                if kind == 'positives':
                    ids = [c['image_id'] for c in row['candidates']]
                    seed = row['seed_target_image_id']
                    marked = [c['image_id'] for c in row['candidates'] if c.get('is_seed')]
                    if not ids or len(ids) != len(set(ids)) or ids[0] != seed or marked != [seed]:
                        raise ValueError(f'{sample}: invalid positive catalog')
                self.by_id[sample] = len(self.entries)
                self.entries.append({'sample_id': sample, 'case_type': row['case_type'], 'offset': offset})
        self._check_unchanged()

    def _signature(self):
        stat = self.path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    def _check_unchanged(self):
        if self._signature() != self.signature:
            raise ValueError('input file changed; stop and restart server to rebuild index')

    def read(self, index):
        if not 0 <= index < len(self.entries):
            raise ValueError('task index out of range')
        self._check_unchanged()
        with self.path.open('rb') as file:
            file.seek(self.entries[index]['offset'])
            row = json.loads(file.readline())
        if row['sample_id'] != self.entries[index]['sample_id']:
            raise ValueError('input changed; stop and restart server to rebuild index')
        return row


def publish(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def image_relative_path(url):
    if not isinstance(url, str):
        raise ValueError('image URL must be a string')
    p = urlparse(url)
    value = parse_qs(p.query).get('d', [p.path])[0]
    value = unquote(value).replace('\\', '/')
    for marker in ('/PIPA/images/', '/images/'):
        if marker in value:
            value = value.split(marker, 1)[1]
            break
    value = value.lstrip('/')
    relative = Path(value)
    if not value or relative.is_absolute() or '..' in relative.parts:
        raise ValueError(f'unsafe image path: {url!r}')
    return relative


def image_url(value, section):
    from urllib.parse import quote
    return f'/{section}/api/image?path=' + quote(image_relative_path(value).as_posix(), safe='/')


def normalize_assignment(sample, case, subjects, candidates):
    if case not in SUBJECTS:
        raise ValueError(f'{sample}: invalid case_type')
    if not isinstance(subjects, list) or any(not isinstance(x, dict) for x in subjects):
        raise ValueError(f'{sample}: subjects must be a list')
    if len(candidates) != len(set(candidates)):
        raise ValueError(f'{sample}: duplicate candidate identity')
    ordered = sorted(subjects, key=lambda x: x.get('subject_id', -1))
    if [x.get('subject_id') for x in ordered] != SUBJECTS[case]:
        raise ValueError(f'{sample}: incorrect subjects for {case}')
    indices = {name: i for i, name in enumerate(candidates)}
    seen = set()
    result = []
    for s in ordered:
        ids = s.get('identity_ids')
        if not isinstance(ids, list) or not ids or any(not isinstance(x, str) or not x for x in ids):
            raise ValueError(f'{sample}: every subject needs identity IDs')
        if len(ids) != len(set(ids)) or any(x not in indices for x in ids) or seen.intersection(ids):
            raise ValueError(f'{sample}: duplicated, unknown, or overlapping identity IDs')
        seen.update(ids)
        result.append({'subject_id': s['subject_id'], 'identity_ids': sorted(ids, key=indices.get)})
    if case in ONE_SUBJECT:
        case = 'GROUP' if len(result[0]['identity_ids']) >= 2 else 'INDIVIDUAL'
    return case, result


def validate_text(case, desc, change):
    if not all(isinstance(v, str) and v.strip() and '\n' not in v and '\r' not in v for v in (desc, change)):
        raise ValueError('final_desc and final_change must be nonempty single lines')
    desc, change = desc.strip(), change.strip()
    if not desc.startswith('Identify ') or not change.startswith('then retrieve target images where '):
        raise ValueError('Text must start with canonical Identify / then retrieve prefixes')
    if desc.endswith(('.', ';', ':', '!', '?')) or change.endswith(('.', ';', ':', '!', '?')):
        raise ValueError('Do not end texts with punctuation')
    expected = SUBJECTS[case]
    if [int(x) for x in SUBJECT_RE.findall(desc)] != expected:
        raise ValueError('final_desc must mention subjects once in order')
    body = desc[len('Identify '):]
    parts = re.split(r'\s+and\s+(?=Subject\s+\d+\s+as\s+)', body)
    if len(parts) != len(expected) or any(not part.startswith(f'Subject {sid} as ') or not part[len(f'Subject {sid} as '):].strip() for part, sid in zip(parts, expected)):
        raise ValueError('Use Identify Subject N as <description> [and Subject N as ...]')
    if set(map(int, SUBJECT_RE.findall(change))) != set(expected):
        raise ValueError('final_change must mention all and only required subjects')
    return desc, change


def validate_review(source, payload):
    sample = source['sample_id']
    if payload.get('sample_id') != sample:
        raise ValueError('sample_id mismatch')
    case, subjects = normalize_assignment(sample, payload.get('case_type'), payload.get('subjects'), source['candidate_identity_ids'])
    desc, change = validate_text(case, payload.get('final_desc'), payload.get('final_change'))
    return {'sample_id': sample, 'case_type': case, 'subjects': subjects, 'final_desc': desc, 'final_change': change}


def validate_positive(source, payload):
    sample = source['sample_id']
    if payload.get('sample_id') != sample:
        raise ValueError('sample_id mismatch')
    candidates = [c['image_id'] for c in source['candidates']]
    seed = source['seed_target_image_id']
    if not candidates or len(candidates) != len(set(candidates)) or candidates[0] != seed:
        raise ValueError(f'{sample}: invalid candidate catalog')
    ids = payload.get('positive_image_ids')
    if not isinstance(ids, list) or any(not isinstance(x, str) or not x for x in ids):
        raise ValueError('positive_image_ids must be a list of strings')
    if len(ids) != len(set(ids)) or seed not in ids or set(ids) - set(candidates):
        raise ValueError('duplicate, invalid, or missing seed positive ID')
    return {'sample_id': sample, 'positive_image_ids': [x for x in candidates if x in ids]}


def review_boxes(row, side):
    ids = row['candidate_identity_ids']
    boxes = {}
    for box in row[f'{side}_boxes']:
        identity = box['identity_id']
        if identity in ids:
            if identity in boxes:
                raise ValueError(f'duplicate box for {identity}')
            boxes[identity] = box
    if set(ids) - set(boxes):
        raise ValueError('candidate identity missing box')
    return [{k: (100 * box[k] if k != 'identity_id' else box[k]) for k in ('identity_id', 'x', 'y', 'width', 'height')} for box in (boxes[i] for i in ids)]


def review_task(row, saved):
    if saved:
        case, subjects = normalize_assignment(row['sample_id'], saved['case_type'], saved['subjects'], row['candidate_identity_ids'])
    else:
        case, subjects = row['case_type'], row['subjects']
    return {'sample_id': row['sample_id'], 'case_type': case, 'subjects': subjects,
            'candidate_identity_ids': row['candidate_identity_ids'],
            'query': {'image_id': row['query_image_id'], 'image_url': image_url(row['query_image_url'], 'review'), 'boxes': review_boxes(row, 'query')},
            'target': {'image_id': row['target_image_id'], 'image_url': image_url(row['target_image_url'], 'review'), 'boxes': review_boxes(row, 'target')},
            'final_desc': (saved or row).get('final_desc') or '', 'final_change': (saved or row).get('final_change') or '',
            'original': {k: row.get(k) for k in ('case_type', 'subjects', 'final_desc', 'final_change')}}


def positive_task(row, saved):
    return {'sample_id': row['sample_id'], 'case_type': row['case_type'], 'subjects': row['subjects'],
            'query': {'image_id': row['query_image_id'], 'image_url': image_url(row['query_image_url'], 'positives'), 'boxes': []},
            'final_desc': row['final_desc'], 'final_change': row['final_change'],
            'candidates': [{'image_id': c['image_id'], 'image_url': image_url(c['image_url'], 'positives'),
                            'is_seed': c.get('is_seed') is True, 'boxes': c.get('subject_boxes', [])} for c in row['candidates']],
            'positive_image_ids': saved['positive_image_ids'] if saved else [row['seed_target_image_id']]}


class Store:
    def __init__(self, kind, input_path, output_path):
        self.kind, self.output = kind, output_path
        self.input = InputIndex(input_path, kind)
        self.rows = self.input.entries   # metadata only (no candidates / boxes)
        self.indices = self.input.by_id
        existing = load(output_path)
        if len({x['sample_id'] for x in existing}) != len(existing):
            raise ValueError(f'{output_path}: duplicate sample_id')
        if any(x['sample_id'] not in self.indices for x in existing):
            raise ValueError(f'{output_path}: unknown sample_id (input changed?)')
        validator = validate_review if kind == 'review' else validate_positive
        self.saved = {
            x['sample_id']: validator(self.input.read(self.indices[x['sample_id']]), x)
            for x in existing
        }
        self.lock = threading.Lock()

    def tasks(self):
        tasks = []
        for i, row in enumerate(self.rows):
            saved = self.saved.get(row['sample_id'])
            tasks.append({'index': i, 'sample_id': row['sample_id'],
                          'case_type': (saved or row)['case_type'] if self.kind == 'review' else row['case_type'],
                          'reviewed' if self.kind == 'review' else 'completed': saved is not None})
        completed = len(self.saved)
        result = {'total': len(tasks), 'completed': completed, 'pending': len(tasks) - completed,
                  'case_types': list(CASES), 'tasks': tasks}
        if self.kind == 'review':
            result.update({'editable_case_types': list(EDITABLE),
                           'one_subject_case_types': sorted(ONE_SUBJECT)})
        return result

    def task(self, index=None, sample_id=None):
        if sample_id is not None:
            if sample_id not in self.indices:
                raise ValueError('unknown sample_id')
            index = self.indices[sample_id]
        else:
            index = 0 if index is None else index
            if not 0 <= index < len(self.rows):
                raise ValueError('task index out of range')
        row = self.input.read(index)   # load one task only
        saved = self.saved.get(row['sample_id'])
        overview = self.tasks()
        result = {k: overview[k] for k in ('total', 'completed', 'pending')}
        result['index'] = index
        result['reviewed' if self.kind == 'review' else 'is_completed'] = saved is not None
        result['task'] = review_task(row, saved) if self.kind == 'review' else positive_task(row, saved)
        return result

    def save(self, payload):
        sample = payload.get('sample_id')
        index = self.indices.get(sample)
        if index is None:
            raise ValueError('unknown sample_id')
        source = self.input.read(index)  # validate against the original catalog
        row = validate_review(source, payload) if self.kind == 'review' else validate_positive(source, payload)
        with self.lock:
            updated = dict(self.saved)
            updated[row['sample_id']] = row
            publish(self.output, (
                updated[item['sample_id']] for item in self.rows
                if item['sample_id'] in updated
            ))
            self.saved = updated
        return row


class Handler(BaseHTTPRequestHandler):
    stores = None
    protocol_version = 'HTTP/1.1'

    def reply(self, payload, code=200):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.write_data(data, 'application/json; charset=utf-8', code)

    def write_data(self, data, mime, code=200):
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            if parsed.path in ('/', '/review', '/review/', '/positives', '/positives/'):
                filename = 'index.html' if parsed.path == '/' else ('review.html' if parsed.path.startswith('/review') else 'positives.html')
                self.write_data((ROOT / 'static' / filename).read_bytes(), 'text/html; charset=utf-8')
                return
            if parsed.path == '/static/app.css':
                self.write_data((ROOT / 'static' / 'app.css').read_bytes(), 'text/css; charset=utf-8')
                return
            if parsed.path == '/static/app.js':
                self.write_data((ROOT / 'static' / 'app.js').read_bytes(), 'text/javascript; charset=utf-8')
                return
            parts = parsed.path.strip('/').split('/')
            if len(parts) != 3 or parts[1] != 'api' or parts[0] not in self.stores:
                self.send_error(404)
                return
            section, _, action = parts
            store = self.stores[section]
            qs = parse_qs(parsed.query)
            if action == 'tasks':
                self.reply(store.tasks())
            elif action == 'task':
                sid = qs.get('id', [None])[0]
                idx = None if sid else int(qs.get('index', ['0'])[0])
                self.reply(store.task(idx, sid))
            elif action == 'image':
                raw = qs.get('path', [''])[0]
                path = image_relative_path(raw)
                resolved = (IMAGES / path).resolve()
                if not resolved.is_relative_to(IMAGES.resolve()) or not resolved.is_file():
                    raise ValueError('image not found or outside image root')
                mime = mimetypes.guess_type(resolved.name)[0] or 'application/octet-stream'
                if not mime.startswith('image/'):
                    raise ValueError('not an image')
                self.write_data(resolved.read_bytes(), mime)
            else:
                self.send_error(404)
        except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
            self.reply({'error': str(exc)}, 400)

    def do_POST(self):
        try:
            parts = urlparse(self.path).path.strip('/').split('/')
            if len(parts) != 3 or parts[1] != 'api' or parts[0] not in self.stores or parts[2] != ('review' if parts[0] == 'review' else 'positive'):
                self.send_error(404)
                return
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > 1_000_000:
                raise ValueError('invalid payload size')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('payload must be an object')
            store = self.stores[parts[0]]
            saved = store.save(payload)
            key = 'review' if parts[0] == 'review' else 'positive'
            self.reply({key: saved, 'summary': store.tasks()})
        except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
            self.reply({'error': str(exc)}, 400)

    def log_message(self, fmt, *args):
        pass


def main():
    p = argparse.ArgumentParser(description='RCR standalone annotation UI')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8090)
    args = p.parse_args()
    DATA.mkdir(exist_ok=True)
    IMAGES.mkdir(exist_ok=True)
    stores = {'review': Store('review', REVIEW_IN, REVIEW_OUT), 'positives': Store('positives', POSITIVE_IN, POSITIVE_OUT)}
    cls = type('AnnotationHandler', (Handler,), {'stores': stores})
    with ThreadingHTTPServer((args.host, args.port), cls) as server:
        print(f'Annotation server: http://{args.host}:{args.port}/ (Review + Positives)', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
