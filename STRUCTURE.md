# WriteE MVP

## Project layout

```text
Frontend/
  index.html                Three-step UI: input, SVG setup, plotter
  style.css                 Samsung One UI-inspired styles
  app.js                    Wizard state and same-origin API calls

Backend/
  mvp_app.py                Flask API, image pipeline, G-code jobs, FluidNC sender
  tests/
    test_mvp_app.py         API, conversion, and simulated-controller tests
  svg2gcode_grbl/
    svg2gcode.py            Existing SVG-to-G-code converter; adjusted for the API
    config.py               GRBL converter defaults
    header.txt              Converter header comments

Stack/
  bambu-cuts/               Separate, unused Bambu plotter/cutter project
  PrintPointPlotter/        Separate printer files, guide, and G-code
  Notes_plans/              Planning, research, CAD, and study materials
  ...                       Other unused drafts and legacy code

requirements.txt            Dependencies for the active MVP
STRUCTURE.md                This guide and the source/attribution record
```

The `Stack` folder is an archive: its projects and notes are not imported by
the MVP and are not included in the shared requirements file.

## Data flow

1. The browser uploads an image, or the user enters a prompt. Additional prompt
   attachments stay in the browser; they are not sent to g4f.
2. Raster images are oriented from EXIF, resized to at most 1200 pixels on the
   longest side, smoothed with OpenCV's edge-preserving filter, and passed
   through Canny edge detection before VTracer converts the contours to
   monochrome SVG. This favors outlines over filled photo regions; fine texture
   is intentionally simplified. SVG uploads pass through SVG validation
   unchanged. For prompt-only input, the user must explicitly consent before
   g4f sends the plotter-oriented prompt to an external provider.
3. The settings screen sends the SVG, bed, placement, speed, and pen-lift
   settings to `POST /api/convert`. The existing GRBL converter produces the
   preview and an in-memory job ID. Pen lift can use either a Z axis or a
   FluidNC PWM servo controlled with `M3 S` and short `G4` settling delays.
4. The plotter screen checks the FluidNC status, then sends only that generated
   job after an explicit confirmation. The server requires an `Idle` status and
   waits for a GRBL `ok` response after each line. It does not accept arbitrary
   G-code for sending.

The FluidNC sender uses its default Telnet TCP port `23` and the GRBL real-time
status query. FluidNC can be configured to use another Telnet port; this MVP
uses the default. The backend binds to `127.0.0.1`, accepts only private LAN
IPv4 addresses or `.local` names, and never attempts to connect automatically.

## Run locally

From the repository root in PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe Backend\mvp_app.py
```

Open `http://127.0.0.1:5000`. To run the automated checks:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s Backend\tests -v
```

The app does not discover or connect to a machine automatically. Enter the
FluidNC LAN address in the last step. The initial TinyBee profile uses the
user-reported 300×350 mm work area and a conservative 5 mm/s feed rate.
Servo mode starts at `S700` up / `S800` draw with a 300 ms settling delay.
These PWM values are editable commissioning starting points, not calibrated
servo angles; they assume FluidNC's default linear PWM `speed_map`. Configure
the correct PWM output pin and 50 Hz spindle PWM in FluidNC, then test both
positions without the pen attached. Servo-mode travel uses the same slow
`G1` feed as drawing instead of the controller's unbounded rapid rate, and the
job ends without an automatic move to machine origin. The UI and API block
servo job transmission until calibration is explicitly confirmed.
Home the machine and verify coordinates, work-area limits, servo direction,
feed rate, and pen contact before plotting. The FluidNC sender is tested
against a local protocol simulator, not physical hardware.

## Components and source material

### Raster image to SVG: VTracer

Integrated the `vtracer` Python package (`1.0.0a4`, currently published as an
alpha prerelease) rather than copying its source. VTracer is a Rust-backed
raster-to-vector converter with a Python API, a monochrome preset suitable for
a single-pen plot, and Windows wheels. This makes it directly callable from
the local Flask backend.

- Project: <https://github.com/visioncortex/vtracer>
- Python package/API: <https://pypi.org/project/vtracer/1.0.0a4/>
- License declared by the package: MIT OR Apache-2.0.

DrawingBotV3 was considered because it is specifically oriented toward
stylized pen-plotter art. Its repository describes a desktop application with
image import and SVG export; it does not provide the small server-side Python
API needed by this MVP. It remains a useful manual art tool, but is not bundled
or automated here.

- Project: <https://github.com/SonarSonic/DrawingBotV3>
- Documentation: <https://drawingbotv3.readthedocs.io/>

### SVG to G-code: existing `svg2gcode_grbl`

The project already contained `Backend/svg2gcode_grbl`. The MVP reuses that
code and preserves its `LICENSE` and `header.txt`. The converter was adjusted
to work from any current directory, support SVGs without an XML namespace,
scale paths into the configured bed, apply placement, emit millimeter/absolute
mode, and raise conversion errors rather than exiting the server process.
Pen-up and pen-down moves use the repository's existing Z-axis model.

- Upstream named in the repository README:
  <https://github.com/arcadeperfect/svg2gcode_grbl>
- License in the vendored converter directory: GNU GPL v3.

Legacy text-to-SVG code and the old frontend stack notes are kept under
`Stack/` and are not part of this pipeline.

### Prompt-to-image: g4f

The MVP uses the installed g4f image client with the `flux` model name. The
prompt is sent only after the user checks the external-AI consent box and is
augmented with a clean line-art style suitable for a single-pen plotter. g4f is
asked to return the original image URL so it does not substitute a local media
cache path; the backend validates and downloads that image. Provider
availability is not guaranteed by g4f; failures are returned in the UI rather
than replaced by a fake image.

- Project: <https://github.com/xtekky/gpt4free>
- Python package: <https://pypi.org/project/g4f/6.8.0/>
- License declared by the package: GNU GPL v3.

### Wireless plotting: FluidNC

FluidNC documents Grbl-compatible G-code sender behavior. Its source defines
the default Telnet server port as `23`; this MVP uses that raw TCP command
channel and does not copy FluidNC firmware code.

- Project and protocol overview: <https://github.com/bdring/FluidNC>
- Telnet server source: <https://github.com/bdring/FluidNC/blob/main/FluidNC/src/WebUI/TelnetServer.h>
- Telnet client/GRBL channel source:
  <https://github.com/bdring/FluidNC/blob/main/FluidNC/src/WebUI/TelnetClient.cpp>
- PWM spindle source and its default 0–10000 to 0–100% speed map:
  <https://github.com/bdring/FluidNC/blob/main/FluidNC/src/Spindles/PWMSpindle.cpp>

The converter and g4f are GPL-3 components. Before redistributing a combined
application, review their licenses and ensure the distribution follows the
applicable GPL obligations.
