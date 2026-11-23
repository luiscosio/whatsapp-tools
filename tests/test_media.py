from pathlib import Path
from types import SimpleNamespace

from wa import cli, media


def test_video_preparation_preserves_original_and_cleans_up(tmp_path, monkeypatch):
    source = tmp_path / "demo.mov"
    source.write_bytes(b"source")
    monkeypatch.setattr(media, "MAX_VIDEO_BYTES", 1)

    def convert(args, **kwargs):
        if args[0] == "ffprobe":
            return SimpleNamespace(stdout="2")
        Path(args[-1]).write_bytes(b"converted")

    monkeypatch.setattr(media.subprocess, "run", convert)
    with media.prepared_media(source) as output:
        assert output.read_bytes() == b"converted"
        assert output.parent != source.parent
    assert not output.exists()
    assert source.read_bytes() == b"source"


def test_preview_skips_conversion_and_browser(tmp_path, monkeypatch):
    source = tmp_path / "demo.mov"
    source.write_bytes(b"source")
    message = tmp_path / "message.md"
    message.write_text("Fixture")

    def unexpected(*args, **kwargs):
        raise AssertionError("Preview started a live operation")

    monkeypatch.setattr(media, "prepared_media", unexpected)
    monkeypatch.setattr(cli, "open_page", unexpected)
    assert cli.main(["send", "--chat", "Fixture", "--file", str(message), "--media", str(source)]) == 0
