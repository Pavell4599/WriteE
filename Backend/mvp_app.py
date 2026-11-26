from __future__ import annotations

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
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory
from PIL import Image, UnidentifiedImageError
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

import image_generation
import inkscape_trace


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "Frontend"
BACKEND_DIR = Path(__file__).resolve().parent
CONVERTER_DIR = BACKEND_DIR / "svg2gcode_grbl"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_TRACE_SIDE = 1200
MAX_SVG_BYTES = 5 * 1024 * 1024
MAX_GCODE_BYTES = 5 * 1024 * 1024
MAX_GCODE_LINES = 50_000
JOB_TTL_SECONDS = 60 * 60
FLUIDNC_PORT = 23

sys.path.insert(0, str(BACKEND_DIR))
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
    return jsonify(status="ok", vectorizer="inkscape-potrace", gcode="svg2gcode_grbl")


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


def _prepare_contour_image(data: bytes, mode: str = "auto") -> bytes:
    """Intermediate Inkscape-style BW bitmap used by tests and debugging."""
    try:
        return inkscape_trace.prepare_debug_bitmap(data, MAX_TRACE_SIDE, mode)
    except ValueError as error:
        raise ApiError("НА ИЗОБРАЖЕНИИ НЕ НАЙДЕНЫ КОНТУРЫ.", 422) from error


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

    trace_mode = request.form.get("mode", "auto")
    if trace_mode not in inkscape_trace.VALID_MODES:
        raise ApiError("НЕИЗВЕСТНЫЙ РЕЖИМ ОБВОДКИ.")

    threshold_raw = request.form.get("threshold")
    threshold = None
    if threshold_raw:
        try:
            threshold = float(threshold_raw)
        except ValueError:
            raise ApiError("ПОРОГ ОБВОДКИ ДОЛЖЕН БЫТЬ ЧИСЛОМ.")
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ApiError("ПОРОГ ОБВОДКИ ДОЛЖЕН БЫТЬ ОТ 0 ДО 1.")

    if filename.lower().endswith(".svg") or uploaded.mimetype == "image/svg+xml":
        _validate_svg(data)
        svg = data.decode("utf-8-sig")
    else:
        _validate_image(data)
        try:
            svg = inkscape_trace.raster_to_svg(data, MAX_TRACE_SIDE, trace_mode, threshold)
        except ValueError as error:
            message = str(error).lower()
            if "contour" in message or "small" in message:
                raise ApiError("НА ИЗОБРАЖЕНИИ НЕ НАЙДЕНЫ КОНТУРЫ.", 422) from error
            logger.exception("Inkscape-style Potrace failed")
            raise ApiError("НЕ УДАЛОСЬ ПРЕОБРАЗОВАТЬ ИЗОБРАЖЕНИЕ В SVG.", 422) from error
        except Exception as error:
            logger.exception("Inkscape-style Potrace failed")
            raise ApiError("НЕ УДАЛОСЬ ПРЕОБРАЗОВАТЬ ИЗОБРАЖЕНИЕ В SVG.", 422) from error
        _validate_svg(svg.encode("utf-8"))

    root = ET.fromstring(svg)
    path_count = sum(1 for element in root.iter() if element.tag.rsplit("}", 1)[-1].lower() == "path")
    size = root.get("viewBox") or root.get("width", "0")
    return jsonify(svg=svg, filename=f"{Path(filename).stem or 'drawing'}.svg", path_count=path_count, trace_mode=trace_mode, threshold=threshold, source_size=size)


@app.post("/api/generate-image")
def generate_image():
    body = _json_body()
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ApiError("ВВЕДИТЕ ОПИСАНИЕ ИЗОБРАЖЕНИЯ.")
    if len(prompt) > image_generation.MAX_PROMPT_CHARS:
        raise ApiError("ОПИСАНИЕ ДОЛЖНО БЫТЬ КОРОЧЕ 2000 СИМВОЛОВ.")
    if body.get("allow_external") is not True:
        raise ApiError("ПОДТВЕРДИТЕ ОТПРАВКУ ТЕКСТА ВНЕШНЕМУ AI.")

    try:
        image = image_generation.generate_plotter_image(prompt)
    except image_generation.ImageGenerationError as error:
        response = jsonify(error=str(error))
        if error.retry_after:
            response.headers["Retry-After"] = str(error.retry_after)
        return response, error.status
    except Exception as error:
        logger.exception("Image generation failed")
        raise ApiError("AI НЕ СМОГ СОЗДАТЬ ИЗОБРАЖЕНИЕ. ПОПРОБУЙТЕ ПОЗЖЕ.", 502) from error

    return app.response_class(image, mimetype="image/png")


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


