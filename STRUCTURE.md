# WriteE MVP

## Project layout

```text
Frontend/
  index.html                Three-step UI: input, SVG setup, plotter
  style.css                 Samsung One UI-inspired styles
  app.js                    Wizard state and same-origin API calls

Backend/
  mvp_app.py                Flask API, upload validation, G-code jobs, FluidNC sender
  image_generation.py       Prompt-to-image requests, retry rules, wordmark cleanup
  inkscape_trace.py         Inkscape-style Potrace bitmap-to-SVG tracing
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

1. The browser uploads an image, or the user enters a prompt. Prompt attachments
   stay in the browser; only the text prompt is sent to the external provider.
2. For prompt-only input, the user must explicitly consent before the backend
   sends the plotter-oriented prompt. The default provider is Pollinations'
   keyless classic endpoint, which allows roughly one anonymous image per IP
   every 45 seconds. Optional `POLLINATIONS_API_KEY` and `HF_TOKEN` environment
   variables enable the Pollinations API and Hugging Face Inference API paths.
   Only HTTP 200 image responses are accepted: 402/429 rate limits and 503 busy
   cards are reported as errors with `Retry-After` instead of being traced as
   artwork. The anonymous wordmark is removed with OpenCV inpainting.
3. Raster images are oriented from EXIF, have transparency flattened onto white,
   and are resized to at most 1200 pixels on the longest side. Potrace then
   traces them with Inkscape's single-scan defaults. `auto` chooses brightness
   cutoff for simple line art and edge detection for colourful or photographic
   input; `brightness` and `edge` can be selected manually. Every Potrace curve
   is emitted as its own `<path>` with `fill="none"`, so the G-code converter
   lifts the pen between contours instead of drawing connecting lines.
4. The settings screen sends the SVG, bed, placement, speed, and pen-lift
   settings to `POST /api/convert`. The existing GRBL converter produces the
   preview and an in-memory job ID. Pen lift can use either a Z axis or a
   FluidNC PWM servo controlled with `M3 S` and short `G4` settling delays.
5. The plotter screen checks the FluidNC status, then sends only that generated
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

### Raster image to SVG: Inkscape-style Potrace

Inkscape's Trace Bitmap is a UI around Peter Selinger's Potrace. To reproduce
that behavior on the server, the MVP calls the pure-Python `potracer` package
from `Backend/inkscape_trace.py`; no Inkscape installation or copied Inkscape
source is required. The two Inkscape single-scan filters are available:

- **Brightness cutoff** - dark pixels become black; best for logos, text, and
  clean line art.
- **Edge detection** - luminance edges become black lines; best for photos and
  gradients.

The Inkscape defaults are used: `turdsize=2`, minority turn policy,
`alphamax=1.0`, `opticurve=True`, and `opttolerance=0.2`. The `auto` mode
chooses between the filters from brightness/saturation statistics. SVG output
uses only simple `M`/`L`/`C`/`Z` commands because the vendored converter parses
classic Inkscape path data. Every Potrace curve is written as a separate
`<path>` with `fill="none"`: the converter treats one `<path>` as one continuous
polyline, so separate paths prevent unwanted pen-down lines between contours.

- Python package: <https://github.com/tatarize/potrace>
- License declared by `potracer==0.0.4`: GNU GPL v2 or later.

DrawingBotV3 was considered as a manual pen-plotter art tool, but it is a
desktop application without the small server-side Python API needed here. It is
not bundled or automated in the MVP.

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

### Prompt-to-image: Pollinations with optional hosted API keys

`Backend/image_generation.py` replaced g4f because its keyless image providers
now commonly require accounts, browser cookies, or shared Hugging Face ZeroGPU
quota. Pollinations' classic endpoint is the default keyless path:

- Anonymous endpoint: `https://image.pollinations.ai/prompt/{prompt}`
- Public rules: <https://github.com/pollinations/pollinations/blob/master/APIDOCS.md>

The public rules observed in November 2026 are one anonymous image per IP every
45 seconds, a 768×768 size limit, occasional `402`/`503` responses delivered as
JPEG cards, and a small `pollinations.ai` wordmark in the bottom-right corner.
The backend rejects those cards by accepting only HTTP 200 responses with an
`image/*` type, validates the decoded image, and removes the wordmark before
tracing. A deterministic seed derived from the prompt makes repeated clicks
reuse Pollinations' server-side cache.

With `POLLINATIONS_API_KEY`, the backend uses the documented gen endpoint
<https://gen.pollinations.ai/docs>. With `HF_TOKEN`, it can call the Hugging
Face Inference API for `black-forest-labs/FLUX.1-schnell`. Both keys are read
from the server environment only; they are never returned to the browser.
Provider failures are surfaced as Russian error messages and an optional
`Retry-After` header instead of a placeholder image.

The prompt is sent only after the user checks the external-AI consent box and
is augmented with a clean line-art style suitable for a single-pen plotter.
Prompt attachments stay in the browser. No image-generation client library is
bundled; only standard-library HTTPS requests are used.

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

The vendored converter is GPL-3 and `potracer` is GPLv2+. Pollinations and
Hugging Face are hosted services contacted over HTTPS; their client code is not
bundled. Before redistributing a combined application, review the applicable
GPL obligations for all bundled components.
