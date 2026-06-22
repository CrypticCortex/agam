"""Tests for the shared data-home path resolver."""

from pathlib import Path

from agam import paths


def test_data_home_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGAM_DATA_HOME", raising=False)
    assert paths.data_home() == tmp_path / ".agam"


def test_data_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGAM_DATA_HOME", str(tmp_path / "custom"))
    assert paths.data_home() == tmp_path / "custom"


def test_kg_path_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGAM_DATA_HOME", raising=False)
    monkeypatch.delenv("AGAM_KG_PATH", raising=False)
    assert paths.kg_path() == tmp_path / ".agam" / "knowledge" / "graph.db"


def test_kg_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGAM_KG_PATH", str(tmp_path / "g.db"))
    assert paths.kg_path() == tmp_path / "g.db"


def test_knowledge_dir_follows_kg(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGAM_DATA_HOME", raising=False)
    monkeypatch.delenv("AGAM_KG_PATH", raising=False)
    assert paths.knowledge_dir() == tmp_path / ".agam" / "knowledge"


def test_identity_dir_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGAM_DATA_HOME", raising=False)
    monkeypatch.delenv("AGAM_HOME", raising=False)
    assert paths.identity_dir() == tmp_path / ".agam"


def test_identity_dir_legacy_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGAM_HOME", str(tmp_path / "legacy"))
    assert paths.identity_dir() == tmp_path / "legacy"


def test_prompts_and_queue(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGAM_DATA_HOME", raising=False)
    monkeypatch.delenv("AGAM_HOME", raising=False)
    monkeypatch.delenv("AGAM_PROMPTS_DIR", raising=False)
    assert paths.prompts_dir() == tmp_path / ".agam" / "prompts"
    assert paths.queue_dir() == tmp_path / ".agam" / "queue"
