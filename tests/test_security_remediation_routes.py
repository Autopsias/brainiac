import unittest

from brain import remediation


class SecurityRoutesTests(unittest.TestCase):
    def test_security_findings_are_visible_banners(self):
        remediation.validate()
        for key in ('read-log:bulk', 'injection:conceal'):
            with self.subTest(key=key):
                self.assertEqual(remediation.resolve(key).disposition, remediation.BANNER)


if __name__ == '__main__':
    unittest.main()
