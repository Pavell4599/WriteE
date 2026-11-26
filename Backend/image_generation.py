"""Prompt-to-image generation for the single-pen plotter.

Why this module exists
----------------------
The first MVP sent prompts through ``g4f``. In practice its free image
providers now need an account (Bing/Designer cookies, a Hugging Face token) or
share the Hugging Face ZeroGPU quota, so prompt generation stopped working
without manual setup. The one keyless provider that still answers is
Pollinations' classic endpoint::

    https://image.pollinations.ai/prompt/{prompt}

Its public rules (checked in November 2026):

* one anonymous image per IP every 45 seconds;
* ``503`` with a "busy" card while the shared free queue is full;
* ``402`` with a "free limit reached" card when the IP is rate-limited;
* every anonymous image carries a small ``pollinations.ai`` wordmark in the
  bottom-right corner;
* error responses are JPEG cards, so a non-200 body must never be treated as
  artwork (otherwise the plotter would trace the "wait 45 seconds" picture).

This module therefore:

1. downloads only HTTP 200 responses with an ``image/*`` content type;
2. converts 402/429/503 into short Russian messages and ``Retry-After``
   seconds instead of blocking the server for minutes;
3. derives a stable seed from the prompt, so a retry or a repeated request
   reuses Pollinations' server-side cache instead of spending another free
   slot;
4. removes the anonymous wordmark with OpenCV inpainting before the image is
   traced;
5. uses a Pollinations API key (no wordmark, no per-IP limit) or a Hugging
   Face token (direct Inference API) when the environment provides one.

No provider is guaranteed. Failures are reported to the UI instead of being
replaced with a fake image.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, UnidentifiedImageError

logger = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 2_000
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MIN_IMAGE_SIDE = 64
MAX_IMAGE_PIXELS = 16_000_000
IMAGE_SIZE = 768
HTTP_TIMEOUT_SECONDS = 120
MAX_FREE_WAIT_SECONDS = 45

POLLINATIONS_CLASSIC_URL = "https://image.pollinations.ai/prompt/{prompt}"
POLLINATIONS_GEN_URL = "https://gen.pollinations.ai/image/{prompt}"
POLLINATIONS_MODEL = "black-forest-labs/flux.1-schnell"
HF_INFERENCE_URL = (
    "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
)
USER_AGENT = "WriteE/1.0 (local plotter MVP)"

# The anonymous wordmark sits in the bottom-right corner. Its exact size
# depends on the generated image, so a slightly larger rectangle is inpainted
# to guarantee that no part of the text survives.
WORDMARK_WIDTH_FRACTION = 0.30
WORDMARK_HEIGHT_FRACTION = 0.10

PLOTTER_IMAGE_STYLE = (
    "Draw this as clean black ink line art for a single-pen plotter. "
    "Use smooth continuous outlines, a pure white background, and generous "
    "white margins. No shading, gradients, halftones, hatching, texture, "
    "filled black areas, text, signatures, watermarks, logos, frames, or borders."
)


class ImageGenerationError(Exception):
    """A user-facing generation failure with an optional retry delay."""

    def __init__(self, message: str, status: int = 502, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def plotter_prompt(prompt: str) -> str:
    """Append the plotter style to the user's text."""
    return f"{prompt.strip()}\n\n{PLOTTER_IMAGE_STYLE}"


