import pytest
import yaml

from clipper import config


def test_defaults_are_valid():
    cfg = config.validate({})
    assert cfg["detect"]["chat_weight"] == 0.6
    assert cfg["streamers"] == {}


def test_template_is_valid_and_complete():
    cfg = config.validate(yaml.safe_load(config.TEMPLATE))
    assert "example" in cfg["streamers"]
    assert cfg["streamers"]["example"]["facecam"]["w"] == 480


def test_partial_config_keeps_other_defaults():
    cfg = config.validate({"detect": {"clip_len": 20, "pre_roll": 5}})
    assert cfg["detect"]["clip_len"] == 20
    assert cfg["detect"]["chat_lag"] == 8


@pytest.mark.parametrize("raw, fragment", [
    ({"detct": {}}, "Did you mean 'detect'"),
    ({"detect": {"clip_length": 30}}, "Did you mean 'clip_len'"),
    ({"detect": {"clip_len": "long"}}, "must be a number"),
    ({"detect": {"clip_len": 5000}}, "between 10 and 180"),
    ({"detect": {"chat_weight": 0, "audio_weight": 0}}, "cannot both be 0"),
    ({"detect": {"clip_len": 60, "max_len": 30}}, "max_len"),
    ({"edit": {"layout": "sideways"}}, "one of"),
    ({"edit": {"caption_highlight": "yellow"}}, "colour"),
    ({"youtube": {"privacy": "everyone"}}, "one of"),
    ({"streamers": {"bad name!": {}}}, "letters, digits"),
    ({"streamers": {"a": {"facecam": {"x": 0, "y": 0, "w": 3000, "h": 200}}}}, "does not fit"),
    ({"streamers": {"a": {"facecam": {"x": 0, "y": 0}}}}, "must look like"),
    ({"streamers": {"a": {"credits": "x"}}}, "Did you mean 'credit'"),
    (["not", "a", "mapping"], "key: value"),
])
def test_bad_config_is_rejected_with_a_clear_message(raw, fragment):
    with pytest.raises(config.ConfigError) as e:
        config.validate(raw)
    assert fragment in str(e.value)


def test_streamer_credit_defaults_to_key():
    cfg = config.validate({"streamers": {"alice": {}}})
    assert cfg["streamers"]["alice"]["credit"] == "alice"
    assert cfg["streamers"]["alice"]["facecam"] is None


def test_unknown_streamer_message(workspace):
    cfg = config.load_config()
    with pytest.raises(config.ConfigError) as e:
        config.streamer_cfg(cfg, "nobody")
    assert "example" in str(e.value)


def test_broken_yaml_is_reported(workspace):
    (workspace / "config.yaml").write_text("detect: [unclosed\n")
    with pytest.raises(config.ConfigError) as e:
        config.load_config()
    assert "not valid YAML" in str(e.value)


def test_safe_name():
    assert config.safe_name("../../etc/passwd") == "etc_passwd"
    assert config.safe_name("v1234567890") == "v1234567890"
    assert config.safe_name("???") == "clip"


def test_init_workspace_creates_config_once(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPPER_HOME", str(tmp_path))
    assert config.init_workspace()
    (tmp_path / "config.yaml").write_text("game: Mine\n")
    assert config.init_workspace() == []
    assert config.load_config()["game"] == "Mine"
