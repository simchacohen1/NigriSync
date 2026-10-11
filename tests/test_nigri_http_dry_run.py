"""Tests for offline Nigri mark creation and score/PDF serialization.

No real students, credentials, browser, network, or Nigri calls.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
import nigri_http_dry_run as sim


class OfflineSimulationTests(unittest.TestCase):
    def test_nine_fictional_students_scores_pdfs(self):
        result = sim.run_offline_simulation()
        self.assertEqual(result['status'], 'simulated_ok')
        self.assertEqual(result['simulated_students'], 9)
        self.assertEqual(result['simulated_scores'], 9)
        self.assertEqual(result['simulated_pdf_attachments'], 9)
        self.assertEqual(result['mock_request_bodies'], 2)
        self.assertEqual(result['requests_sent'], 0)
        self.assertEqual(result['automated_retries'], 0)
        self.assertTrue(result['offline_only'])
        self.assertTrue(result['fictional_data_only'])
        self.assertFalse(result['nigri_contacted'])
        self.assertFalse(result['nigri_writes'])
        self.assertFalse(result['real_save_enabled'])
        self.assertFalse(result['compatible_with_nigri_proven'])
        self.assertEqual(result['simulated_scores_percent'][0], 100)
        self.assertEqual(result['simulated_scores_percent'][1], 96)
        self.assertEqual(result['simulated_scores_percent'][6], 75)
        self.assertEqual(len(result['remaining_real_world_unknowns']), 3)
        self.assertNotIn('TEST DELETE ME', str(result))

    def test_no_network_function_is_called(self):
        with patch.object(requests.sessions.Session, 'send',
                          side_effect=AssertionError('NO NETWORK EVER')), \
             patch.object(requests.sessions.Session, 'request',
                          side_effect=AssertionError('NO NETWORK EVER')):
            self.assertEqual(sim.run_offline_simulation()['requests_sent'], 0)

    def test_only_invalid_domain_is_used_for_request_preparation(self):
        observed = []
        original = sim.Request
        def prepare(method, url, **kwargs):
            self.assertTrue(url.startswith('https://nigri-mock.invalid/'))
            observed.append((method, url))
            return original(method, url, **kwargs)
        with patch.object(sim, 'Request', side_effect=prepare):
            result = sim.run_offline_simulation()
        self.assertEqual(len(observed), 2)
        self.assertTrue(all(m == 'POST' for m, _ in observed))
        self.assertEqual(result['requests_sent'], 0)

    def test_reject_real_host_request_preparation(self):
        with self.assertRaises(ValueError):
            sim._prepare_only('POST', '/real/new-mark', data={})
        with self.assertRaises(ValueError):
            sim._prepare_only('GET', '/mock/new-mark', data={})

    def test_pdf_is_real_readable_pdf(self):
        from io import BytesIO
        from pypdf import PdfReader
        pdf = sim.synthetic_pdf()
        self.assertTrue(pdf.startswith(b'%PDF-'))
        self.assertEqual(len(PdfReader(BytesIO(pdf)).pages), 1)

    def test_incorrect_pdf_blocks_before_preparing_either_request(self):
        students = sim.fictional_students(2)
        students[1]['pdf_bytes'] = b'%PDF-1.4 bad pdf'
        with patch.object(sim, '_prepare_only',
                          side_effect=AssertionError('Must validate first')):
            with self.assertRaises(ValueError):
                sim.run_offline_simulation(students)

    def test_missing_pdf_blocks(self):
        students = sim.fictional_students(1)
        students[0]['pdf_bytes'] = None
        with self.assertRaises(ValueError):
            sim.run_offline_simulation(students)

    def test_nonfictional_name_blocks(self):
        students = sim.fictional_students(1)
        students[0]['name'] = 'Real Student'
        with self.assertRaises(ValueError):
            sim.run_offline_simulation(students)

    def test_duplicate_students_blocks(self):
        students = sim.fictional_students(2)
        students[1]['child_id'] = students[0]['child_id']
        with self.assertRaises(ValueError):
            sim.run_offline_simulation(students)
        students = sim.fictional_students(2)
        students[1]['name'] = students[0]['name']
        with self.assertRaises(ValueError):
            sim.run_offline_simulation(students)

    def test_points_out_of_range_blocks(self):
        for bad in (-1, 25, 'NaN', 'Infinity', True):
            with self.subTest(bad=bad):
                students = sim.fictional_students(1)
                students[0]['points'] = bad
                with self.assertRaises(ValueError):
                    sim.run_offline_simulation(students)

    def test_maximum_zero_blocks(self):
        students = sim.fictional_students(1)
        students[0]['maximum_points'] = 0
        with self.assertRaises(ValueError):
            sim.run_offline_simulation(students)

    def test_invalid_child_id_blocks(self):
        students = sim.fictional_students(1)
        students[0]['child_id'] = '21; DROP TABLE'
        with self.assertRaises(ValueError):
            sim.run_offline_simulation(students)

    def test_count_is_restricted(self):
        for value in (0, -1, 21, 2.5, '9', True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    sim.fictional_students(value)

    def test_zero_score_is_preserved_as_zero(self):
        students = sim.fictional_students(1)
        students[0]['points'] = 0
        result = sim.run_offline_simulation(students)
        self.assertEqual(result['simulated_scores_percent'], [0])
        self.assertEqual(result['simulated_pdf_attachments'], 1)

    def test_score_and_pdf_preparation_under_mock_transport(self):
        prepared = []
        original = sim._prepare_only
        def intercept(method, path, **kwargs):
            out = original(method, path, **kwargs)
            prepared.append(out)
            return out
        with patch.object(sim, '_prepare_only', side_effect=intercept):
            sim.run_offline_simulation(sim.fictional_students(3))
        self.assertEqual(len(prepared), 2)
        header, final = prepared
        self.assertEqual(header.method, 'POST')
        self.assertEqual(final.method, 'POST')
        self.assertIn('testGradeID', str(header.body))
        self.assertIn('testTopicID', str(header.body))
        self.assertIn('testType', str(header.body))
        self.assertIn('testName', str(header.body))
        self.assertIn('testDate', str(header.body))
        self.assertIn('testDesc', str(header.body))
        self.assertEqual(final.body.count(b'%PDF-'), 3)
        for i in range(3):
            self.assertIn(f'testChild_{900001+i}_mark'.encode(), final.body)
            self.assertIn(f'testChild_{900001+i}_File1'.encode(), final.body)
        self.assertIn(b'testCompleted', final.body)
        self.assertIn(b'Save!', final.body)
        self.assertNotIn(b'real nigri', final.body.lower())


if __name__ == '__main__':
    unittest.main()
