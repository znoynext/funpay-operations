from __future__ import annotations

import unittest

from funpay_operations.setup_wizard import mask_secret


class SetupWizardTests(unittest.TestCase):
    def test_masks_all_diagnostic_values(self) -> None:
        self.assertEqual(mask_secret(None), "<missing>")
        self.assertEqual(mask_secret("unit-value"), "<masked>")
