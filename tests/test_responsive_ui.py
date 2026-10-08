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
        release = "20261008-continuous-settings-1"
        self.assertEqual(INDEX.count(release), 2)

    def test_export_error_toast_is_fixed_and_safe_area_aware(self) -> None:
        self.assertIn('id="toast"', INDEX)
        self.assertRegex(STYLES, re.compile(r"\.toast\s*\{[^}]*position:\s*fixed;", re.DOTALL))
        self.assertIn("env(safe-area-inset-top)", STYLES)

    def test_parameters_remain_continuously_accessible_in_original_order(self) -> None:
        panel = re.search(r'<aside\b[^>]*id="parameterPanel"[^>]*>(.*?)</aside>', INDEX, re.DOTALL)
        self.assertIsNotNone(panel)
        content = panel.group(1)
        sections = re.findall(r'<section\b([^>]*class="control-section"[^>]*)>(.*?)</section>', content, re.DOTALL)
        self.assertEqual(len(sections), 3)
        expected = (
            ("回测范围", (
                "initialCapital", "startDate", "endDate", "monthlySpend",
                "rebalanceFrequency", "annualRebalanceMonth", "rebalanceBand", "rebalanceToTarget",
                "rollingWindowYears", "rebalanceMonthAnalysisEnabled", "rebalanceRuleHint",
                "dipBuyEnabled", "dipBuyDrawdown", "dipBuyTotalParts", "dipBuyLevelMode",
                "dipBuyCostBasisMode", "dipBuyRecoverySellEnabled", "dipBuyAssetCapEnabled",
                "dipBuyAssetCapRatio", "dipBuyBlackoutEnabled", "dipBuyBlackoutMonths",
            )),
            ("现金与消费池", ("repoTargetMode", "repoFixedTarget", "repoFixedRatio", "repoSymbol")),
            ("资产配置", ("assetControls", "allocationSummary", "restoreDefaultAllocation")),
        )
        for (attributes, section), (heading, fields) in zip(sections, expected):
            self.assertNotRegex(attributes, r'\bhidden\b|role="tabpanel"')
            self.assertRegex(section, rf'<h2\b[^>]*>{heading}</h2>')
            for field in fields:
                self.assertEqual(section.count(f'id="{field}"'), 1, field)
                self.assertEqual(INDEX.count(f'id="{field}"'), 1, field)
        for field in (
            "cnCommission", "ibkrPlan", "fxOutBps", "fxInBps", "hkCommission",
            "hkFxBps", "hkPortfolioFee", "usDividendTax", "feeSummary",
        ):
            self.assertEqual(content.count(f'id="{field}"'), 1, field)
            self.assertGreater(content.index(f'id="{field}"'), content.index('id="assetControls"'))
        self.assertNotIn("data-parameter-tab", INDEX)
        self.assertNotIn("data-parameter-panel", INDEX)
        self.assertNotIn("selectParameterStep", APP_JS)
        for field in ("rebalanceBand", "repoFixedRatio"):
            self.assertRegex(content, rf'<input\b[^>]*id="{field}"[^>]*max="1"')

    def test_results_distinguish_draft_run_state_and_rebalance_evidence(self) -> None:
        for field in ("resultState", "resultConfigSummary", "draftNotice", "workspaceMessage", "rebalanceExplanation"):
            self.assertEqual(INDEX.count(f'id="{field}"'), 1)
        self.assertLess(INDEX.index('id="summaryGrid"'), INDEX.index('id="rebalanceExplanation"'))
        self.assertLess(INDEX.index('id="rebalanceExplanation"'), INDEX.index('id="analysisSection"'))
        self.assertIn('id="resultStatusText" class="visually-hidden"', INDEX)
        self.assertLess(INDEX.index('id="analysisSection"'), INDEX.index('id="riskDetails"'))

    def test_typography_uses_shared_readable_scale(self) -> None:
        for token in ("--font-caption: 13px", "--font-body: 14px", "--font-section: 18px", "--font-metric: 28px"):
            self.assertIn(token, STYLES)
        explicit_sizes = [float(size) for size in re.findall(r"font-size:\s*(\d+(?:\.\d+)?)px", STYLES)]
        self.assertTrue(all(size >= 13 for size in explicit_sizes))

    def test_run_and_analysis_recovery_actions_have_visible_hosts(self) -> None:
        for field in ("runBtn", "mobileRunBtn", "workspaceRunBtn", "analysisMessage", "retryAnalysisBtn"):
            self.assertEqual(INDEX.count(f'id="{field}"'), 1)


if __name__ == "__main__":
    unittest.main()