def _gcode_metrics(gcode: str, bed_width: float, bed_height: float, speed_mm_s: float, pen_mode: str = "z", servo_down_s: int = 800) -> dict[str, Any]:
    x = y = z = 0.0
    drawing_mm = 0.0
    travel_mm = 0.0
    drawing_points = 0
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    pen_down = False

    for original in gcode.splitlines():
        line = original.split(";", 1)[0].strip().upper()
        match = re.fullmatch(r"(G0|G1)(?:\s+([XYZF][-+]?(?:\d+(?:\.\d*)?|\.\d+)))*", line)
        if not match:
            if line.startswith("M3 S"):
                try:
                    servo = int(line.split("S", 1)[1])
                    pen_down = servo == (servo_down_s if pen_mode == "servo" else 800)
                except (ValueError, IndexError):
                    pass
            continue
        values = re.findall(r"([XYZ])([-+]?(?:\d+(?:\.\d*)?|\.\d+))", line)
        next_x, next_y, next_z = x, y, z
        for axis, raw in values:
            if axis == "X": next_x = float(raw)
            elif axis == "Y": next_y = float(raw)
            elif axis == "Z": next_z = float(raw)
        if next_z != z:
            pen_down = next_z <= 0.5
        length = math.hypot(next_x - x, next_y - y)
        if pen_down:
            drawing_mm += length
            drawing_points += 1 if length > 0 else 0
            min_x = min(min_x, x, next_x)
            min_y = min(min_y, y, next_y)
            max_x = max(max_x, x, next_x)
            max_y = max(max_y, y, next_y)
        else:
            travel_mm += length
        x, y, z = next_x, next_y, next_z

    if not math.isfinite(min_x):
        min_x = min_y = max_x = max_y = 0.0
    width = max(0.0, max_x - min_x)
    height = max(0.0, max_y - min_y)
    estimated_seconds = (drawing_mm + travel_mm) / max(speed_mm_s, 0.1) * 1.15
    outside = max_x > bed_width or max_y > bed_height or min_x < 0 or min_y < 0
    return {
        "drawing_length_mm": round(drawing_mm, 1),
        "travel_length_mm": round(travel_mm, 1),
        "estimated_seconds": round(estimated_seconds),
        "bounds_mm": {"x": round(min_x, 1), "y": round(min_y, 1), "width": round(width, 1), "height": round(height, 1)},
        "drawing_moves": drawing_points,
        "outside_workspace": outside,
        "warnings": (["РИСУНОК ВЫХОДИТ ЗА ПРЕДЕЛЫ РАБОЧЕГО ПОЛЯ."] if outside else []),
    }


@app.post("/api/preflight")
def preflight():
    body = _json_body()
    svg_value = body.get("svg")
    if not isinstance(svg_value, str):
        raise ApiError("SVG НЕ ПЕРЕДАН.")
    _validate_svg(svg_value.encode("utf-8"))
    bed_width = _number(body, "bed_width_mm", 300, 1, 2000)
    bed_height = _number(body, "bed_height_mm", 350, 1, 2000)
    speed_mm_s = _number(body, "speed_mm_s", 5, 1, 100)

    preview_body = dict(body)
    preview_body.setdefault("pen_mode", "z")
    pen_mode = preview_body["pen_mode"]
    if pen_mode not in {"z", "servo"}:
        raise ApiError("НЕИЗВЕСТНЫЙ СПОСОБ ПОДЪЁМА ПЕРА.")

    with tempfile.TemporaryDirectory(prefix="writee-preflight-") as directory:
        source = Path(directory) / "source.svg"
        destination = Path(directory) / "preview.gcode"
        source.write_text(svg_value, encoding="utf-8")
        converter_values = {
            "bed_max_x": bed_width, "bed_max_y": bed_height, "units": "mm",
            "auto_scale": True, "optimise": False, "feed_rate": round(speed_mm_s * 60),
            "zTravel": _number(preview_body, "z_up_mm", 4, -50, 50),
            "zDraw": _number(preview_body, "z_down_mm", 0, -50, 50),
            "placement_x": _number(preview_body, "offset_x_percent", 50, 0, 100) / 100,
            "placement_y": _number(preview_body, "offset_y_percent", 50, 0, 100) / 100,
            "pen_lift_mode": pen_mode,
            "servo_up_s": _integer(preview_body, "servo_up_s", 700, 1, 10000),
            "servo_down_s": _integer(preview_body, "servo_down_s", 800, 1, 10000),
            "servo_delay_ms": _integer(preview_body, "servo_delay_ms", 300, 0, 2000),
        }
        with converter_lock:
            previous = {key: getattr(svg_converter, key) for key in converter_values}
            try:
                for key, value in converter_values.items(): setattr(svg_converter, key, value)
                svg_converter.main(str(source), str(destination))
            finally:
                for key, value in previous.items(): setattr(svg_converter, key, value)
        gcode = destination.read_text(encoding="utf-8")
    job = GcodeJob(gcode, bed_width, bed_height, converter_values["zTravel"], converter_values["zDraw"], time.time(), pen_mode, converter_values["servo_up_s"], converter_values["servo_down_s"])
    commands = _validate_gcode(gcode, job)
    metrics = _gcode_metrics(gcode, bed_width, bed_height, speed_mm_s, pen_mode, converter_values["servo_down_s"])
    metrics["line_count"] = len(commands)
    return jsonify(metrics)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
