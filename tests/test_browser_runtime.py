import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_runtime import browser_slot, run_browser


def harmless_worker():
    return {'pid': os.getpid()}


def slow_worker():
    time.sleep(30)


class RuntimeTests(unittest.TestCase):
    def test_overlapping_slots_rejected(self):
        with browser_slot():
            with self.assertRaisesRegex(ValueError, 'Another browser'):
                with browser_slot(): pass
        with browser_slot(): pass

    def test_child_cleanup_and_slot_release(self):
        with patch('browser_runtime.memory_usage', return_value=(0, 0)):
            result = run_browser(harmless_worker)
        with self.assertRaises(ProcessLookupError): os.kill(result['pid'], 0)
        with browser_slot(): pass

    def test_memory_guard_stops_child_and_service_survives(self):
        with patch('browser_runtime.memory_usage', return_value=(450 * 1024**2, 512 * 1024**2)):
            with self.assertRaisesRegex(ValueError, 'stopped safely'):
                run_browser(slow_worker)
        with browser_slot(): pass

    def test_archive_uses_disk_and_streamed_pdf(self):
        from classtime_review import import_review_archive, store_review, JOBS
        import shutil
        import io, json, zipfile
        from reportlab.pdfgen import canvas
        report = io.BytesIO(); page = canvas.Canvas(report)
        page.drawString(30, 750, 'Student Report: Arik Traxler Class: B3 ET Session: ZZKUYA 5 / 24 points')
        page.save()
        archive = io.BytesIO()
        metadata = dict(session_code='ZZKUYA', class_section='B3 ET', students=[dict(classtime_name='Arik Traxler', points=5, maximum_points=24)])
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('review.json', json.dumps(metadata))
            output.writestr('../../untrusted-name.pdf', report.getvalue())
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            archive_path = Path(folder) / 'review.zip'
            archive_path.write_bytes(archive.getvalue())
            with patch('browser_runtime.memory_usage', return_value=(0, 0)):
                result, pdfs = run_browser(import_review_archive, archive_path)
        self.assertEqual(len(pdfs), 1)
        self.assertTrue(all(isinstance(p, str) and Path(p).is_file() for p in pdfs.values()))
        import app
        key = store_review('test-owner', result, pdfs)
        with patch('classtime_auth.review_owner', return_value='test-owner'):
            response = app.app.test_client().get('/classtime/reviews/' + key + '/pdfs/' + next(iter(pdfs)))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.data.startswith(b'%PDF-'))
            response.close()
        JOBS.clear()
        for folder in {str(Path(p).parent) for p in pdfs.values()}: shutil.rmtree(folder)

if __name__ == '__main__': unittest.main()
