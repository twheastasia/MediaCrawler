"""Compatibility contracts for the platform/model/store directory split."""
import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from media_platform.weixin.core import GATE_LOG, ROOT
from model.m_weixin import WeixinContent


@pytest.mark.parametrize("legacy,current", [
    ("collection.weixin", "media_platform.weixin.core"),
    ("collection.weixin_extract", "media_platform.weixin.extractor"),
    ("collection.weixin_monitor", "media_platform.weixin.monitor"),
    ("collection.weixin_store", "store.weixin._store_impl"),
    ("collection.wxarticle_records", "store.weixin.records"),
    ("collection.wxarticle_schema", "store.weixin.schema"),
    ("collection.wxarticle_migrate", "store.weixin.migrate"),
])
def test_legacy_modules_forward_to_the_same_implementation(legacy, current):
    assert importlib.import_module(legacy) is importlib.import_module(current)


def test_paths_still_use_the_original_project_and_rate_limit_log():
    assert ROOT == Path(__file__).resolve().parents[1]
    assert GATE_LOG == ROOT / "data" / "weixin" / "requests.jsonl"


@pytest.mark.parametrize("module", ["collection.weixin", "media_platform.weixin"])
def test_both_cli_entries_can_read_status_without_starting_collection(module, tmp_path):
    original = {"platform": "weixin", "run_id": "weixin_test", "status": "running", "pid": 123}
    (tmp_path / "status.json").write_text(json.dumps(original))
    result = subprocess.run([sys.executable, "-m", module, "--run-id", "weixin_test", "--output", str(tmp_path), "--status"],
                            cwd=ROOT, capture_output=True, text=True, check=True, timeout=15)
    assert json.loads(result.stdout) == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["status.json"]


def test_monitor_launches_the_canonical_entry_from_the_project_root(tmp_path, monkeypatch):
    import media_platform.weixin.monitor as module
    calls = []
    (tmp_path / "status.json").write_text(json.dumps({"platform": "weixin", "pid": 456, "status": "completed"}))
    class Child:
        pid = 456
        returncode = 0
        def poll(self): return 0
    def spawn(command, **kwargs):
        calls.append((command, kwargs["cwd"]))
        return Child()
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(sys, "argv", ["collection.weixin", "--run-id", "weixin_test", "--monitor", "--background"])
    assert module.monitor(SimpleNamespace(output=tmp_path)) == 0
    command, cwd = calls[0]
    assert command[:3] == [sys.executable, "-m", "media_platform.weixin"]
    assert "--monitor" not in command and "--background" not in command
    assert cwd == ROOT


def test_model_keeps_original_article_and_future_public_raw_fields():
    data = {"type": "article", "id": "wx_test", "author_id": None, "title": "原文",
            "body": "原始正文", "url": "https://mp.weixin.qq.com/s/test", "time": None,
            "raw": {"article_html": "<p>原始正文</p>", "future_public_field": {"value": "原始值"}}}
    assert WeixinContent.model_validate(data).model_dump() == data
