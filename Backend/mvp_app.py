from __future__ import annotations

import base64
import binascii
import ipaddress
import logging
import math
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import vtracer
from flask import Flask, jsonify, request, send_from_directory
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from g4f.client import Client


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "Frontend"
CONVERTER_DIR = ROOT / "Backend" / "svg2gcode_grbl"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_TRACE_SIDE = 1200
MAX_SVG_BYTES = 5 * 1024 * 1024
MAX_GCODE_BYTES = 5 * 1024 * 1024
MAX_GCODE_LINES = 50_000
JOB_TTL_SECONDS = 60 * 60
FLUIDNC_PORT = 23
PLOTTER_IMAGE_STYLE = (
    "Create a clean black-and-white line drawing for a pen plotter. Use smooth, "
    "simple contour outlines on a pure white background. Avoid shading, gray "
    "tones, gradients, halftones, hatching, texture, large filled areas, and tiny details."
)

sys.path.insert(0, str(CONVERTER_DIR))
import svg2gcode as svg_converter  # noqa: E402


app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 1024 * 1024
logger = logging.getLogger(__name__)
converter_lock = threading.Lock()
jobs_lock = threading.Lock()


@dataclass
class GcodeJob:
    gcode: str
    bed_width_mm: float
    bed_height_mm: float
    z_up_mm: float
    z_down_mm: float
    created_at: float
    pen_mode: str = "z"
    servo_up_s: int = 700
    servo_down_s: int = 800
    state: str = "ready"


jobs: dict[str, GcodeJob] = {}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class FluidNCError(Exception):
    pass


@app.errorhandler(ApiError)
def handle_api_error(error: ApiError):
    return jsonify(error=str(error)), error.status


@app.errorhandler(RequestEntityTooLarge)
def handle_large_request(_error):
    return jsonify(error="ФАЙЛ СЛИШКОМ БОЛЬШОЙ. МАКСИМУМ 20 МБ."), 413


@app.errorhandler(HTTPException)
def handle_http_error(error: HTTPException):
    if request.path.startswith("/api/"):
        return jsonify(error=error.description), error.code
    return error


@app.errorhandler(Exception)
def handle_unexpected_error(error: Exception):
    logger.exception("Unhandled application error")
    if request.path.startswith("/api/"):
        return jsonify(error="ВНУТРЕННЯЯ ОШИБКА СЕРВЕРА. ПРОВЕРЬТЕ ЖУРНАЛ BACKEND."), 500
    return "ВНУТРЕННЯЯ ОШИБКА СЕРВЕРА.", 500


def _json_body() -> dict[str, Any]:
    if not request.is_json:
        raise ApiError("ОЖИДАЕТСЯ JSON-ЗАПРОС.")
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiError("ОЖИДАЕТСЯ JSON-ОБЪЕКТ.")
    return body


@app.get("/")
def index():
    return send_from_directory(FRONTEND, "index.html")


@app.get("/<path:asset>")
def frontend_asset(asset: str):
    if asset not in {"app.js", "style.css"}:
        return jsonify(error="ФАЙЛ НЕ НАЙДЕН."), 404
    return send_from_directory(FRONTEND, asset)


@app.get("/api/health")
def health():
    return jsonify(status="ok", vectorizer="vtracer", gcode="svg2gcode_grbl")


def _validate_image(data: bytes) -> tuple[str, int, int]:
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ApiError("ФАЙЛ ПУСТОЙ ИЛИ БОЛЬШЕ 20 МБ.")
    try:
        with Image.open(BytesIO(data)) as image:
            image_format = image.format
            width, height = image.size
            image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ApiError("НЕ УДАЛОСЬ ПРОЧИТАТЬ ИЗОБРАЖЕНИЕ.") from error
    if not image_format or width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
        raise ApiError("РАЗМЕР ИЗОБРАЖЕНИЯ ПРЕВЫШАЕТ ЛИМИТ.")
    return image_format, width, height


