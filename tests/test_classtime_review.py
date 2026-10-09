import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from classtime_review import match_student, parse_session, verify_pdf, JOBS, get_review
from reportlab.pdfgen import canvas

class ReviewTests(unittest.TestCase):
    def test_all_et_matches_and_class_boundary(self):
        names = ['Arik Traxler','Avrohom Rosenfeld','Kehos Notik','Levi Rozmarin','Mayer Chaikin','Moshe Lapine','Moshe Raichman','Sholom Huebner','Yisroel Oirechman','Yossi Gourarie']
        ids = [match_student(name, 'B3 ET')['child_id'] for name in names]
        self.assertEqual(len(set(ids)), 10)
        self.assertTrue(all(match_student(name, 'B3 WT') is None for name in names))
        self.assertIsNone(match_student('Unknown Student', 'B3 ET'))
    def test_scores_and_missing_are_preserved(self):
        text='Session: "B3 ET Shoroshim Week 4"\nZZKUYA\nB3 ET\n24 pts'
        result=parse_session(['Arik Traxler\n5.00','Yisroel Oirechman\n–'], text,'ZZKUYA','B3 ET')
        self.assertEqual(result['students'][0]['percentage'],20.83)
        self.assertIsNone(result['students'][1]['points'])
        self.assertFalse(result['sync_enabled'])
        with self.assertRaises(ValueError): parse_session(['Arik Traxler\n5.00'],text,'ZZKUYA','B3 WT')
        with self.assertRaises(ValueError): parse_session(['Arik Traxler\n5.00\n2.00'],text,'ZZKUYA','B3 ET')
        with self.assertRaises(ValueError): parse_session(['Arik Traxler\n5.00']*2,text,'ZZKUYA','B3 ET')
    def pdf(self, text):
        output=io.BytesIO(); c=canvas.Canvas(output); c.drawString(30,750,text); c.save(); return output.getvalue()
    def test_pdf_contents_not_filename(self):
        students=[{'classtime_name':'Arik Traxler'},{'classtime_name':'Yossi Gourarie'}]
        student,text=verify_pdf(self.pdf('Student Report: Arik Traxler'),students)
        self.assertEqual(student['classtime_name'],'Arik Traxler')
        for content in ['Unknown Student','Arik Traxler and Yossi Gourarie']:
            with self.assertRaises(ValueError): verify_pdf(self.pdf(content),students)
        with self.assertRaises(ValueError): verify_pdf(b'not a PDF',students)
    def test_owner_and_expiry(self):
        import time
        JOBS['test']={'owner':'owner','expires':time.time()+100,'status':'ready','pdfs':{}}
        self.assertIsNone(get_review('test','other'))
        self.assertIsNotNone(get_review('test','owner'))
        JOBS['test']['expires']=0
        self.assertIsNone(get_review('test','owner'))
        JOBS.pop('test')
    def test_endpoints_do_not_call_nigri(self):
        import app
        client=app.app.test_client()
        with patch('classtime_auth.review_owner', return_value=None), patch('app.run_sync') as attendance, patch('app.run_marks_sync') as marks:
            self.assertEqual(client.post('/classtime/reviews',json={}).status_code,401)
            self.assertEqual(client.get('/classtime/reviews/unknown').status_code,401)
            self.assertEqual(client.get('/classtime/reviews/unknown/pdfs/unknown').status_code,401)
            marks.assert_not_called(); attendance.assert_not_called()
            foreign = client.get('/classtime/reviews/unknown', headers={'Origin':'https://untrusted.example'})
            self.assertNotIn('Access-Control-Allow-Origin', foreign.headers)
            trusted = client.get('/classtime/reviews/unknown', headers={'Origin':'https://simchacohen1.github.io'})
            self.assertEqual(trusted.headers.get('Access-Control-Allow-Origin'), 'https://simchacohen1.github.io')

if __name__ == '__main__': unittest.main()
