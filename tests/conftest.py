"""Shared pytest fixtures.

Test-isolation safety net: strip every agam env var before each test so a test
can never read from or write to a real ~/.claude or ~/.agam via the ambient
environment. Tests that need these set provide their own via monkeypatch or by
passing explicit paths. This guards against the failure mode where a function
that defaults to a real graph path silently mutates the user's knowledge graph
during a test run.
"""

import pytest

_AGAM_ENV_VARS = (
    "AGAM_KG_PATH",
    "AGAM_KG_DIR",
    "AGAM_HOME",
    "AGAM_DATA_HOME",
    "AGAM_SOURCE_AGENT",
    "AGAM_PROMPTS_DIR",
    "AGAM_SESSIONS_DIR",
    "AGAM_TOOLS_DIR",
    "AGAM_HOOKS_DIR",
    "AGAM_WORK_LOG",
    "AGAM_LLM_CLI",
    "AGAM_LLM_CLI_PIN",
    "AGAM_GRAPH_ONLY",
)


@pytest.fixture(autouse=True)
def _isolate_agam_env(monkeypatch):
    for var in _AGAM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