def _prepare_contour_image(data: bytes) -> bytes:
    with Image.open(BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source).convert("L")

    image.thumbnail((MAX_TRACE_SIDE, MAX_TRACE_SIDE), Image.Resampling.LANCZOS)
    width, height = image.size
    if width < 3 or height < 3:
        raise ApiError("НА ИЗОБРАЖЕНИИ НЕ НАЙДЕНЫ КОНТУРЫ.", 422)

    grayscale = np.frombuffer(image.tobytes(), dtype=np.uint8).reshape((height, width))
    grayscale = cv2.normalize(grayscale, None, 0, 255, cv2.NORM_MINMAX)
    grayscale = cv2.bilateralFilter(grayscale, 7, 50, 50)
    edges = cv2.Canny(grayscale, 40, 100, L2gradient=True)
    edges[[0, -1], :] = 0
    edges[:, [0, -1]] = 0
    if cv2.countNonZero(edges) == 0:
        raise ApiError("НА ИЗОБРАЖЕНИИ НЕ НАЙДЕНЫ КОНТУРЫ.", 422)

    contours = Image.fromarray(cv2.bitwise_not(edges))
    output = BytesIO()
    contours.save(output, format="PNG")
    return output.getvalue()


def _validate_svg(data: bytes) -> ET.Element:
    if not data or len(data) > MAX_SVG_BYTES:
        raise ApiError("SVG ПУСТОЙ ИЛИ БОЛЬШЕ 5 МБ.")
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ApiError("DTD И XML-СУЩНОСТИ В SVG НЕ ПОДДЕРЖИВАЮТСЯ.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise ApiError("НЕКОРРЕКТНЫЙ SVG.") from error
    if root.tag.rsplit("}", 1)[-1].lower() != "svg":
        raise ApiError("ФАЙЛ ДОЛЖЕН СОДЕРЖАТЬ SVG.")
    element_count = 0
    for element in root.iter():
        element_count += 1
        if element_count > 100_000:
            raise ApiError("В SVG СЛИШКОМ МНОГО ЭЛЕМЕНТОВ.")
        if element.tag.rsplit("}", 1)[-1].lower() in {"script", "style", "foreignobject", "iframe"}:
            raise ApiError("SVG СОДЕРЖИТ НЕПОДДЕРЖИВАЕМЫЙ ЭЛЕМЕНТ.")
        for key, value in element.attrib.items():
            attribute = key.rsplit("}", 1)[-1].lower()
            lowered_value = value.strip().lower()
            if attribute.startswith("on"):
                raise ApiError("SVG СОДЕРЖИТ НЕПОДДЕРЖИВАЕМЫЙ АТРИБУТ.")
            if attribute == "href" and lowered_value and not lowered_value.startswith("#"):
                raise ApiError("ВНЕШНИЕ ССЫЛКИ В SVG НЕ ПОДДЕРЖИВАЮТСЯ.")
            if "url(" in lowered_value:
                references = re.findall(r"url\(([^)]*)\)", lowered_value)
                if len(references) != lowered_value.count("url(") or any(
                    not reference.strip(" '\"").startswith("#") for reference in references
                ):
                    raise ApiError("ВНЕШНИЕ ССЫЛКИ В SVG НЕ ПОДДЕРЖИВАЮТСЯ.")
    return root


@app.post("/api/vectorize")
def vectorize():
    uploaded = request.files.get("file")
    if uploaded is None:
        raise ApiError("ДОБАВЬТЕ ИЗОБРАЖЕНИЕ.")
    data = uploaded.read(MAX_UPLOAD_BYTES + 1)
    filename = uploaded.filename or ""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ApiError("ФАЙЛ БОЛЬШЕ 20 МБ.", 413)

    if filename.lower().endswith(".svg") or uploaded.mimetype == "image/svg+xml":
        _validate_svg(data)
        svg = data.decode("utf-8-sig")
    else:
        _validate_image(data)
        contour_data = _prepare_contour_image(data)
        tracer = vtracer.Config.bw()
        tracer.filter_speckle = 4
        tracer.optimize = 2
        try:
            svg = tracer.convert_bytes(contour_data)
        except Exception as error:
            logger.exception("VTracer failed to process an image")
            raise ApiError("НЕ УДАЛОСЬ ПРЕОБРАЗОВАТЬ ИЗОБРАЖЕНИЕ В SVG.", 422) from error
        _validate_svg(svg.encode("utf-8"))

    return jsonify(svg=svg, filename=f"{Path(filename).stem or 'drawing'}.svg")


def _response_field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _validate_public_host(host: str) -> None:
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except OSError as error:
        raise ApiError("НЕ УДАЛОСЬ НАЙТИ СЕРВЕР ИЗОБРАЖЕНИЯ.", 502) from error
    if not addresses:
        raise ApiError("СЕРВЕР ИЗОБРАЖЕНИЯ НЕ НАЙДЕН.", 502)
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as error:
            raise ApiError("НЕКОРРЕКТНЫЙ АДРЕС СЕРВЕРА ИЗОБРАЖЕНИЯ.", 502) from error
        if not parsed.is_global:
            raise ApiError("ССЫЛКА НА ИЗОБРАЖЕНИЕ ДОЛЖНА ВЕСТИ В ПУБЛИЧНУЮ СЕТЬ.", 502)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _request, _response, _code, _message, _headers, _url):
        return None


