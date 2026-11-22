import io
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mvp_app


SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">
<path d="M 10 10 L 90 10 L 90 90 L 10 90 Z"/>
</svg>"""


def make_png() -> bytes:
    image = Image.new("RGB", (32, 32), "white")
    for x in range(6, 26):
        for y in range(6, 26):
            image.putpixel((x, y), (0, 0, 0))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class MvpApiTests(unittest.TestCase):
    def setUp(self):
        mvp_app.app.config.update(TESTING=True)
        self.client = mvp_app.app.test_client()

    def send_to_fake_fluidnc(self, job_id, servo_calibrated=False):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        received = []
        failures = []

        def fake_fluidnc():
            try:
                connection, _ = server.accept()
                with connection:
                    first = connection.recv(1024)
                    if first != b"?":
                        raise AssertionError(f"Expected status query, got {first!r}")
                    connection.sendall(b"<Idle|MPos:0.000,0.000,4.000>\n")
                    buffer = bytearray()
                    while True:
                        chunk = connection.recv(1024)
                        if not chunk:
                            break
                        buffer.extend(chunk)
                        while b"\n" in buffer:
                            line, _, rest = buffer.partition(b"\n")
                            buffer = bytearray(rest)
                            received.append(line.decode("ascii"))
                            connection.sendall(b"ok\n")
            except Exception as error:
                failures.append(error)
            finally:
                server.close()

        worker = threading.Thread(target=fake_fluidnc, daemon=True)
        worker.start()
        payload = {
            "host": "fluidnc.local",
            "job_id": job_id,
            "confirm": True,
            "servo_calibrated": servo_calibrated,
        }
        with (
            mock.patch.object(mvp_app, "_resolve_fluidnc_host", return_value="127.0.0.1"),
            mock.patch.object(mvp_app, "FLUIDNC_PORT", port),
        ):
            response = self.client.post("/api/fluidnc/send", json=payload)
        worker.join(timeout=3)
        return response, received, failures

    def test_health_reports_local_pipeline(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["vectorizer"], "vtracer")
        self.assertEqual(response.json["gcode"], "svg2gcode_grbl")

    def test_api_http_errors_are_json(self):
        response = self.client.get("/api/unknown-endpoint")
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.json)

    def test_serves_frontend_and_assets(self):
        for path in ("/", "/app.js", "/style.css"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_vectorize_returns_svg_for_raster_image(self):
        response = self.client.post(
            "/api/vectorize",
            data={"file": (io.BytesIO(make_png()), "test.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIn("<svg", response.json["svg"])
        self.assertIn("<path", response.json["svg"])
        converted = self.client.post("/api/convert", json={"svg": response.json["svg"]})
        self.assertEqual(converted.status_code, 200, converted.json)
        self.assertGreater(converted.json["line_count"], 0)

    def test_raster_vectorization_keeps_edges_without_filled_regions(self):
        image = Image.new("RGB", (64, 64), "white")
        ImageDraw.Draw(image).rectangle((10, 10, 53, 53), fill="black")
        output = io.BytesIO()
        image.save(output, format="PNG")

        contour_data = mvp_app._prepare_contour_image(output.getvalue())

        with Image.open(io.BytesIO(contour_data)) as contours:
            self.assertEqual(contours.getpixel((32, 32)), 255)
            edge_width = sum(contours.getpixel((x, 32)) == 0 for x in range(7, 11))
            self.assertGreater(edge_width, 0)
            self.assertLessEqual(edge_width, 2)

    def test_vectorize_rejects_images_without_detectable_contours(self):
        image = Image.new("RGB", (32, 32), "white")
        output = io.BytesIO()
        image.save(output, format="PNG")

        response = self.client.post(
            "/api/vectorize",
            data={"file": (io.BytesIO(output.getvalue()), "blank.png")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("КОНТУРЫ", response.json["error"])

    def test_vectorize_rejects_non_image(self):
        response = self.client.post(
            "/api/vectorize",
            data={"file": (io.BytesIO(b"not an image"), "test.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json)

    def test_convert_generates_bounded_pen_gcode(self):
        response = self.client.post("/api/convert", json={"svg": SVG})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIn("G21", response.json["gcode"])
        self.assertIn("G90", response.json["gcode"])
        self.assertIn("G1 X", response.json["gcode"])
        self.assertIn("Z0.000", response.json["gcode"])
        self.assertTrue(response.json["job_id"])

    def test_convert_handles_paths_with_many_segments(self):
        points = " ".join(f"L {index % 100} {(index * 7) % 100}" for index in range(1100))
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" '
            f'viewBox="0 0 100 100"><path d="M 0 0 C 0 100 100 100 100 0 {points}"/></svg>'
        )

        response = self.client.post("/api/convert", json={"svg": svg})

        self.assertEqual(response.status_code, 200, response.json)
        self.assertGreater(response.json["line_count"], 1100)

    def test_convert_generates_configurable_servo_gcode(self):
        response = self.client.post(
            "/api/convert",
            json={
                "svg": SVG,
                "pen_mode": "servo",
                "bed_width_mm": 300,
                "bed_height_mm": 350,
                "speed_mm_s": 5,
                "servo_up_s": 500,
                "servo_down_s": 750,
                "servo_delay_ms": 300,
            },
        )
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["pen_mode"], "servo")
        self.assertIn("F300", response.json["gcode"])
        self.assertIn("M3 S500", response.json["gcode"])
        self.assertIn("M3 S750", response.json["gcode"])
        self.assertIn("G4 P0.300", response.json["gcode"])
        self.assertNotIn(" Z", response.json["gcode"])
        self.assertNotIn("\nG0 ", response.json["gcode"])

    def test_convert_returns_json_when_converter_crashes(self):
        with mock.patch.object(mvp_app.svg_converter, "main", side_effect=RuntimeError("converter failure")):
            response = self.client.post("/api/convert", json={"svg": SVG})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json["error"], "ВНУТРЕННЯЯ ОШИБКА СЕРВЕРА. ПРОВЕРЬТЕ ЖУРНАЛ BACKEND.")

    def test_convert_rejects_svg_with_external_reference(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"><image href="https://example.com/image.png"/></svg>'
        response = self.client.post("/api/convert", json={"svg": svg})
        self.assertEqual(response.status_code, 400)

    def test_convert_rejects_mixed_svg_references(self):
        svg = """<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1">
        <path style="fill:url(#local);stroke:url(https://example.com/style)"/>
        </svg>"""
        response = self.client.post("/api/convert", json={"svg": svg})
        self.assertEqual(response.status_code, 400)

    def test_ai_generation_requires_external_data_consent(self):
        with mock.patch.object(mvp_app, "Client") as client_class:
            response = self.client.post("/api/generate-image", json={"prompt": "tree"})
        self.assertEqual(response.status_code, 400)
        client_class.assert_not_called()

    def test_ai_generation_requires_and_uses_explicit_consent(self):
        generated = mock.Mock()
        generated.images.generate.return_value = {
            "data": [{"url": "https://images.example.com/generated.png"}]
        }
        with (
            mock.patch.object(mvp_app, "Client", return_value=generated),
            mock.patch.object(mvp_app, "_download_generated_image", return_value=make_png()) as download,
        ):
            response = self.client.post(
                "/api/generate-image",
                json={"prompt": "tree", "allow_external": True},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/png")
        generated.images.generate.assert_called_once_with(
            prompt=f"tree\n\n{mvp_app.PLOTTER_IMAGE_STYLE}",
            model="flux",
            response_format="url",
        )
        download.assert_called_once_with("https://images.example.com/generated.png")

    def test_fluidnc_rejects_public_hosts(self):
        response = self.client.post("/api/fluidnc/status", json={"host": "8.8.8.8"})
        self.assertEqual(response.status_code, 400)

    def test_fluidnc_send_requires_confirmation(self):
        converted = self.client.post("/api/convert", json={"svg": SVG})
        with mock.patch.object(mvp_app, "_resolve_fluidnc_host") as resolve_host:
            response = self.client.post(
                "/api/fluidnc/send",
                json={"host": "192.168.1.10", "job_id": converted.json["job_id"]},
            )
        self.assertEqual(response.status_code, 400)
        resolve_host.assert_not_called()

    def test_fluidnc_servo_send_requires_calibration(self):
        converted = self.client.post("/api/convert", json={"svg": SVG, "pen_mode": "servo"})
        with mock.patch.object(mvp_app, "_resolve_fluidnc_host") as resolve_host:
            response = self.client.post(
                "/api/fluidnc/send",
                json={
                    "host": "192.168.1.10",
                    "job_id": converted.json["job_id"],
                    "confirm": True,
                },
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("КАЛИБРОВКУ СЕРВО", response.json["error"])
        resolve_host.assert_not_called()

    def test_fluidnc_sends_only_a_ready_job_to_idle_controller(self):
        converted = self.client.post("/api/convert", json={"svg": SVG})
        job_id = converted.json["job_id"]
        response, received, failures = self.send_to_fake_fluidnc(job_id)

        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json["sent"])
        self.assertTrue(received)
        self.assertFalse(failures, failures)

        duplicate = self.client.post(
            "/api/fluidnc/send",
            json={"host": "fluidnc.local", "job_id": job_id, "confirm": True},
        )
        self.assertEqual(duplicate.status_code, 409)

    def test_fluidnc_sends_calibrated_servo_gcode(self):
        converted = self.client.post("/api/convert", json={"svg": SVG, "pen_mode": "servo"})
        response, received, failures = self.send_to_fake_fluidnc(
            converted.json["job_id"], servo_calibrated=True
        )
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIn("M3 S700", received)
        self.assertIn("M3 S800", received)
        self.assertIn("G4 P0.3", received)
        self.assertFalse(failures, failures)


if __name__ == "__main__":
    unittest.main()
