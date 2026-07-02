"""Lightweight tests for the Hermes runtime integration (no Docker / Hermes).

覆盖纯 Python 层回归点：
- registry 注册 ``hermes`` 且能构造 ``HermesAdapter``（镜像常量正确）；
- runner 参数从 ``CONFIG`` 派生（model 后缀、base_url/api_key/max_tokens 解耦）；
- runner sentinel 协议常量与 legacy 一致。
"""

from __future__ import annotations

import importlib

import pytest

from src.lift.adapters.registry import SUPPORTED_RUNTIMES, create_adapter
from src.lift.pipeline.run_options import RunOptions
from src.paths import HERMES_DOCKER_IMAGE


def test_hermes_registered() -> None:
    """``hermes`` 在 SUPPORTED_RUNTIMES 且工厂返回 HermesAdapter。"""
    assert "hermes" in SUPPORTED_RUNTIMES
    from src.lift.adapters.hermes.adapter import HermesAdapter

    adapter = create_adapter("hermes", RunOptions())
    assert isinstance(adapter, HermesAdapter)
    assert adapter.resolve_docker_image() == HERMES_DOCKER_IMAGE
    assert HERMES_DOCKER_IMAGE == "evolve-eval-hermes:latest"


def test_runner_params_model_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--model`` 取 MODEL_NAME 的 / 后缀；HERMES_MODEL_NAME 优先。"""
    from src.lift.adapters.hermes import container_exec

    cfg = container_exec.CONFIG
    monkeypatch.setattr(cfg, "hermes_model_name", None, raising=False)
    monkeypatch.setattr(cfg, "model", "custom-ep/doubao-seed-2-0-pro", raising=False)
    monkeypatch.setattr(cfg, "work_openai_base_url", "https://work.example/v1", raising=False)
    monkeypatch.setattr(cfg, "hermes_api_url", None, raising=False)
    monkeypatch.setattr(cfg, "work_openai_api_key", "sk-work", raising=False)
    monkeypatch.setattr(cfg, "max_tokens", 51200, raising=False)

    params = container_exec.hermes_runner_params()
    assert params.model == "doubao-seed-2-0-pro"
    assert params.base_url == "https://work.example/v1"
    assert params.api_key == "sk-work"
    assert params.max_tokens == 51200


def test_runner_params_explicit_model_and_api_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """HERMES_MODEL_NAME 覆盖 model 后缀；HERMES_API_URL 覆盖 work base_url。"""
    from src.lift.adapters.hermes import container_exec

    cfg = container_exec.CONFIG
    monkeypatch.setattr(cfg, "hermes_model_name", "explicit-model", raising=False)
    monkeypatch.setattr(cfg, "model", "custom-ep/should-be-ignored", raising=False)
    monkeypatch.setattr(cfg, "hermes_api_url", "https://hermes.example/v1", raising=False)
    monkeypatch.setattr(cfg, "work_openai_base_url", "https://work.example/v1", raising=False)
    monkeypatch.setattr(cfg, "work_openai_api_key", "sk-work", raising=False)
    monkeypatch.setattr(cfg, "max_tokens", 4096, raising=False)

    params = container_exec.hermes_runner_params()
    assert params.model == "explicit-model"
    assert params.base_url == "https://hermes.example/v1"
    assert params.max_tokens == 4096


def test_runner_sentinels_match_legacy() -> None:
    """chat_agent 的 sentinel 常量与容器内 runner 协议一致。"""
    chat_agent = importlib.import_module("src.lift.adapters.hermes.chat_agent")
    assert chat_agent._TASK_END == "__evo_task_end__"
    assert chat_agent._MSG_END == "__evo_msg_end__"
    assert chat_agent._RESP_END == "__evo_resp_end__"
