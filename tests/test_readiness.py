from pathlib import Path
import unittest


class TechnicalReadinessTests(unittest.TestCase):
    def test_marker_keeps_connection_and_live_write_limitations_explicit(self) -> None:
        document = (
            Path(__file__).resolve().parents[1] / "TECHNICAL_READINESS.md"
        ).read_text(encoding="utf-8")
        self.assertIn("# TECHNICALLY_READY_FOR_CONNECTION", document)
        self.assertIn("mock adapters only", document)
        self.assertIn("Production FunPay lot, price, and raise execution\nremains hard-disabled", document)
