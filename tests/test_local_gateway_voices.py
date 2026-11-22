from app import local_gateway


def test_voice_catalog_only_resolves_known_local_voice_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(local_gateway, "PIPER_VOICE_DIR", tmp_path)
    voice_file = tmp_path / "en_US-amy-medium.onnx"
    voice_file.write_bytes(b"test-model")

    resolved = local_gateway._piper_voice_path("en_US-amy-medium")
    assert resolved == voice_file.resolve()
    assert local_gateway._piper_voice_path("../../secret") is None
    assert local_gateway._piper_voice_path("not-a-real-piper-voice") is None


def test_voice_catalog_reports_missing_models_without_claiming_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(local_gateway, "PIPER_VOICE_DIR", tmp_path)
    monkeypatch.setattr(local_gateway, "PIPER_MODEL", "")
    voices = local_gateway._piper_voice_status()
    amy = next(voice for voice in voices if voice["id"] == "en_US-amy-medium")
    assert amy["gender"] == "female"
    assert amy["accent"] == "American English"
    assert amy["available"] is False
