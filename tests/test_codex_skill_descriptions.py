"""Brainiac's capture/style bundles must load within Codex metadata limits."""
from pathlib import Path
import unittest
from brain.frontmatter import parse_text


class SkillDescriptionTests(unittest.TestCase):
    def test_descriptions_are_bounded_and_client_mirrors_match(self):
        repo = Path(__file__).resolve().parents[1]
        for name in ('overlay-style', 'vault-ingestion'):
            canonical = (repo / '.claude/skills' / name / 'SKILL.md').read_text()
            meta, body = parse_text(canonical)
            self.assertTrue(meta['description'])
            self.assertLessEqual(len(meta['description']), 1024)
            self.assertIn('Hard', body)
            for base in ('.agents/skills', 'plugins/brainiac-kernel/skills'):
                self.assertEqual((repo / base / name / 'SKILL.md').read_text(), canonical)


if __name__ == '__main__':
    unittest.main()
