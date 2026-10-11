"""Blank New Mark inspection: one validated read-only GET, NEVER submit forms."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nigri_http_probe as probe


LISTING = ('<a href="/main/default_os_prog.asp?section=teachers&amp;subSection=tests&amp;side=2">'
           'Create New Mark</a>')
FORM = ('<form method="post" action="/main/default_os_prog.asp?submit=save">'
        '<select id="testGradeID" name="testGradeID"><option value="SECRET">SECRET</option></select>'
        '<select id="testTopicID" name="testTopicID"></select>'
        '<input name="testName" value="SENSITIVE_NAME">'
        '<input name="testDate" value="DO_NOT_DISCLOSE">'
        '<textarea name="testDesc"></textarea>'
        '<input type="radio" name="testType" value="test">'
        '<input type="submit" name="submit" value="Create Mark!">'
        '</form>')


class FakeResponse:
    def __init__(self, html=FORM, status=200):
        self.status_code = status
        self.text = html
        self.content = html.encode()
        self.headers = {'Location': 'https://example.invalid/ignored'}


class FakeSession:
    def __init__(self, *, status=200, html=FORM):
        self.calls = []
        self.status = status
        self.html = html

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return FakeResponse(self.html, self.status)


class BlankMarkProbeTests(unittest.TestCase):
    def run_probe(self, listing=LISTING, **kwargs):
        session = FakeSession(**kwargs)
        result = probe._probe_blank_mark_form(session, probe._page(FakeResponse(listing)))
        return result, session

    def test_readonly_get_finds_expected_form_structure(self):
        result, session = self.run_probe()
        self.assertEqual(result['status'], 'blank_form_found')
        self.assertEqual(result['field_names'], [
            'testDate', 'testDesc', 'testGradeID', 'testName', 'testTopicID', 'testType'])
        self.assertTrue(result['create_button_present'])
        self.assertTrue(result['only_one_extra_get'])
        self.assertTrue(result['no_submit_performed'])
        self.assertFalse(result['nigri_writes'])
        self.assertEqual(len(session.calls), 1)
        method, url, options = session.calls[0]
        self.assertEqual(method, 'GET')
        self.assertIn('side=2', url)
        self.assertIs(options['allow_redirects'], False)
        self.assertNotIn('SENSITIVE_NAME', str(result))
        self.assertNotIn('DO_NOT_DISCLOSE', str(result))
        self.assertNotIn('SECRET', str(result))

    def test_redirect_is_never_followed(self):
        result, session = self.run_probe(status=302)
        self.assertEqual(result['status'], 'blank_form_get_not_200')
        self.assertTrue(result['redirect_not_followed'])
        self.assertEqual(len(session.calls), 1)
        self.assertIs(session.calls[0][2]['allow_redirects'], False)

    def test_get_does_not_claim_success_for_nonform(self):
        result, session = self.run_probe(html='<div>Nothing here</div>')
        self.assertEqual(result['status'], 'blank_form_unconfirmed')
        self.assertFalse(result['form_detected'])
        self.assertEqual(len(session.calls), 1)

    def test_known_new_form_action_allowed(self):
        link = ('<a href="/main/default_os_prog.asp?section=teachers&amp;'
                'spec=tests&amp;action=new_test">Create New Mark</a>')
        result, session = self.run_probe(link)
        self.assertEqual(result['status'], 'blank_form_found')
        self.assertEqual(len(session.calls), 1)

    def test_malformed_or_mutating_links_never_requested(self):
        urls = [
            '', '#new', 'javascript:submitMark()',
            'https://evil.example/main/default_os_prog.asp?section=teachers&spec=tests',
            '/main/default_os_prog.asp?section=teachers&subSection=tests&action=delete',
            '/main/default_os_prog.asp?section=teachers&subSection=tests&action=save',
            '/main/default_os_prog.asp?section=teachers&subSection=tests&action=edit',
            '/main/default_os_prog.asp?section=teachers&subSection=tests&testID=42',
            '/main/default_os_prog.asp?section=teachers&subSection=tests&submit=1',
            '/main/default_os_prog.asp?section=students&subSection=tests&side=2',
            '/main/other.asp?section=teachers&subSection=tests&side=2',
            '/main/default_os_prog.asp?section=teachers&subSection=tests&side=2#next',
        ]
        for url in urls:
            with self.subTest(url=url):
                listing = '<a href="' + url.replace('&', '&amp;') + '">Create New Mark</a>'
                result, session = self.run_probe(listing)
                self.assertTrue(result['status'].startswith('blank_form_link_'))
                self.assertEqual(session.calls, [])
                self.assertFalse(result['nigri_writes'])

    def test_rejected_link_reports_only_safe_shape(self):
        unsafe = ('/main/other.asp?section=teachers&spec=tests&token=PRIVATE_TOKEN'
                  '&action=save&testID=813')
        listing = '<a href="' + unsafe.replace('&', '&amp;') + '">Create New Mark</a>'
        result, session = self.run_probe(listing)
        self.assertEqual(session.calls, [])
        diag = result['link_diagnostics']
        self.assertEqual(diag['link_kind'], 'same_host_https')
        self.assertFalse(diag['expected_asp_path'])
        self.assertEqual(diag['recognized_query_keys'], ['action', 'section', 'spec'])
        self.assertEqual(diag['unknown_query_key_count'], 2)
        self.assertEqual(diag['safe_unknown_parameter_names'], ['testID'])
        self.assertEqual(diag['redacted_parameter_name_count'], 1)
        self.assertEqual(diag['unsupported_action_key_count'], 1)
        self.assertTrue(diag['no_navigation'])
        self.assertNotIn('PRIVATE_TOKEN', str(result))
        self.assertNotIn('813', str(result))
        self.assertNotIn('token', str(result))
        self.assertNotIn('/main/other.asp', str(result))

    def test_link_with_no_query_keys_does_not_expose_path(self):
        result, session = self.run_probe(
            '<a href="/main/nigri-new-mark.asp">Create New Mark</a>')
        self.assertEqual(session.calls, [])
        self.assertEqual(result['link_diagnostics']['recognized_query_keys'], [])
        self.assertFalse(result['link_diagnostics']['expected_asp_path'])
        self.assertNotIn('nigri-new-mark.asp', str(result))

    def test_missing_query_names_are_reported_without_values(self):
        target = ('/main/default_os_prog.asp?section=teachers&spec=tests'
                  '&menuID=SECRET_NUMBER&position=SECRET_POSITION'
                  '&token=SECRET_TOKEN&loginEmail=PRIVATE_PERSON')
        html = '<a href="' + target.replace('&', '&amp;') + '">Create New Mark</a>'
        result, session = self.run_probe(html)
        self.assertEqual(session.calls, [])
        diag = result['link_diagnostics']
        self.assertEqual(diag['safe_unknown_parameter_names'], ['menuID', 'position'])
        self.assertEqual(diag['redacted_parameter_name_count'], 2)
        self.assertEqual(diag['unknown_query_key_count'], 4)
        self.assertFalse(result['nigri_writes'])
        for secret in ('SECRET_NUMBER', 'SECRET_POSITION', 'SECRET_TOKEN',
                       'PRIVATE_PERSON', 'loginEmail'):
            self.assertNotIn(secret, str(result))

    def test_selection_parameter_types_no_values(self):
        link = ('/main/default_os_prog.asp?section=teachers&spec=tests'
                '&testCoursTypeSelect=0&testGradeSelect=12'
                '&testID=0&testTopicSelect=')
        html = '<a href="' + link.replace('&', '&amp;') + '">Create New Mark</a>'
        result, session = self.run_probe(html)
        self.assertEqual(session.calls, [])
        diag = result['link_diagnostics']
        self.assertEqual(diag['selection_parameter_kinds'], {
            'testCoursTypeSelect': 'zero',
            'testGradeSelect': 'nonzero_number',
            'testID': 'zero',
            'testTopicSelect': 'blank',
        })
        self.assertFalse(diag['test_id_may_reference_existing_mark'])
        self.assertFalse(result['nigri_writes'])
        self.assertNotIn('12', str(diag))

    def test_nonzero_test_id_may_reference_existing_mark(self):
        link = ('/main/default_os_prog.asp?section=teachers&spec=tests'
                '&testCoursTypeSelect=0&testGradeSelect=&testID=98765432&testTopicSelect=1')
        result, session = self.run_probe(
            '<a href="' + link.replace('&', '&amp;') + '">Create New Mark</a>')
        self.assertEqual(session.calls, [])
        diag = result['link_diagnostics']
        self.assertEqual(diag['selection_parameter_kinds']['testID'], 'nonzero_number')
        self.assertTrue(diag['test_id_may_reference_existing_mark'])
        self.assertNotIn('98765432', str(diag))
        self.assertFalse(result['nigri_writes'])

    def test_sensitive_selector_values_are_redacted(self):
        link = ('/main/default_os_prog.asp?section=teachers&spec=tests'
                '&testCoursTypeSelect=SENSITIVE_DATA&testID=PRIVATE_ID'
                '&testGradeSelect=HIDDEN_TEXT_VALUE&testTopicSelect=-9')
        result, session = self.run_probe(
            '<a href="' + link.replace('&', '&amp;') + '">Create New Mark</a>')
        self.assertEqual(session.calls, [])
        diag = result['link_diagnostics']
        self.assertEqual(set(diag['selection_parameter_kinds'].values()),
                         {'other_redacted'})
        self.assertTrue(diag['test_id_may_reference_existing_mark'])
        for value in ('SENSITIVE_DATA', 'PRIVATE_ID', 'HIDDEN_TEXT_VALUE', '-9'):
            self.assertNotIn(value, str(result))

    def test_duplicate_test_id_is_not_considered_blank(self):
        link = ('/main/default_os_prog.asp?section=teachers&spec=tests'
                '&testID=0&testID=999')
        result, session = self.run_probe(
            '<a href="' + link.replace('&', '&amp;') + '">Create New Mark</a>')
        self.assertEqual(session.calls, [])
        diag = result['link_diagnostics']
        self.assertEqual(diag['selection_parameter_kinds']['testID'],
                         'repeated_or_missing')
        self.assertTrue(diag['test_id_may_reference_existing_mark'])
        self.assertTrue(diag['has_duplicate_keys'])

    def test_multiple_create_links_are_rejected(self):
        result, session = self.run_probe(LISTING + LISTING)
        self.assertEqual(result['status'], 'blank_form_link_link_missing_or_ambiguous')
        self.assertEqual(session.calls, [])

    def test_existing_marks_listing_is_not_new_form(self):
        listing = '<a href="' + probe.MARKS_URL.replace('&', '&amp;') + '">Create New Mark</a>'
        result, session = self.run_probe(listing)
        self.assertEqual(result['status'], 'blank_form_link_link_is_listing')
        self.assertEqual(session.calls, [])


if __name__ == '__main__':
    unittest.main()
