from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cli_app.arguments import parse_args
from cli_app.diagnostics import run_doctor
from config.settings import Settings, apply_runtime_overrides
from providers.mock_provider import MockModelProvider
from providers.openrouter_provider import OpenRouterModelProvider
from providers.openrouter_capabilities import (
    ModelCapabilityResult,
    ModelCapabilityStatus,
)
from runtime import build_all_managers, build_provider


class RuntimeConfigurationTests(unittest.TestCase):
    class _CompatibleCapabilityClient:
        def inspect(self, model_id: str) -> ModelCapabilityResult:
            return ModelCapabilityResult(
                requested_model_id=model_id,
                resolved_model_id=model_id,
                status=ModelCapabilityStatus.COMPATIBLE,
                reason="TEST_COMPATIBLE",
                endpoint_count=1,
                compatible_endpoint_count=1,
            )

    def _settings(self, data_dir: Path, *, provider: str, safe: bool) -> Settings:
        with patch.dict(
            os.environ,
            {
                "PRDCP_DATA_DIR": str(data_dir),
                "PRDCP_PROVIDER": provider,
                "PRDCP_DEMO_SAFE_MODE": "true" if safe else "false",
                "OPENROUTER_API_KEY": "test-key",
            },
        ):
            return Settings.from_env()

    def test_cli_options_are_optional_and_safe_flags_are_exclusive(self) -> None:
        args = parse_args([])
        self.assertIsNone(args.provider)
        self.assertIsNone(args.demo_safe_mode)
        self.assertEqual(parse_args(["--provider", "openrouter"]).provider, "openrouter")
        self.assertTrue(parse_args(["--safe-mode"]).demo_safe_mode)
        self.assertFalse(parse_args(["--no-safe-mode"]).demo_safe_mode)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_args(["--safe-mode", "--no-safe-mode"])

    def test_cli_provider_override_has_precedence_over_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for environment, cli, expected in (
                ("mock", None, "mock"),
                ("openrouter", None, "openrouter"),
                ("mock", "openrouter", "openrouter"),
                ("openrouter", "mock", "mock"),
            ):
                settings = self._settings(Path(temporary), provider=environment, safe=True)
                effective = apply_runtime_overrides(settings, provider=cli)
                self.assertEqual(effective.provider, expected)

    def test_cli_safe_mode_override_has_precedence_over_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for environment, cli, expected in (
                (True, None, True),
                (False, None, False),
                (True, False, False),
                (False, True, True),
            ):
                settings = self._settings(
                    Path(temporary), provider="mock", safe=environment
                )
                effective = apply_runtime_overrides(settings, demo_safe_mode=cli)
                self.assertIs(effective.demo_safe_mode, expected)

    def test_all_four_provider_and_safe_mode_combinations_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for provider_id in ("mock", "openrouter"):
                for safe in (True, False):
                    settings = self._settings(
                        Path(temporary) / f"{provider_id}-{safe}",
                        provider=provider_id,
                        safe=safe,
                    )
                    provider = build_provider(settings)
                    self.assertEqual(provider.provider_id, provider_id)
                    self.assertEqual(
                        provider.reservation_root,
                        settings.data_dir / "provider_call_reservations",
                    )
                    self.assertIsInstance(
                        provider,
                        MockModelProvider
                        if provider_id == "mock"
                        else OpenRouterModelProvider,
                    )

    def test_all_five_managers_share_one_effective_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for provider_id, safe in (
                ("mock", True),
                ("mock", False),
                ("openrouter", True),
                ("openrouter", False),
            ):
                settings = self._settings(
                    Path(temporary) / f"all-{provider_id}-{safe}",
                    provider=provider_id,
                    safe=safe,
                )
                managers = build_all_managers(settings)
                providers = [
                    next(iter(manager.registry._agents.values())).provider
                    for manager in managers
                ]
                self.assertTrue(all(manager.demo_safe_mode is safe for manager in managers))
                self.assertTrue(all(item is providers[0] for item in providers))
                self.assertEqual(providers[0].provider_id, provider_id)
                self.assertTrue(
                    all(
                        agent.demo_safe_mode is safe
                        for manager in managers
                        for agent in manager.registry._agents.values()
                    )
                )

    def test_doctor_reports_effective_configuration_and_unsafe_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = self._settings(
                Path(temporary), provider="openrouter", safe=False
            )
            checks = run_doctor(
                settings,
                capability_client=self._CompatibleCapabilityClient(),
            )
            effective = next(
                check for check in checks if check.name == "Effective Runtime Configuration"
            )
            self.assertIn("provider=openrouter", effective.detail)
            self.assertIn("demo_safe_mode=false", effective.detail)
            warning = next(check for check in checks if check.name == "Runtime safety")
            self.assertEqual(warning.level, "WARN")
            self.assertIn("additional provider calls", warning.action or "")
            capability = next(
                check for check in checks if check.name == "MODEL CAPABILITY PREFLIGHT"
            )
            self.assertEqual(capability.level, "PASS")
            self.assertIn("Compatible: 31/31", capability.detail)

    def test_environment_model_override_is_the_preflight_effective_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(
                os.environ,
                {"MODEL_PRODUCER_TOPIC_SCOUT": "override/strict-model"},
            ):
                settings = self._settings(
                    Path(temporary), provider="openrouter", safe=True
                )
            self.assertEqual(
                settings.models["producer.topic_scout"],
                "override/strict-model",
            )

    def test_doctor_fails_closed_for_incompatible_and_unknown_models(self) -> None:
        class SelectiveCapabilityClient:
            def inspect(self, model_id: str) -> ModelCapabilityResult:
                if model_id == "bad/model":
                    status = ModelCapabilityStatus.INCOMPATIBLE
                    reason = "REQUIRED_PARAMETERS_UNSUPPORTED"
                elif model_id == "unknown/model":
                    status = ModelCapabilityStatus.UNKNOWN
                    reason = "METADATA_UNAVAILABLE:TimeoutError"
                else:
                    status = ModelCapabilityStatus.COMPATIBLE
                    reason = "TEST_COMPATIBLE"
                return ModelCapabilityResult(
                    requested_model_id=model_id,
                    resolved_model_id=model_id,
                    status=status,
                    reason=reason,
                    endpoint_count=1,
                    compatible_endpoint_count=(
                        1 if status is ModelCapabilityStatus.COMPATIBLE else 0
                    ),
                )

        with tempfile.TemporaryDirectory() as temporary:
            base = self._settings(
                Path(temporary), provider="openrouter", safe=True
            )
            models = dict(base.models)
            models["producer.topic_scout"] = "bad/model"
            models["producer.topic_selector"] = "unknown/model"
            checks = run_doctor(
                replace(base, models=models),
                capability_client=SelectiveCapabilityClient(),
            )
        summary = next(
            check for check in checks if check.name == "MODEL CAPABILITY PREFLIGHT"
        )
        self.assertEqual(summary.level, "FAIL")
        self.assertIn("Unknown: 1", summary.detail)
        self.assertIn("Failed: 1", summary.detail)

    def test_runtime_override_does_not_mutate_base_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = self._settings(Path(temporary), provider="mock", safe=True)
            effective = apply_runtime_overrides(
                base, provider="openrouter", demo_safe_mode=False
            )
            self.assertEqual((base.provider, base.demo_safe_mode), ("mock", True))
            self.assertEqual(
                (effective.provider, effective.demo_safe_mode),
                ("openrouter", False),
            )
            self.assertEqual(effective.data_dir, base.data_dir)


if __name__ == "__main__":
    unittest.main()
