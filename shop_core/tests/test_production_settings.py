"""End-to-end checks that shop_core/settings.py actually wires env_config's
functions correctly.

settings.py is imported exactly once by this very test process (Django is
already running), so its module-level code cannot be re-exercised with a
different environment inside this process — these tests spawn a fresh
`python manage.py check` subprocess with a controlled environment instead.
This is slower than a plain unit test, so it is used sparingly, only to
confirm the wiring in settings.py itself; the parsing/validation logic is
covered exhaustively (and fast) in test_env_config.py.
"""

import os
import subprocess
import sys
from unittest import mock

from django.test import SimpleTestCase

from shop_core.env_config import DEV_INSECURE_SECRET_KEY

MANAGE_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "manage.py")

# Every environment variable read anywhere by shop_core/settings.py or
# shop_core/env_config.py (verified by grepping both files for env_bool/
# env_int/env_str/env_list/resolve_*/build_database_config call sites — no
# other module in this project reads an application-controlled environment
# variable). Kept as a single authoritative tuple here rather than in
# env_config.py: env_config.py has no existing names registry of its own,
# and adding a test-only one there would be an unrelated production-file
# change for no production benefit.
APPLICATION_ENV_VARS = (
    "DJANGO_DEBUG",
    "DJANGO_SECRET_KEY",
    "DJANGO_ALLOWED_HOSTS",
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    "PAYMENTS_SIMULATION_ENABLED",
    "DJANGO_SECURE_SSL_REDIRECT",
    "DJANGO_SESSION_COOKIE_SECURE",
    "DJANGO_CSRF_COOKIE_SECURE",
    "DJANGO_SECURE_HSTS_SECONDS",
    "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS",
    "DJANGO_SECURE_HSTS_PRELOAD",
    "DJANGO_SECURE_PROXY_SSL_HEADER",
    "DATABASE_URL",
    "DJANGO_STATIC_ROOT",
    "DJANGO_MEDIA_ROOT",
    "DJANGO_LOG_LEVEL",
)


def _sanitized_environ():
    """A full copy of this process's real environment, with every
    application-controlled variable (APPLICATION_ENV_VARS) removed.

    Starting from a full copy — not an empty/PATH-and-PYTHONPATH-only dict
    — is required so platform-level variables the interpreter itself needs
    survive into the subprocess. On Windows/Python 3.12, importing asyncio
    (which manage.py/Django trigger during startup) initializes
    `_overlapped`'s I/O completion port machinery, which depends on system
    environment variables (e.g. SYSTEMROOT) being present; with only
    PATH/PYTHONPATH inherited, that import raises
    `OSError: [WinError 10106] The requested service provider could not be
    loaded or initialized` before Django settings are ever evaluated —
    entirely unrelated to whatever a given test is actually checking.
    Determinism instead comes from explicitly removing every
    application-controlled variable below, so a developer's or CI
    runner's real local/deployment configuration can never leak into these
    tests — not from starting with a stripped-down environment.
    """
    env = os.environ.copy()
    for name in APPLICATION_ENV_VARS:
        env.pop(name, None)
    return env


def _run_check(extra_env):
    """Run `manage.py check` in a subprocess with a sanitized, otherwise
    fully-inherited environment."""
    env = _sanitized_environ()
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, MANAGE_PY, "check"],
        cwd=os.path.dirname(MANAGE_PY),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


class DevelopmentDefaultsRemainUsableTests(SimpleTestCase):
    def test_no_env_vars_at_all_still_passes_check(self):
        result = _run_check({})
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("System check identified no issues", result.stdout + result.stderr)


