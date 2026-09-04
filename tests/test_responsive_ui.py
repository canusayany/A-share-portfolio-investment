from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STYLES = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")
APP_JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")


class ResponsiveUiContractTests(unittest.TestCase):
    def test_tablet_breakpoint_and_runtime_query_stay_aligned(self) -> None:
        self.assertIn('@media (max-width: 1100px)', STYLES)
        self.assertIn('const MOBILE_LAYOUT_QUERY = "(max-width: 1100px)";', APP_JS)
        self.assertNotIn('matchMedia("(max-width: 900px)")', APP_JS)

    def test_touch_targets_and_safe_areas_have_mobile_contracts(self) -> None:
        self.assertIn("--touch-target: 44px;", STYLES)
        self.assertIn("touch-action: manipulation;", STYLES)
        self.assertIn("env(safe-area-inset-top)", STYLES)
        self.assertIn("env(safe-area-inset-bottom)", STYLES)
        self.assertRegex(
            STYLES,
            re.compile(
                r"@media \(max-width: 1100px\).*?\.csv-export-dialog input\s*\{[^}]*"
                r"min-height:\s*var\(--touch-target\);",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            STYLES,
            re.compile(
                r"@media \(max-width: 1100px\).*?\.asset-comovement-toolbar select\s*\{[^}]*"
                r"min-height:\s*var\(--touch-target\);",
                re.DOTALL,
            ),
        )

    def test_small_phone_header_and_drawers_have_dedicated_layouts(self) -> None:
        self.assertIn(".mobile-app-bar > .mobile-app-actions", STYLES)
        self.assertRegex(
            STYLES,
            re.compile(
                r"@media \(max-width: 600px\).*?\.mobile-app-actions\s*\{.*?"
                r"grid-template-columns:\s*repeat\(3,\s*minmax\(0,\s*1fr\)\)",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            STYLES,
            re.compile(
                r"@media \(max-width: 1100px\).*?\.workspace-actions\s*\{\s*display:\s*none;",
                re.DOTALL,
            ),
        )
        self.assertIn("@media (max-height: 600px) and (max-width: 1100px)", STYLES)

    def test_responsive_release_cache_busts_both_static_assets(self) -> None:
        release = "20260904-current-run-cash-export-3"
        self.assertEqual(INDEX.count(release), 2)

    def test_export_error_toast_is_fixed_and_safe_area_aware(self) -> None:
        self.assertIn('id="toast"', INDEX)
        self.assertRegex(STYLES, re.compile(r"\.toast\s*\{[^}]*position:\s*fixed;", re.DOTALL))
        self.assertIn("env(safe-area-inset-top)", STYLES)


if __name__ == "__main__":
    unittest.main()
