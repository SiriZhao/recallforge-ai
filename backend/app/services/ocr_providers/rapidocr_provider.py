from PIL import Image
from functools import lru_cache
from threading import RLock

from app.schemas.review import OCRConfig
from app.services.ocr_providers.base import BaseOCRProvider
from app.services.ocr_providers.utils import image_to_bytes

_inference_lock = RLock()


@lru_cache(maxsize=1)
def rapidocr_engine():
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR()


class RapidOCROCRProvider(BaseOCRProvider):
    name = "rapidocr"

    def extract_text(self, image: Image.Image, config: OCRConfig) -> str:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise RuntimeError(
                "RapidOCR 未安装。请运行 pip install -r backend/requirements.txt。"
            ) from exc

        with _inference_lock:
            result, _ = rapidocr_engine()(image_to_bytes(image))
        if not result:
            return ""

        lines = [item[1].strip() for item in result if len(item) >= 2 and item[1].strip()]
        return "\n".join(lines)
