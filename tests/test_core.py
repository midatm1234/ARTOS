"""State machine and CLI surface (kept from the skeleton, updated for v0.2)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from artos.cli import build_parser, main
from artos.state_machine import validate_transition


class CoreTest(unittest.TestCase):
    def test_state_machine_rejects_skipped_science_gate(self) -> None:
        validate_transition("intake_created", "inventory_ready")
        with self.assertRaises(ValueError):
            validate_transition("contract_frozen", "complete")
        with self.assertRaises(ValueError):
            validate_transition("routed_hypothesis", "contract_frozen")
        with self.assertRaises(ValueError):
            validate_transition("audit_running", "complete")
        with self.assertRaises(ValueError):
            validate_transition("complete", "primary_running")

    def test_cli_surface_parses(self) -> None:
        parser = build_parser()
        for argv in (["doctor"], ["run", "list"], ["hypotheses", "init", "RUN-1"], ["jobs", "step", "R", "--dry-run"],
                     ["plan", "write", "R", "p.json"], ["evaluate", "R"], ["audit", "adopt", "R", "a.json"],
                     ["run", "route", "R", "hypothesis", "--rationale", "because"], ["run", "cancel", "R"]):
            args = parser.parse_args(argv)
            self.assertTrue(callable(args.func))

    def test_run_list_works_on_empty_root(self) -> None:
        example = Path(__file__).resolve().parents[1] / "artos.example.json"
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "artos.json").write_text(example.read_text(encoding="utf-8"))
            self.assertEqual(main(["--root", tmp, "run", "list"]), 0)

    def test_invalid_config_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "artos.json").write_text(json.dumps({"agents": {"max_budget_usd": -1}}))
            with self.assertRaises(SystemExit) as ctx:
                main(["--root", tmp, "run", "list"])
            self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