class ProductionSafetyEnforcedEndToEndTests(SimpleTestCase):
    def test_debug_false_without_secret_key_fails_fast(self):
        result = _run_check({"DJANGO_DEBUG": "False", "DJANGO_ALLOWED_HOSTS": "example.com"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY is required", result.stderr)

    def test_debug_false_with_dev_secret_key_fails_fast(self):
        result = _run_check(
            {
                "DJANGO_DEBUG": "False",
                "DJANGO_ALLOWED_HOSTS": "example.com",
                "DJANGO_SECRET_KEY": DEV_INSECURE_SECRET_KEY,
            }
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("known development-only insecure key", result.stderr)

    def test_debug_false_without_allowed_hosts_fails_fast(self):
        result = _run_check(
            {"DJANGO_DEBUG": "False", "DJANGO_SECRET_KEY": "a-real-unique-production-secret"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_ALLOWED_HOSTS must be set", result.stderr)

    def test_debug_false_fully_configured_passes(self):
        result = _run_check(
            {
                "DJANGO_DEBUG": "False",
                "DJANGO_SECRET_KEY": "a-real-unique-production-secret",
                "DJANGO_ALLOWED_HOSTS": "example.com,www.example.com",
                "DJANGO_CSRF_TRUSTED_ORIGINS": "https://example.com",
            }
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_invalid_boolean_env_var_fails_with_clear_message(self):
        result = _run_check({"DJANGO_SECURE_SSL_REDIRECT": "maybe"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not a valid boolean", result.stderr)

    def test_invalid_database_url_fails_with_clear_message(self):
        result = _run_check({"DATABASE_URL": "mysql://user:pass@host/db"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DATABASE_URL scheme", result.stderr)


class SubprocessEnvironmentSanitizationTests(SimpleTestCase):
    """Regression coverage for the PATH/PYTHONPATH-only subprocess
    environment that caused every test above to fail on Windows/Python 3.12
    with `OSError: [WinError 10106]` before Django settings were ever
    evaluated — asyncio/_overlapped needs real system environment
    variables (e.g. SYSTEMROOT) to initialize, which a PATH/PYTHONPATH-only
    environment does not provide. These tests are platform-agnostic: none
    of them assert a specific, platform-only variable name."""

    def test_system_environment_is_inherited_not_replaced(self):
        """Every real, non-application-controlled variable already set in
        this test process must still be present (and unchanged) in the
        sanitized environment `_run_check` builds — proving inheritance,
        not a PATH/PYTHONPATH-only reconstruction."""
        sanitized = _sanitized_environ()
        non_application_names = set(os.environ) - set(APPLICATION_ENV_VARS)
        self.assertTrue(
            non_application_names,
            "test process unexpectedly has no non-application environment "
            "variables to assert inheritance against",
        )
        for name in non_application_names:
            self.assertEqual(sanitized.get(name), os.environ.get(name))

    def test_application_env_var_present_in_parent_does_not_leak_without_extra_env(self):
        """A project environment variable already set in the parent process
        (a developer's shell, or this CI runner's own deployment config)
        must never leak into a subprocess call unless explicitly supplied
        via `extra_env`. Proven behaviorally, not by inspecting the built
        dict: if DJANGO_DEBUG=False leaked through, production-safety
        would kick in and demand a real DJANGO_SECRET_KEY/DJANGO_ALLOWED_HOSTS
        that this call deliberately never supplies, and the subprocess would
        fail — instead it must still pass, using the development defaults,
        exactly as if the parent process had never set DJANGO_DEBUG at all.
        """
        with mock.patch.dict(os.environ, {"DJANGO_DEBUG": "False"}):
            result = _run_check({})
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("System check identified no issues", result.stdout + result.stderr)

    def test_subprocess_can_initialize_django_under_sanitized_environment(self):
        """A bare Python subprocess given exactly the sanitized+inherited
        environment `_run_check` builds can import and set up Django (which
        itself imports asyncio as part of Django's async support) — the
        same startup path that raises WinError 10106 on Windows when the
        environment is reduced to only PATH/PYTHONPATH."""
        env = _sanitized_environ()
        env.setdefault("DJANGO_SETTINGS_MODULE", "shop_core.settings")
        result = subprocess.run(
            [sys.executable, "-c", "import django; django.setup(); print('OK')"],
            cwd=os.path.dirname(MANAGE_PY),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("OK", result.stdout)