def _download_generated_image(url: str) -> bytes:
    for _ in range(4):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ApiError("AI ВЕРНУЛ НЕКОРРЕКТНУЮ ССЫЛКУ НА ИЗОБРАЖЕНИЕ.", 502)
        _validate_public_host(parsed.hostname)
        request_image = urllib.request.Request(url, headers={"Accept": "image/*"})
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request_image, timeout=15) as response:
                if not response.headers.get("Content-Type", "").lower().startswith("image/"):
                    raise ApiError("AI ВЕРНУЛ НЕ ИЗОБРАЖЕНИЕ.", 502)
                data = response.read(MAX_UPLOAD_BYTES + 1)
                if len(data) > MAX_UPLOAD_BYTES:
                    raise ApiError("СГЕНЕРИРОВАННОЕ ИЗОБРАЖЕНИЕ БОЛЬШЕ 20 МБ.", 502)
                return data
        except urllib.error.HTTPError as error:
            location = error.headers.get("Location")
            if error.code not in {301, 302, 303, 307, 308} or not location:
                raise ApiError("НЕ УДАЛОСЬ ЗАГРУЗИТЬ СГЕНЕРИРОВАННОЕ ИЗОБРАЖЕНИЕ.", 502) from error
            url = urllib.parse.urljoin(url, location)
        except (urllib.error.URLError, TimeoutError) as error:
            raise ApiError("НЕ УДАЛОСЬ ЗАГРУЗИТЬ СГЕНЕРИРОВАННОЕ ИЗОБРАЖЕНИЕ.", 502) from error
    raise ApiError("СЛИШКОМ МНОГО ПЕРЕАДРЕСАЦИЙ ПРИ ЗАГРУЗКЕ ИЗОБРАЖЕНИЯ.", 502)


def _image_from_g4f(response: Any) -> bytes:
    items = _response_field(response, "data")
    if not items:
        raise ApiError("AI НЕ ВЕРНУЛ ИЗОБРАЖЕНИЕ.", 502)
    item = items[0]
    encoded = _response_field(item, "b64_json")
    if encoded:
        if encoded.startswith("data:"):
            encoded = encoded.split(",", 1)[-1]
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ApiError("AI ВЕРНУЛ ПОВРЕЖДЁННОЕ ИЗОБРАЖЕНИЕ.", 502) from error
    else:
        url = _response_field(item, "url")
        if not isinstance(url, str):
            raise ApiError("AI НЕ ВЕРНУЛ ССЫЛКУ ИЛИ ДАННЫЕ ИЗОБРАЖЕНИЯ.", 502)
        if url.startswith("data:image/"):
            try:
                data = base64.b64decode(url.split(",", 1)[1], validate=True)
            except (IndexError, binascii.Error, ValueError) as error:
                raise ApiError("AI ВЕРНУЛ ПОВРЕЖДЁННОЕ ИЗОБРАЖЕНИЕ.", 502) from error
        else:
            data = _download_generated_image(url)
    _validate_image(data)
    return data


def _normalize_png(data: bytes) -> bytes:
    _validate_image(data)
    with Image.open(BytesIO(data)) as image:
        output = BytesIO()
        image.convert("RGBA").save(output, format="PNG")
    return output.getvalue()


