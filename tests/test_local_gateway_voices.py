from app import local_gateway


def test_kokoro_voice_catalog_reports_selected_voices_and_missing_models(tmp_path, monkeypatch):
    monkeypatch.setattr(local_gateway, "KOKORO_MODEL_PATH", tmp_path / "kokoro-v1.0.onnx")
    monkeypatch.setattr(local_gateway, "KOKORO_VOICES_PATH", tmp_path / "voices-v1.0.bin")
    voices = local_gateway._kokoro_voice_status()
    assert {voice["id"] for voice in voices} == {"af_heart", "af_jessica", "am_eric"}
    assert all(voice["available"] is False for voice in voices)
    assert next(voice for voice in voices if voice["id"] == "am_eric")["gender"] == "male"


def test_kokoro_voice_catalog_reports_models_ready_only_when_both_files_exist(tmp_path, monkeypatch):
    model = tmp_path / "kokoro-v1.0.onnx"
    voice_data = tmp_path / "voices-v1.0.bin"
    monkeypatch.setattr(local_gateway, "KOKORO_MODEL_PATH", model)
    monkeypatch.setattr(local_gateway, "KOKORO_VOICES_PATH", voice_data)
    model.write_bytes(b"test-model")
    assert all(voice["available"] is False for voice in local_gateway._kokoro_voice_status())
    voice_data.write_bytes(b"test-voices")
    assert all(voice["available"] is True for voice in local_gateway._kokoro_voice_status())
