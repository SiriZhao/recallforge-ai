from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from app.config import settings
from app.main import app
from app.schemas.review import OCRConfig, ParsedFile, ParsedPage
from app.services import file_parser
from app.services.chapter_extractor import detect_chapter_title, is_bad_unit_title
from app.services.subprocess_utils import subprocess_no_window_kwargs
from app.services.file_parser import native_pdf_text_is_reliable


def test_parse_endpoint_returns_unified_structure(
    tmp_path: Path, monkeypatch
) -> None:
    settings.upload_dir = tmp_path
    uploaded = tmp_path / "lecture.docx"
    uploaded.write_bytes(b"mock docx")

    def fake_parse_file(path: Path, ocr_config: OCRConfig) -> ParsedFile:
        return ParsedFile(
            filename=path.name,
            file_type=path.suffix,
            path=str(path),
            pages=[
                ParsedPage(
                    page_number=1,
                    text="chapter one review notes",
                    source="text_extract",
                )
            ],
            raw_text="chapter one review notes",
        )

    monkeypatch.setattr("app.routers.parse.parse_file", fake_parse_file)
    client = TestClient(app)

    response = client.post("/parse", json={"files": [uploaded.name]})

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "已成功解析 1 个文件。"
    assert body["files"][0]["filename"] == "lecture.docx"
    assert body["files"][0]["pages"][0]["source"] == "text_extract"
    assert body["files"][0]["raw_text"] == "chapter one review notes"


def test_parse_rejects_paths_outside_uploads(tmp_path: Path) -> None:
    settings.upload_dir = tmp_path / "uploads"
    settings.upload_dir.mkdir()
    outside_file = tmp_path / "outside.pdf"
    outside_file.write_bytes(b"outside")
    client = TestClient(app)

    response = client.post("/parse", json={"files": [str(outside_file)]})

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "只能解析上传目录中的文件。"
    )


def test_parse_rejects_unsupported_file_type(tmp_path: Path) -> None:
    settings.upload_dir = tmp_path
    uploaded = tmp_path / "tool.exe"
    uploaded.write_bytes(b"not allowed")
    client = TestClient(app)

    response = client.post("/parse", json={"files": [uploaded.name]})

    assert response.status_code == 400
    assert "exe" in response.json()["detail"]


def test_text_parse_reads_text(tmp_path: Path) -> None:
    text_path = tmp_path / "notes.txt"
    text_path.write_text("plain text review notes", encoding="utf-8")

    parsed = file_parser.parse_file(text_path)

    assert parsed.file_type == ".txt"
    assert parsed.pages[0].source == "text_extract"
    assert parsed.raw_text == "plain text review notes"


def test_markdown_parse_reads_text(tmp_path: Path) -> None:
    markdown_path = tmp_path / "demo_course_material.md"
    markdown_path.write_text("# Demo Course\n\nChapter 1 notes", encoding="utf-8")

    parsed = file_parser.parse_file(markdown_path)

    assert parsed.file_type == ".md"
    assert parsed.pages[0].source == "text_extract"
    assert "Chapter 1 notes" in parsed.raw_text


def test_image_parse_uses_ocr_provider(tmp_path: Path, monkeypatch) -> None:
    image_path = tmp_path / "scan.png"
    image_path.write_bytes(b"fake image bytes")

    def fake_ocr(path: Path, config: OCRConfig) -> str:
        assert path == image_path
        assert config.provider == "custom_api"
        assert config.api_key == "request-only-key"
        return "ocr text from image"

    monkeypatch.setattr(file_parser, "run_ocr_on_path", fake_ocr)

    parsed = file_parser.parse_file(
        image_path,
        OCRConfig(
            provider="custom_api",
            api_url="https://example.test/ocr",
            api_key="request-only-key",
        ),
    )

    assert parsed.pages[0].source == "ocr_fallback"
    assert parsed.raw_text == "ocr text from image"