@app.post("/api/generate-image")
def generate_image():
    body = _json_body()
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ApiError("ВВЕДИТЕ ОПИСАНИЕ ИЗОБРАЖЕНИЯ.")
    if len(prompt) > 2_000:
        raise ApiError("ОПИСАНИЕ ДОЛЖНО БЫТЬ КОРОЧЕ 2000 СИМВОЛОВ.")
    if body.get("allow_external") is not True:
        raise ApiError("ПОДТВЕРДИТЕ ОТПРАВКУ ТЕКСТА ВНЕШНЕМУ AI.")

    try:
        result = Client().images.generate(
            prompt=f"{prompt.strip()}\n\n{PLOTTER_IMAGE_STYLE}",
            model="flux",
            response_format="url",
        )
        image = _image_from_g4f(result)
    except ApiError:
        raise
    except Exception as error:
        logger.exception("g4f image generation failed")
        raise ApiError("AI НЕ СМОГ СОЗДАТЬ ИЗОБРАЖЕНИЕ. ПОПРОБУЙТЕ ПОЗЖЕ.", 502) from error

    return app.response_class(_normalize_png(image), mimetype="image/png")


def _number(body: dict[str, Any], name: str, default: float, minimum: float, maximum: float) -> float:
    value = body.get(name, default)
    if isinstance(value, bool):
        raise ApiError(f"ПАРАМЕТР {name} ДОЛЖЕН БЫТЬ ЧИСЛОМ.")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ApiError(f"ПАРАМЕТР {name} ДОЛЖЕН БЫТЬ ЧИСЛОМ.") from error
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ApiError(f"ПАРАМЕТР {name} ВНЕ ДОПУСТИМОГО ДИАПАЗОНА.")
    return number


def _integer(body: dict[str, Any], name: str, default: int, minimum: int, maximum: int) -> int:
    value = _number(body, name, default, minimum, maximum)
    if not value.is_integer():
        raise ApiError(f"ПАРАМЕТР {name} ДОЛЖЕН БЫТЬ ЦЕЛЫМ ЧИСЛОМ.")
    return int(value)


def _validate_gcode(gcode: str, job: GcodeJob) -> list[str]:
    if not gcode or len(gcode.encode("utf-8")) > MAX_GCODE_BYTES:
        raise ApiError("G-CODE ПУСТОЙ ИЛИ СЛИШКОМ БОЛЬШОЙ.", 422)
    commands: list[str] = []
    servo_positions: set[int] = set()
    for original in gcode.splitlines():
        line = original.split(";", 1)[0]
        line = re.sub(r"\([^)]*\)", "", line).strip()
        if not line:
            continue
        if len(line) > 120:
            raise ApiError("СТРОКА G-CODE СЛИШКОМ ДЛИННАЯ.", 422)
        if line.upper() in {"G21", "G90"}:
            commands.append(line.upper())
            continue
        if re.fullmatch(r"F\d+(?:\.\d+)?", line, re.IGNORECASE):
            commands.append(line.upper())
            continue
        servo_match = re.fullmatch(r"M3\s+S(\d+)", line, re.IGNORECASE)
        if servo_match:
            servo_value = int(servo_match.group(1))
            if job.pen_mode != "servo" or servo_value not in {job.servo_up_s, job.servo_down_s}:
                raise ApiError("G-CODE СОДЕРЖИТ НЕРАЗРЕШЁННУЮ КОМАНДУ СЕРВО.", 422)
            servo_positions.add(servo_value)
            commands.append(f"M3 S{servo_value}")
            continue
        dwell_match = re.fullmatch(r"G4\s+P(\d+(?:\.\d+)?)", line, re.IGNORECASE)
        if dwell_match:
            dwell_seconds = float(dwell_match.group(1))
            if job.pen_mode != "servo" or not 0 <= dwell_seconds <= 2:
                raise ApiError("G-CODE СОДЕРЖИТ НЕДОПУСТИМУЮ ПАУЗУ.", 422)
            commands.append(f"G4 P{dwell_seconds:g}")
            continue
        match = re.fullmatch(r"(G0|G1)(?:\s+([XYZF][-+]?(?:\d+(?:\.\d*)?|\.\d+)))*", line, re.IGNORECASE)
        if not match:
            raise ApiError("G-CODE СОДЕРЖИТ НЕРАЗРЕШЁННУЮ КОМАНДУ.", 422)
        values = re.findall(r"([XYZF])([-+]?(?:\d+(?:\.\d*)?|\.\d+))", line, re.IGNORECASE)
        for axis, raw_value in values:
            value = float(raw_value)
            if axis.upper() == "X" and not 0 <= value <= job.bed_width_mm:
                raise ApiError("КООРДИНАТА X ВЫХОДИТ ЗА ПРЕДЕЛЫ РАБОЧЕГО ПОЛЯ.", 422)
            if axis.upper() == "Y" and not 0 <= value <= job.bed_height_mm:
                raise ApiError("КООРДИНАТА Y ВЫХОДИТ ЗА ПРЕДЕЛЫ РАБОЧЕГО ПОЛЯ.", 422)
            if axis.upper() == "Z" and (
                job.pen_mode != "z" or not job.z_down_mm <= value <= job.z_up_mm
            ):
                raise ApiError("КООРДИНАТА Z ВЫХОДИТ ЗА ПРЕДЕЛЫ ПЕРА.", 422)
        commands.append(line.upper())
    if not commands or not any(command.startswith(("G0 ", "G1 ")) for command in commands):
        raise ApiError("В G-CODE НЕТ КОМАНД ДВИЖЕНИЯ.", 422)
    if job.pen_mode == "servo" and not {job.servo_up_s, job.servo_down_s}.issubset(servo_positions):
        raise ApiError("В G-CODE НЕТ ОБЕИХ ПОЗИЦИЙ СЕРВО.", 422)
    if len(commands) > MAX_GCODE_LINES:
        raise ApiError("В G-CODE СЛИШКОМ МНОГО КОМАНД.", 422)
    return commands


