"""Contract and HTTP smoke tests; no external packages."""
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as app

REVIEW = {
    'sample_id': 'r1', 'case_type': 'INDIVIDUAL',
    'subjects': [{'subject_id': 1, 'identity_ids': ['alice']}],
    'candidate_identity_ids': ['alice', 'bob'],
    'query_image_id': 'q', 'target_image_id': 't',
    'query_image_url': '/PIPA/images/train/q.jpg',
    'target_image_url': '/PIPA/images/train/t.jpg',
    'query_boxes': [{'identity_id': i, 'x': .1, 'y': .2, 'width': .3, 'height': .4} for i in ('alice', 'bob')],
    'target_boxes': [{'identity_id': i, 'x': .2, 'y': .1, 'width': .3, 'height': .4} for i in ('alice', 'bob')],
    'final_desc': 'Identify Subject 1 as a woman wearing red',
    'final_change': 'then retrieve target images where Subject 1 is seated',
}
POSITIVE = {
    'sample_id': 'p1', 'case_type': 'INDIVIDUAL', 'subjects': REVIEW['subjects'],
    'query_image_id': 'q', 'query_image_url': '/PIPA/images/train/q.jpg',
    'seed_target_image_id': 't', 'final_desc': REVIEW['final_desc'],
    'final_change': REVIEW['final_change'],
    'candidates': [
        {'image_id': 't', 'image_url': '/PIPA/images/train/t.jpg', 'is_seed': True, 'subject_boxes': []},
        {'image_id': 'u', 'image_url': '/PIPA/images/train/u.jpg', 'is_seed': False, 'subject_boxes': []},
    ],
}


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        app.publish(self.root/'review_input.jsonl', [REVIEW])
        app.publish(self.root/'positive_set_input.jsonl', [POSITIVE])

    def test_review_round_trip(self):
        store = app.Store('review', self.root/'review_input.jsonl', self.root/'reviewed.jsonl')
        payload = {k: REVIEW[k] for k in ('sample_id', 'case_type', 'subjects', 'final_desc', 'final_change')}
        saved = store.save(payload)
        self.assertEqual(saved, app.load(self.root/'reviewed.jsonl')[0])
        self.assertEqual(store.tasks()['completed'], 1)
        self.assertEqual(app.Store('review', self.root/'review_input.jsonl', self.root/'reviewed.jsonl').task(0)['task']['final_desc'], REVIEW['final_desc'])

    def test_positive_round_trip_and_seed(self):
        store = app.Store('positives', self.root/'positive_set_input.jsonl', self.root/'positive_sets.jsonl')
        self.assertEqual(store.save({'sample_id':'p1', 'positive_image_ids':['u', 't']})['positive_image_ids'], ['t', 'u'])
        with self.assertRaises(ValueError):
            store.save({'sample_id':'p1', 'positive_image_ids':['u']})
        self.assertEqual(app.load(self.root/'positive_sets.jsonl')[0]['positive_image_ids'], ['t', 'u'])

    def test_input_validation(self):
        with self.assertRaises(ValueError):
            app.normalize_assignment('x', 'DUAL', [{'subject_id': 1, 'identity_ids': ['alice']}], ['alice', 'bob'])
        with self.assertRaises(ValueError):
            app.image_relative_path('../../etc/passwd')
        with self.assertRaises(ValueError):
            app.validate_review(REVIEW, {'sample_id':'r1', 'case_type':'INDIVIDUAL', 'subjects':REVIEW['subjects'], 'final_desc':'broken', 'final_change':REVIEW['final_change']})

    def test_http(self):
        stores = {
            'review': app.Store('review', self.root/'review_input.jsonl', self.root/'reviewed.jsonl'),
            'positives': app.Store('positives', self.root/'positive_set_input.jsonl', self.root/'positive_sets.jsonl'),
        }
        handler = type('TestHandler', (app.Handler,), {'stores': stores})
        image_dir = self.root/'images'/'train'
        image_dir.mkdir(parents=True)
        (image_dir/'q.jpg').write_bytes(b'fake-jpeg-content')
        with patch.object(app, 'IMAGES', self.root/'images'):
            srv = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            worker = threading.Thread(target=srv.serve_forever, daemon=True)
            worker.start()
            base = f'http://127.0.0.1:{srv.server_port}'
            try:
                for path in ('/', '/review/', '/positives/', '/static/app.js'):
                    with urllib.request.urlopen(base+path) as response:
                        self.assertEqual(response.status, 200)
                with urllib.request.urlopen(base+'/review/api/tasks') as response:
                    self.assertEqual(json.load(response)['total'], 1)
                with urllib.request.urlopen(base+'/positives/api/task?index=0') as response:
                    self.assertEqual(json.load(response)['task']['positive_image_ids'], ['t'])
                payload = {'sample_id':'p1','positive_image_ids':['t','u']}
                request=urllib.request.Request(base+'/positives/api/positive', data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'}, method='POST')
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(json.load(response)['positive']['positive_image_ids'], ['t','u'])
                with urllib.request.urlopen(base+'/review/api/image?path=train/q.jpg') as response:
                    self.assertEqual(response.read(), b'fake-jpeg-content')
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(base+'/review/api/image?path=../outside.jpg')
                self.assertEqual(err.exception.code, 400)
            finally:
                srv.shutdown()
                srv.server_close()
                worker.join(timeout=2)

if __name__ == '__main__':
    unittest.main()
