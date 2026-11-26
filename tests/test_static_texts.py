"""The German interface: every text the page translates has a German entry – no English slips through."""

import ast
import re
import unittest
from pathlib import Path

from backend import engine

STATIC = Path(__file__).resolve().parents[1] / "backend" / "static"
_ENTRY = re.compile(r"^\s+'((?:[^'\\]|\\.)*)':\s*'", re.M)
_CALL = re.compile(r"(?<![\w.$])(?:LR\.)?t\(\s*'((?:[^'\\]|\\.)*)'")


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), text)


def german_keys() -> set[str]:
    return {_unescape(m.group(1)) for m in _ENTRY.finditer((STATIC / "i18n.js").read_text(encoding="utf-8"))}


class GermanTextTests(unittest.TestCase):
    def test_every_translated_text_has_a_german_entry(self):
        keys = german_keys()
        missing = []
        for path in sorted(STATIC.glob("*.js")):  # i18n.js too: the templates for server messages
            for match in _CALL.finditer(path.read_text(encoding="utf-8")):
                if _unescape(match.group(1)) not in keys:
                    missing.append(f"{path.name}: {_unescape(match.group(1))!r}")
        self.assertEqual(missing, [], "texts without a German entry in i18n.js")

    def test_model_descriptions_from_the_catalog_are_german_too(self):
        keys = german_keys()
        missing = [model.description for model in engine.CATALOG if model.description not in keys]
        self.assertEqual(missing, [], "catalog descriptions without a German entry in i18n.js")

    def test_translated_server_sentences_still_exist_on_the_server(self):
        """A changed English sentence on the server would silently lose its German translation."""
        i18n = (STATIC / "i18n.js").read_text(encoding="utf-8")
        section = i18n.split("// messages from the server (errors)", 1)[1].split("\n    //", 1)[0]
        sentences = [key for key in (_unescape(m.group(1)) for m in _ENTRY.finditer(section)) if "{" not in key]
        self.assertGreater(len(sentences), 15)
        backend = Path(__file__).resolve().parents[1] / "backend"
        constants = set()
        for path in backend.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    constants.add(node.value)
        self.assertEqual([s for s in sentences if s not in constants], [], "translated, but no longer sent by the server")


if __name__ == "__main__":
    unittest.main()