def test_pdf_short_text_page_uses_ocr_fallback(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "scan.pdf"
    pdf_path.write_bytes(b"%PDF mock")

    class FakePdfPage:
        def extract_text(self) -> str:
            return "too short"

    class FakePdfReader:
        def __init__(self, _: str) -> None:
            self.pages = [FakePdfPage()]

    monkeypatch.setattr("pypdf.PdfReader", FakePdfReader)
    monkeypatch.setattr(
        file_parser,
        "render_pdf_page_to_image",
        lambda path, page_number: Image.new("RGB", (10, 10), "white"),
    )
    monkeypatch.setattr(
        file_parser,
        "run_ocr_on_image",
        lambda image, config: "ocr text from scanned pdf",
    )

    parsed = file_parser.parse_file(pdf_path, OCRConfig(provider="local_tesseract"))

    assert parsed.pages[0].page_number == 1
    assert parsed.pages[0].source == "ocr_fallback"
    assert parsed.raw_text == "ocr text from scanned pdf"


def test_windows_subprocess_kwargs_hide_console(monkeypatch) -> None:
    # Linux has no Win32 subprocess objects. Supply the platform API only on
    # those hosts; Windows runners still exercise the real STARTUPINFO class.
    import subprocess
    from types import SimpleNamespace

    if not hasattr(subprocess, "STARTUPINFO"):
        monkeypatch.setattr(subprocess, "STARTUPINFO", lambda: SimpleNamespace(dwFlags=0), raising=False)
        monkeypatch.setattr(subprocess, "STARTF_USESHOWWINDOW", 1, raising=False)
        monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr("app.services.subprocess_utils.sys.platform", "win32")

    kwargs = subprocess_no_window_kwargs()

    assert kwargs["creationflags"] != 0
    assert kwargs["startupinfo"].dwFlags != 0

def test_arabic_major_chapter_marker_survives_title_cleaning() -> None:
    title = detect_chapter_title(chr(31532) + "1" + chr(31456) + " " + chr(34892) + chr(21015) + chr(24335))
    assert title == chr(31532) + "1" + chr(31456) + " " + chr(34892) + chr(21015) + chr(24335)


def test_pdf_with_text_layer_skips_ocr(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "text-layer.pdf"
    pdf_path.write_bytes(b"%PDF mock")
    calls = {"ocr": 0}
    messages: list[str] = []

    class FakePdfPage:
        def extract_text(self) -> str:
            return "This is a text layer page with enough course material."

    class FakePdfReader:
        def __init__(self, _: str) -> None:
            self.pages = [FakePdfPage()]

    def fail_ocr(*args, **kwargs):
        calls["ocr"] += 1
        raise AssertionError("OCR should not run for text-layer PDFs")

    monkeypatch.setattr("pypdf.PdfReader", FakePdfReader)
    monkeypatch.setattr(file_parser, "run_ocr_on_image", fail_ocr)

    parsed = file_parser.parse_file(pdf_path, OCRConfig(provider="rapidocr"), lambda message, ratio: messages.append(message))

    assert parsed.pages[0].source == "text_extract"
    assert parsed.ocr_cache_used is False
    assert calls["ocr"] == 0
    assert any("跳过 OCR" in message for message in messages)


def test_native_pdf_text_with_legacy_marker_is_retained() -> None:
    text = "锟斤拷 线性代数 矩阵 行列式 向量空间 定义与性质。"

    assert native_pdf_text_is_reliable(text) is True


def test_scanned_pdf_uses_ocr_cache_on_second_parse(tmp_path: Path, monkeypatch) -> None:
    settings.ocr_cache_dir = tmp_path / "cache" / "ocr"
    pdf_path = tmp_path / "scan.pdf"
    pdf_path.write_bytes(b"%PDF mock")
    calls = {"ocr": 0}

    class FakePdfPage:
        def extract_text(self) -> str:
            return ""

    class FakePdfReader:
        def __init__(self, _: str) -> None:
            self.pages = [FakePdfPage()]

    def fake_ocr(image, config):
        calls["ocr"] += 1
        return "cached scanned text"

    monkeypatch.setattr("pypdf.PdfReader", FakePdfReader)
    monkeypatch.setattr(file_parser, "render_pdf_page_to_image", lambda path, page_number: Image.new("RGB", (10, 10), "white"))
    monkeypatch.setattr(file_parser, "run_ocr_on_image", fake_ocr)

    first = file_parser.parse_file(pdf_path, OCRConfig(provider="rapidocr"))
    second = file_parser.parse_file(pdf_path, OCRConfig(provider="rapidocr"))

    assert first.raw_text == "cached scanned text"
    assert second.raw_text == "cached scanned text"
    assert second.ocr_cache_used is True
    assert calls["ocr"] == 1


def test_bad_unit_titles_are_filtered() -> None:
    bad_titles = ["2 3,", "1 +1", "2 + V2", "0 =1%V9", "P(X≥k)", "未识别章节"]

    assert all(is_bad_unit_title(title) for title in bad_titles)