def _store_job(
    gcode: str,
    bed_width: float,
    bed_height: float,
    z_up: float,
    z_down: float,
    pen_mode: str = "z",
    servo_up_s: int = 700,
    servo_down_s: int = 800,
) -> str:
    job_id = secrets.token_urlsafe(24)
    now = time.time()
    with jobs_lock:
        expired = [key for key, job in jobs.items() if now - job.created_at > JOB_TTL_SECONDS]
        for key in expired:
            del jobs[key]
        if len(jobs) >= 50:
            oldest = min(jobs, key=lambda key: jobs[key].created_at)
            del jobs[oldest]
        jobs[job_id] = GcodeJob(
            gcode, bed_width, bed_height, z_up, z_down, now, pen_mode, servo_up_s, servo_down_s
        )
    return job_id


@app.post("/api/convert")
def convert_svg():
    body = _json_body()
    svg_value = body.get("svg")
    if not isinstance(svg_value, str):
        raise ApiError("SVG НЕ ПЕРЕДАН.")
    svg_bytes = svg_value.encode("utf-8")
    _validate_svg(svg_bytes)

    pen_mode = body.get("pen_mode", "z")
    if not isinstance(pen_mode, str) or pen_mode not in {"z", "servo"}:
        raise ApiError("НЕИЗВЕСТНЫЙ СПОСОБ ПОДЪЁМА ПЕРА.")
    bed_width = _number(body, "bed_width_mm", 300, 1, 2000)
    bed_height = _number(body, "bed_height_mm", 350, 1, 2000)
    speed_mm_s = _number(body, "speed_mm_s", 5, 1, 100)
    z_up = _number(body, "z_up_mm", 4, -50, 50)
    z_down = _number(body, "z_down_mm", 0, -50, 50)
    placement_x = _number(body, "offset_x_percent", 50, 0, 100) / 100
    placement_y = _number(body, "offset_y_percent", 50, 0, 100) / 100
    servo_up_s = _integer(body, "servo_up_s", 700, 1, 10000)
    servo_down_s = _integer(body, "servo_down_s", 800, 1, 10000)
    servo_delay_ms = _integer(body, "servo_delay_ms", 300, 0, 2000)
    if z_up <= z_down:
        raise ApiError("ВЫСОТА ПЕРА ДОЛЖНА БЫТЬ ВЫШЕ ПОЛОЖЕНИЯ РИСОВАНИЯ.")
    if pen_mode == "servo" and servo_up_s == servo_down_s:
        raise ApiError("ПОЗИЦИИ ПОДЪЁМА И РИСОВАНИЯ СЕРВО ДОЛЖНЫ РАЗЛИЧАТЬСЯ.")

    try:
        with tempfile.TemporaryDirectory(prefix="writee-") as directory:
            source = Path(directory) / "source.svg"
            destination = Path(directory) / "drawing.gcode"
            source.write_bytes(svg_bytes)
            converter_values = {
                "bed_max_x": bed_width,
                "bed_max_y": bed_height,
                "units": "mm",
                "auto_scale": True,
                "optimise": False,
                "feed_rate": round(speed_mm_s * 60),
                "zTravel": z_up,
                "zDraw": z_down,
                "placement_x": placement_x,
                "placement_y": placement_y,
                "pen_lift_mode": pen_mode,
                "servo_up_s": servo_up_s,
                "servo_down_s": servo_down_s,
                "servo_delay_ms": servo_delay_ms,
            }
            with converter_lock:
                previous = {key: getattr(svg_converter, key) for key in converter_values}
                try:
                    for key, value in converter_values.items():
                        setattr(svg_converter, key, value)
                    svg_converter.main(str(source), str(destination))
                finally:
                    for key, value in previous.items():
                        setattr(svg_converter, key, value)
            gcode = destination.read_text(encoding="utf-8")
    except (OSError, ET.ParseError, ValueError, IndexError, ZeroDivisionError) as error:
        logger.exception("SVG to G-code conversion failed")
        raise ApiError("НЕ УДАЛОСЬ ПРЕОБРАЗОВАТЬ SVG В G-CODE.", 422) from error

    job = GcodeJob(
        gcode, bed_width, bed_height, z_up, z_down, time.time(), pen_mode, servo_up_s, servo_down_s
    )
    commands = _validate_gcode(gcode, job)
    job_id = _store_job(gcode, bed_width, bed_height, z_up, z_down, pen_mode, servo_up_s, servo_down_s)
    return jsonify(job_id=job_id, gcode=gcode, line_count=len(commands), pen_mode=pen_mode)


