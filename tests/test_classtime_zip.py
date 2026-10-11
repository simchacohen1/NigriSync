import io
import sys
import types
import unittest
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Made-up roster, so the tests need neither the real names nor the browser package.
if 'nigri_playwright' not in sys.modules:
    fake = types.ModuleType('nigri_playwright')
    fake.REWARDS_CHILD_IDS = {'Sample Alpha': 'c1', 'Sample Beta': 'c2'}
    fake._MARKS_CLASS_STUDENTS = {'B3 ET': ['Sample Alpha', 'Sample Beta'], 'B3 WT': []}
    sys.modules['nigri_playwright'] = fake
try:
    import fcntl  # Linux only; Windows test runs use a stand-in
except ImportError:
    sys.modules['fcntl'] = types.ModuleType('fcntl')
from reportlab.pdfgen import canvas
import classtime_zip as cz


def report(name, score, code='ABC123', title='B3 ET Test Quiz', klass='B3 ET', maximum='12'):
    out = io.BytesIO(); c = canvas.Canvas(out)
    c.drawString(40, 800, 'Date: 10/9/2026 Class: ' + klass)
    c.drawString(40, 780, 'Name: ' + name)
    c.drawString(40, 760, 'Grade:')
    c.drawString(40, 740, ' '.join(code) + ' ' + title + ' ' + score + ' / ' + maximum + ' points')
    c.drawString(40, 100, name + ' | Session: ' + code + '. ' + title + ' 2026-10-09')
    c.save(); return out.getvalue()


def make_zip(tmp, reports):
    path = Path(tmp) / 'r.zip'
    with zipfile.ZipFile(path, 'w') as z:
        for i, data in enumerate(reports): z.writestr('%02d-ABC123-x.pdf' % i, data)
    return path


class ZipTests(unittest.TestCase):
    def setUp(self):
        import tempfile; self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)
    def test_scores_and_ungraded_dash(self):
        path = make_zip(self.dir.name, [report('Sample Alpha', '9'), report('Sample Beta', '-')])
        review = cz.review_from_zip(path, 'ABC123', 'B3 ET')
        a, b = review['students']
        self.assertEqual((a['points'], a['maximum_points'], a['percentage']), (9.0, 12.0, 75.0))
        self.assertIsNone(b['points']); self.assertEqual(a['nigri']['child_id'], 'c1')
        self.assertEqual(review['session_name'], 'B3 ET Test Quiz')
    def test_wrong_class_title_is_refused(self):
        path = make_zip(self.dir.name, [report('Sample Alpha', '9', title='B3 WT Test Quiz')])
        with self.assertRaises(ValueError): cz.review_from_zip(path, 'ABC123', 'B3 ET')
    def test_mixed_sessions_and_wrong_code_are_refused(self):
        path = make_zip(self.dir.name, [report('Sample Alpha', '9'), report('Sample Beta', '8', title='B3 ET Other Quiz')])
        with self.assertRaises(ValueError): cz.review_from_zip(path, 'ABC123', 'B3 ET')
        with self.assertRaises(ValueError): cz.review_from_zip(make_zip(self.dir.name, [report('Sample Alpha', '9')]), 'ZZZ999', 'B3 ET')
    def test_duplicate_student_is_refused(self):
        path = make_zip(self.dir.name, [report('Sample Alpha', '9'), report('Sample Alpha', '8')])
        with self.assertRaises(ValueError): cz.review_from_zip(path, 'ABC123', 'B3 ET')
    def test_score_above_maximum_is_refused(self):
        path = make_zip(self.dir.name, [report('Sample Alpha', '13')])
        with self.assertRaises(ValueError): cz.review_from_zip(path, 'ABC123', 'B3 ET')
    def test_full_hand_off_to_existing_pdf_verification(self):
        from classtime_review import attach_archive_reports
        path = make_zip(self.dir.name, [report('Sample Alpha', '9'), report('Sample Beta', '10')])
        review, pdfs = attach_archive_reports(cz.review_from_zip(path, 'ABC123', 'B3 ET'), path)
        try:
            self.assertEqual(len(pdfs), 2)
            self.assertTrue(all(s['pdf_status'] == 'verified' for s in review['students']))
        finally:
            import shutil; shutil.rmtree(str(Path(next(iter(pdfs.values()))).parent), ignore_errors=True)

if __name__ == '__main__':
    unittest.main()
