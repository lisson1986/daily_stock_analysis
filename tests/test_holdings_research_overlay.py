"""Regression guard: 301660 is researched but cannot be selected for paper entries."""
import ast
import unittest
from pathlib import Path

class ResearchOverlayTest(unittest.TestCase):
    def test_overlay_and_paper_candidate_guard(self):
        tree = ast.parse(Path("src/research/runner.py").read_text(encoding="utf-8"))
        source = ast.unparse(tree)
        self.assertIn("research_only = {'301660'}", source)
        self.assertIn("priority_codes = set(cfg['watchlist']) | research_only", source)
        self.assertIn("code not in research_only and snapshot_source", source)

    def test_production_config_unchanged(self):
        import yaml
        cfg = yaml.safe_load(Path("config/research-system.yaml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["watchlist"], ["300136", "301297"])

if __name__ == "__main__":
    unittest.main()
