from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.schema.tags import SchemaError, TagLedger, main, validate_evolution

BASE = """\
schema = 1
namespace = "model"

[[tag]]
name = "IRI"
value = 1
status = "active"

[[tag]]
name = "OLD_LITERAL"
value = 2
status = "retired"
"""


class SchemaTagTests(unittest.TestCase):
    def test_render_and_generation_are_deterministic(self) -> None:
        first = TagLedger.parse(BASE)
        reordered = TagLedger.parse(
            BASE.replace(
                '[[tag]]\nname = "IRI"\nvalue = 1\nstatus = "active"\n\n'
                '[[tag]]\nname = "OLD_LITERAL"\nvalue = 2\nstatus = "retired"',
                '[[tag]]\nname = "OLD_LITERAL"\nvalue = 2\nstatus = "retired"\n\n'
                '[[tag]]\nname = "IRI"\nvalue = 1\nstatus = "active"',
            )
        )
        self.assertEqual(first.render(), reordered.render())
        self.assertEqual(first.render_python(), reordered.render_python())
        self.assertIn("IRI = 1", first.render_python())
        self.assertIn("# retired: OLD_LITERAL = 2", first.render_python())

    def test_duplicate_name_and_value_are_rejected(self) -> None:
        with self.assertRaisesRegex(SchemaError, "duplicate tag name"):
            TagLedger.parse(BASE + '\n[[tag]]\nname = "IRI"\nvalue = 3\n')
        with self.assertRaisesRegex(SchemaError, "duplicate tag value"):
            TagLedger.parse(BASE + '\n[[tag]]\nname = "CLASS"\nvalue = 1\n')

    def test_evolution_reserves_retired_names_and_values(self) -> None:
        previous = TagLedger.parse(BASE)
        with self.assertRaisesRegex(SchemaError, "cannot be reactivated"):
            validate_evolution(
                previous,
                TagLedger.parse(BASE.replace('status = "retired"', 'status = "active"')),
            )
        with self.assertRaisesRegex(SchemaError, "was reused"):
            validate_evolution(
                previous,
                TagLedger.parse(BASE.replace("OLD_LITERAL", "NEW_LITERAL")),
            )
        current = TagLedger.parse(BASE + '\n[[tag]]\nname = "CLASS"\nvalue = 3\n')
        validate_evolution(previous, current)

    def test_cli_check_and_stale_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "ledger.toml"
            generated = root / "tags.py"
            ledger.write_text(BASE, encoding="utf-8")
            self.assertEqual(main(["check", str(ledger)]), 0)
            self.assertEqual(main(["generate", str(ledger), str(generated)]), 0)
            self.assertEqual(main(["generate", str(ledger), str(generated), "--check"]), 0)
            generated.write_text("stale\n", encoding="utf-8")
            self.assertEqual(main(["generate", str(ledger), str(generated), "--check"]), 1)


if __name__ == "__main__":
    unittest.main()