def stable_seed(prompt: str) -> int:
    """Return a deterministic seed for a prompt.

    Pollinations caches results by the complete request URL. Keeping the seed
    stable means that a second click for the same prompt returns the cached
    image instantly and does not consume the one-per-45-seconds free slot.
    """
    digest = hashlib.sha256(prompt.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % (2**31)


def _env_token(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _http_request(
    url: str,
    headers: dict[str, str],
    *,
    data: bytes | None = None,
    max_bytes: int = MAX_IMAGE_BYTES,
    timeout: int = HTTP_TIMEOUT_SECONDS,
) -> tuple[int, str, bytes, Any]:
    """Perform one HTTP request and return status/type/body/headers.

    HTTP errors are returned instead of raised so callers can inspect the
    ``Retry-After`` header and the JSON error body. Only transport failures
    raise :class:`ImageGenerationError`.
    """
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ImageGenerationError("СГЕНЕРИРОВАННОЕ ИЗОБРАЖЕНИЕ БОЛЬШЕ 20 МБ.", 502)
            return response.status, response.headers.get("Content-Type", ""), body, response.headers
    except urllib.error.HTTPError as error:
        body = error.read(64 * 1024)
        return error.code, error.headers.get("Content-Type", ""), body, error.headers
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ImageGenerationError(
            "НЕ УДАЛОСЬ СВЯЗАТЬСЯ С СЕРВИСОМ ГЕНЕРАЦИИ ИЗОБРАЖЕНИЙ.", 502
        ) from error


def _retry_after(headers: Any, body: bytes, fallback: int = 45) -> int:
    """Read Retry-After seconds from a header or a JSON/text error body."""
    header_value = headers.get("Retry-After") if headers is not None else None
    if isinstance(header_value, str) and header_value.strip().isdigit():
        return max(1, min(300, int(header_value.strip())))

    text = body.decode("utf-8", errors="replace")
    match = re.search(r"retry\s+after\s+(\d+)\s*s", text, re.IGNORECASE)
    if match:
        return max(1, min(300, int(match.group(1))))
    match = re.search(r'"retry_after"\s*:\s*(\d+)', text)
    if match:
        return max(1, min(300, int(match.group(1))))
    return fallback


def _error_detail(body: bytes) -> str:
    """Extract a short human-readable detail from a JSON error body."""
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        error = error.get("message")
    if isinstance(error, str):
        return " ".join(error.split())[:200]
    return ""


def _validate_image_bytes(data: bytes) -> None:
    if not data:
        raise ImageGenerationError("СЕРВИС ГЕНЕРАЦИИ ВЕРНУЛ ПУСТОЙ ОТВЕТ.", 502)
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageGenerationError("СГЕНЕРИРОВАННОЕ ИЗОБРАЖЕНИЕ БОЛЬШЕ 20 МБ.", 502)
    try:
        with Image.open(BytesIO(data)) as image:
            image_format = image.format
            width, height = image.size
            image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ImageGenerationError("СЕРВИС ГЕНЕРАЦИИ ВЕРНУЛ НЕ ИЗОБРАЖЕНИЕ.", 502) from error
    if image_format not in {"JPEG", "PNG", "WEBP"}:
        raise ImageGenerationError("СЕРВИС ГЕНЕРАЦИИ ВЕРНУЛ НЕПОДДЕРЖИВАЕМЫЙ ФОРМАТ.", 502)
    if width < MIN_IMAGE_SIDE or height < MIN_IMAGE_SIDE:
        raise ImageGenerationError("СГЕНЕРИРОВАННОЕ ИЗОБРАЖЕНИЕ СЛИШКОМ МАЛО.", 502)
    if width * height > MAX_IMAGE_PIXELS:
        raise ImageGenerationError("СГЕНЕРИРОВАННОЕ ИЗОБРАЖЕНИЕ СЛИШКОМ БОЛЬШОЕ.", 502)


def remove_classic_wordmark(data: bytes) -> bytes:
    """Erase the free Pollinations wordmark from the bottom-right corner.

    The anonymous API always draws a light ``pollinations.ai`` caption. If it
    is left in place, the tracer converts it into plotter strokes. OpenCV
    inpainting continues the surrounding artwork instead of leaving a hard
    white rectangle; if inpainting is unavailable, the area is filled white,
    which is correct for the requested line-art style.
    """
    with Image.open(BytesIO(data)) as source:
        image = source.convert("RGB")

    array = np.asarray(image)
    height, width = array.shape[:2]
    x0 = int(width * (1 - WORDMARK_WIDTH_FRACTION))
    y0 = int(height * (1 - WORDMARK_HEIGHT_FRACTION))
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[y0:height, x0:width] = 255

    try:
        cleaned = cv2.inpaint(array, mask, 3, cv2.INPAINT_TELEA)
        image = Image.fromarray(cleaned)
    except cv2.error:
        logger.warning("OpenCV inpainting failed; covering the wordmark with white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((x0, y0, width - 1, height - 1), fill="white")

    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _prepare_image(data: bytes, *, remove_wordmark: bool) -> bytes:
    _validate_image_bytes(data)
    if remove_wordmark:
        return remove_classic_wordmark(data)
    with Image.open(BytesIO(data)) as image:
        output = BytesIO()
        image.convert("RGB").save(output, format="PNG")
    return output.getvalue()


def _pollinations_url(prompt: str, api_key: str | None) -> str:
    encoded = urllib.parse.quote(prompt.strip().strip(". \n"), safe="")
    params = {
        "width": str(IMAGE_SIZE),
        "height": str(IMAGE_SIZE),
        "seed": str(stable_seed(prompt)),
        "safe": "false",
    }
    if api_key:
        params["model"] = POLLINATIONS_MODEL
        base = POLLINATIONS_GEN_URL.format(prompt=encoded)
    else:
        # The classic host still accepts nologo=true, but currently ignores
        # it; remove_classic_wordmark() is the actual cleanup step.
        params["nologo"] = "true"
        base = POLLINATIONS_CLASSIC_URL.format(prompt=encoded)
    return f"{base}?{urllib.parse.urlencode(params)}"


def _pollinations(prompt: str, api_key: str | None) -> bytes:
    """Generate through Pollinations, waiting once if the free slot is close."""
    url = _pollinations_url(prompt, api_key)
    headers = {"Accept": "image/*", "User-Agent": USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    for attempt in range(3):
        status, content_type, body, response_headers = _http_request(url, headers)

        if status == 200 and content_type.lower().startswith("image/"):
            _validate_image_bytes(body)
            return body

        retry_after = _retry_after(response_headers, body)
        detail = _error_detail(body)

        if status in {402, 429}:
            if attempt == 0 and retry_after <= MAX_FREE_WAIT_SECONDS:
                logger.info("Pollinations limit hit; waiting %ss before retry", retry_after)
                time.sleep(retry_after + 1)
                continue
            if api_key:
                message = (
                    "POLLINATIONS НЕ ПРИНЯЛ ЗАПРОС: ПРОВЕРЬТЕ БАЛАНС И КЛЮЧ "
                    f"POLLINATIONS_API_KEY. ПОВТОРИТЕ ЧЕРЕЗ {retry_after} СЕК."
                )
            else:
                message = (
                    "ЛИМИТ БЕСПЛАТНОЙ ГЕНЕРАЦИИ POLLINATIONS. "
                    f"ПОВТОРИТЕ ЧЕРЕЗ {retry_after} СЕК ИЛИ ЗАДАЙТЕ POLLINATIONS_API_KEY."
                )
            raise ImageGenerationError(message, 502, retry_after)

        if status in {500, 502, 503, 504}:
            if attempt < 2:
                wait = min(15, 3 * (attempt + 1))
                logger.info("Pollinations is busy (HTTP %s); retrying in %ss", status, wait)
                time.sleep(wait)
                continue
            raise ImageGenerationError(
                f"СЕРВИС ГЕНЕРАЦИИ ЗАНЯТ. ПОВТОРИТЕ ЧЕРЕЗ {retry_after or 15} СЕК.",
                502,
                retry_after or 15,
            )

        if status in {401, 403}:
            raise ImageGenerationError(
                "POLLINATIONS ОТКЛОНИЛ ЗАПРОС. ПРОВЕРЬТЕ POLLINATIONS_API_KEY.",
                502,
            )

        logger.warning("Pollinations HTTP %s: %s", status, detail or body[:200])
        raise ImageGenerationError(
            "СЕРВИС ГЕНЕРАЦИИ ВЕРНУЛ ОШИБКУ. ПОПРОБУЙТЕ ПОЗЖЕ.", 502
        )

    raise ImageGenerationError("НЕ УДАЛОСЬ СГЕНЕРИРОВАТЬ ИЗОБРАЖЕНИЕ. ПОПРОБУЙТЕ ПОЗЖЕ.", 502)


def _huggingface(prompt: str, token: str) -> bytes:
    """Optional fallback through the Hugging Face Inference API."""
    payload = json.dumps({"inputs": prompt}).encode("utf-8")
    headers = {
        "Accept": "image/*",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    status, content_type, body, response_headers = _http_request(
        HF_INFERENCE_URL, headers, data=payload
    )
    if status == 200 and content_type.lower().startswith("image/"):
        _validate_image_bytes(body)
        return body
    retry_after = _retry_after(response_headers, body, fallback=30)
    detail = _error_detail(body)
    if status in {401, 403}:
        raise ImageGenerationError("HUGGING FACE ОТКЛОНИЛ HF_TOKEN.", 502)
    if status == 404:
        raise ImageGenerationError("МОДЕЛЬ FLUX НЕДОСТУПНА ПО HF_TOKEN.", 502)
    if status in {402, 429}:
        raise ImageGenerationError(
            f"ЛИМИТ HUGGING FACE. ПОВТОРИТЕ ЧЕРЕЗ {retry_after} СЕК.", 502, retry_after
        )
    if status in {500, 502, 503, 504}:
        raise ImageGenerationError(
            f"HUGGING FACE ЗАНЯТ. ПОВТОРИТЕ ЧЕРЕЗ {retry_after} СЕК.", 502, retry_after
        )
    logger.warning("Hugging Face HTTP %s: %s", status, detail or body[:200])
    raise ImageGenerationError("HUGGING FACE НЕ СМОГ СОЗДАТЬ ИЗОБРАЖЕНИЕ.", 502)


def generate_plotter_image(prompt: str) -> bytes:
    """Return a PNG for the prompt, trying every configured provider.

    Order: Pollinations API key, Hugging Face token, anonymous Pollinations.
    The anonymous path is always tried because it is the only one that works
    out of the box. The most actionable error (with a retry delay) is raised
    when every provider fails.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ImageGenerationError("ВВЕДИТЕ ОПИСАНИЕ ИЗОБРАЖЕНИЯ.", 400)
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ImageGenerationError("ОПИСАНИЕ ДОЛЖНО БЫТЬ КОРОЧЕ 2000 СИМВОЛОВ.", 400)

    styled_prompt = plotter_prompt(prompt)
    errors: list[ImageGenerationError] = []

    api_key = _env_token("POLLINATIONS_API_KEY", "POLLINATIONS_KEY")
    if api_key:
        try:
            data = _pollinations(styled_prompt, api_key)
            return _prepare_image(data, remove_wordmark=False)
        except ImageGenerationError as error:
            errors.append(error)
            logger.warning("Pollinations key path failed: %s", error)

    hf_token = _env_token("HF_TOKEN", "HUGGINGFACE_HUB_TOKEN", "HUGGING_FACE_HUB_TOKEN")
    if hf_token:
        try:
            data = _huggingface(styled_prompt, hf_token)
            return _prepare_image(data, remove_wordmark=False)
        except ImageGenerationError as error:
            errors.append(error)
            logger.warning("Hugging Face path failed: %s", error)

    try:
        data = _pollinations(styled_prompt, None)
        return _prepare_image(data, remove_wordmark=True)
    except ImageGenerationError as error:
        errors.append(error)

    for error in errors:
        if error.retry_after:
            raise error
    raise errors[-1]