def _resolve_fluidnc_host(host: Any) -> str:
    if not isinstance(host, str) or not host.strip() or len(host) > 253:
        raise ApiError("ВВЕДИТЕ IP-АДРЕС ИЛИ ИМЯ FLUIDNC.")
    value = host.strip().rstrip(".")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if not value.lower().endswith(".local"):
            raise ApiError("ДОПУСКАЮТСЯ ТОЛЬКО ЛОКАЛЬНЫЕ IP-АДРЕСА И ИМЕНА .LOCAL.")
        try:
            addresses = socket.getaddrinfo(value, FLUIDNC_PORT, family=socket.AF_INET, type=socket.SOCK_STREAM)
        except OSError as error:
            raise ApiError("FLUIDNC НЕ НАЙДЕН В ЛОКАЛЬНОЙ СЕТИ.", 400) from error
        for entry in addresses:
            resolved = ipaddress.ip_address(entry[4][0])
            if resolved.is_private and not resolved.is_loopback and not resolved.is_link_local:
                return str(resolved)
        raise ApiError("АДРЕС FLUIDNC ДОЛЖЕН БЫТЬ В ЛОКАЛЬНОЙ СЕТИ.")
    if address.version != 4 or not address.is_private or address.is_loopback or address.is_link_local:
        raise ApiError("УКАЖИТЕ ЛОКАЛЬНЫЙ IPv4-АДРЕС FLUIDNC.")
    return str(address)


def _read_status(sock: socket.socket, timeout: float = 2.0) -> str:
    sock.sendall(b"?")
    deadline = time.monotonic() + timeout
    response = bytearray()
    while time.monotonic() < deadline:
        sock.settimeout(max(0.1, deadline - time.monotonic()))
        try:
            chunk = sock.recv(2048)
        except socket.timeout:
            break
        if not chunk:
            break
        response.extend(chunk)
        reports = re.findall(rb"<([^<>]+)>", response)
        if reports:
            return reports[-1].decode("ascii", errors="replace")
    raise FluidNCError("FLUIDNC НЕ ВЕРНУЛ СОСТОЯНИЕ МАШИНЫ.")


