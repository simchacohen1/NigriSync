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

class ClassCheckTests(unittest.TestCase):
    def full(self, reports, section='B3 ET'):
        import tempfile, shutil
        from classtime_review import attach_archive_reports
        with tempfile.TemporaryDirectory() as d:
            path = make_zip(d, reports)
            review = cz.review_from_zip(path, 'ABC123', section)
            result, pdfs = attach_archive_reports(review, path)
            if pdfs: shutil.rmtree(str(Path(next(iter(pdfs.values()))).parent), ignore_errors=True)
            return result
    def test_blank_class_field_with_underscored_title_is_accepted(self):
        result = self.full([report('Sample Alpha', '9', title='B3_ET Test Quiz', klass='')])
        self.assertEqual(result['students'][0]['pdf_status'], 'verified')
    def test_lowercase_and_spacing_are_accepted(self):
        self.full([report('Sample Alpha', '9', title='b3  et Test Quiz', klass='b3_et')])
    def test_filled_class_field_naming_another_class_is_refused(self):
        with self.assertRaises(ValueError): self.full([report('Sample Alpha', '9', klass='B3 WT')])
    def test_blank_class_field_and_other_class_title_is_refused(self):
        with self.assertRaises(ValueError): self.full([report('Sample Alpha', '9', title='B3 WT Test Quiz', klass='')])
    def test_similar_class_name_is_not_a_match(self):
        with self.assertRaises(ValueError): self.full([report('Sample Alpha', '9', title='B3 ETA Test Quiz', klass='')])
    def test_class_log_has_no_names_or_scores(self):
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(ValueError): self.full([report('Sample Alpha', '9', klass='B3 WT')])
        log = out.getvalue()
        self.assertIn('picked=B3 ET', log); self.assertIn('pdf_class_field=B3 WT', log)
        for secret in ('Sample Alpha', 'ABC123', '9 / 12'): self.assertNotIn(secret, log)

class ScoreCheckTests(ClassCheckTests):
    def test_student_who_did_not_take_quiz_is_accepted(self):
        result = self.full([report('Sample Alpha', '9'), report('Sample Beta', '-')])
        self.assertTrue(all(s['pdf_status'] == 'verified' for s in result['students']))
        self.assertIsNone(result['students'][1]['points'])
    def test_decimal_score_is_accepted(self):
        result = self.full([report('Sample Alpha', '8.5')])
        self.assertEqual(result['students'][0]['points'], 8.5)
    def test_real_mismatch_is_still_refused_and_log_is_number_free(self):
        import contextlib, tempfile
        from classtime_review import attach_archive_reports
        with tempfile.TemporaryDirectory() as d:
            path = make_zip(d, [report('Sample Alpha', '9'), report('Sample Beta', '7')])
            review = cz.review_from_zip(path, 'ABC123', 'B3 ET')
            review['students'][1]['points'] = 6.5   # review disagrees with the PDF
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                with self.assertRaises(ValueError): attach_archive_reports(review, path)
        log = out.getvalue()
        self.assertIn('1 of 2 PDFs failed', log); self.assertIn('points differ (within 1 point', log); self.assertIn('pdf_pattern=# / ## points', log)
        for secret in ('Sample', '7 / 12', '6.5', '9 / 12'): self.assertNotIn(secret, log)
    def test_dash_against_a_number_is_refused(self):
        import tempfile
        from classtime_review import attach_archive_reports
        with tempfile.TemporaryDirectory() as d:
            path = make_zip(d, [report('Sample Alpha', '-')])
            review = cz.review_from_zip(path, 'ABC123', 'B3 ET'); review['students'][0]['points'] = 5.0
            with self.assertRaises(ValueError): attach_archive_reports(review, path)

if __name__ == '__main__':
    unittest.main()