def _read_ack(sock: socket.socket, timeout: float = 10.0) -> bytes:
    deadline = time.monotonic() + timeout
    response = bytearray()
    while time.monotonic() < deadline:
        sock.settimeout(max(0.1, deadline - time.monotonic()))
        try:
            chunk = sock.recv(2048)
        except socket.timeout as error:
            raise FluidNCError("ТАЙМ-АУТ ОЖИДАНИЯ ПОДТВЕРЖДЕНИЯ FLUIDNC.") from error
        if not chunk:
            raise FluidNCError("FLUIDNC ЗАКРЫЛ СОЕДИНЕНИЕ.")
        response.extend(chunk)
        lines = response.decode("ascii", errors="replace").splitlines()
        for line in lines:
            normalized = line.strip()
            if normalized.lower() == "ok":
                return bytes(response)
            if normalized.lower().startswith(("error:", "alarm:")):
                raise FluidNCError(f"FLUIDNC ОТКЛОНИЛ КОМАНДУ: {normalized}")
    raise FluidNCError("ТАЙМ-АУТ ОЖИДАНИЯ ПОДТВЕРЖДЕНИЯ FLUIDNC.")


def _fluidnc_status(host: str) -> str:
    target = _resolve_fluidnc_host(host)
    try:
        with socket.create_connection((target, FLUIDNC_PORT), timeout=2.0) as connection:
            connection.settimeout(2.0)
            return _read_status(connection)
    except (OSError, FluidNCError) as error:
        raise ApiError(f"НЕ УДАЛОСЬ ПОДКЛЮЧИТЬСЯ К FLUIDNC: {error}", 502) from error


@app.post("/api/fluidnc/status")
def fluidnc_status():
    body = _json_body()
    report = _fluidnc_status(body.get("host"))
    state = report.split("|", 1)[0]
    return jsonify(connected=True, state=state, report=report)


@app.post("/api/fluidnc/send")
def fluidnc_send():
    body = _json_body()
    if body.get("confirm") is not True:
        raise ApiError("ПОДТВЕРДИТЕ ЗАПУСК ПЛОТТЕРА.")
    job_id = body.get("job_id")
    with jobs_lock:
        job = jobs.get(job_id) if isinstance(job_id, str) else None
        if job is None or time.time() - job.created_at > JOB_TTL_SECONDS:
            raise ApiError("ЗАДАНИЕ НЕ НАЙДЕНО. СОЗДАЙТЕ G-CODE ЗАНОВО.", 404)
        if job.pen_mode == "servo" and body.get("servo_calibrated") is not True:
            raise ApiError("ПОДТВЕРДИТЕ КАЛИБРОВКУ СЕРВО ПЕРЕД ОТПРАВКОЙ.", 409)
        if job.state != "ready":
            raise ApiError("ЭТО ЗАДАНИЕ УЖЕ ОТПРАВЛЯЛОСЬ ИЛИ НАХОДИТСЯ В РАБОТЕ.", 409)
        job.state = "sending"

    transmission_started = False
    try:
        target = _resolve_fluidnc_host(body.get("host"))
        commands = _validate_gcode(job.gcode, job)
        with socket.create_connection((target, FLUIDNC_PORT), timeout=2.0) as connection:
            connection.settimeout(2.0)
            report = _read_status(connection)
            state = report.split("|", 1)[0]
            if state != "Idle":
                raise ApiError(f"ПЛОТТЕР НЕ ГОТОВ К ЗАПУСКУ: {state}.", 409)
            try:
                for command in commands:
                    transmission_started = True
                    connection.sendall(command.encode("ascii") + b"\n")
                    _read_ack(connection)
            except (OSError, FluidNCError):
                try:
                    connection.sendall(b"!")
                except OSError:
                    pass
                raise
        with jobs_lock:
            job.state = "sent"
        return jsonify(sent=True, command_count=len(commands))
    except ApiError as error:
        with jobs_lock:
            job.state = "ready"
        raise error
    except (OSError, UnicodeEncodeError, FluidNCError) as error:
        with jobs_lock:
            job.state = "failed" if transmission_started else "ready"
        logger.exception("FluidNC G-code transmission failed")
        raise ApiError(f"ОТПРАВКА ОСТАНОВЛЕНА: {error}", 502) from error


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
