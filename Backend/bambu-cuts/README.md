# Bambu Cuts - Cutter and Plotter for Bambu Lab Printers

Control your Bambu Lab 3D printer as a CNC cutter or plotter. Convert SVG/DXF files to G-code and execute them with a web-based control interface.


# WARNING

This can brake your printer, or you, or your cat, or all of the above. Especially if you put a knife on your printer. Using this software there will be no hard limits of the printer movements, make sure your gcode is reasonable. 


The origin of the print is at the current tool head position for X and Y. 
The Z=0 is set with the jogger, always make sure the Z is correctly zeroes. Otherwise it can plunge the knife into the print bed.

The printer needs to be in LAN mode with developer options enabled, or run an older firmware (1.0.4 worked for me)

## TLDR
   ```
   pip install git+https://github.com/unrelatedlabs/bambu-cuts.git
   bambucuts server
   ```

## Demo

![Plotter in Action](docs/plotter.gif)

*ploting on fabric*

![Cutter in Action](docs/cutter.gif)

*cutting a sticker with a drag knife*


![Cutter](docs/cutter_render.png)
![Pen Holder](docs/pen_holder_render.png)


*3D printed pen holder for plotting operations*


## Assembling the cutter 

![Cutter Assembly](docs/cutter_crosssection.jpeg)

Print in PETG! PLA tends to creep more. 

Parts: 
 - 2mm ID 6mm OD 3mm deep bearing 2x. https://amzn.to/46W4Ju7
 - roland style 2mm shaft blades https://amzn.to/4gY9w2Q 
 - 7mm OD, 20mm long spring https://amzn.to/4mQEISK
 - 1.7mm diameter 13mm long steel rod. (I cut a nail to size)
 - a small magnet to hold the blade. I've attached it on the clamp after the cutter is mounted on the printer. 


 The cutter assembly fits in the place of the hot end. Insert to a piece of filament to control the spring tension with the extruder.


## Features

![Web UI Screenshot](docs/gui.jpg)


- 🎮 **Web-based Control Interface** - Jog controls, G-code editor, and live monitoring
- ✂️ **SVG/DXF to G-code Conversion** - Convert vector graphics to cutting paths




![Cutter Cross-Section](docs/cutter crosssection.jpeg)

*Detailed cross-section view of the cutter mechanism*
- 🖥️ **CLI Tools** - Command-line utilities for batch processing


*3D printed pen holder for plotting operations*
- 🔄 **3MF Integration** - Automatically packages G-code for Bambu Lab printers

## Installation

### From Source

```bash
git clone git@github.com:unrelatedlabs/bambu-cuts.git
cd bambu-cuts
pip install -e .
```

## Quick Start

### Option 1: Run Without Installing (Development)

**Quick Start (Linux/Mac):**
```bash
./run.sh
```

**Manual Setup:**
```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the server directly
python -m bambucuts.webui.app
```

Open http://localhost:5425 in your browser.

### Option 2: Run with Docker

```bash
# Build the Docker image
docker build -t bambucuts .

# Run the container (interactive for first-time config)
docker run -it -p 5425:5425 bambucuts

# Or run with existing config (non-interactive)
docker run -p 5425:5425 -v ~/.bambucuts.conf:/root/.bambucuts.conf bambucuts
```

**Note:** Use `-it` flag on first run to interactively enter printer configuration. After configuration is saved, you can mount the config file with `-v` flag for subsequent runs.

Open http://localhost:5425 in your browser.

### Option 3: Install and Use CLI

After installation with `pip install -e .`:

```bash
bambucuts server
```

On first run, you'll be prompted for your printer configuration. Configuration is saved to `~/.bambucuts.conf`

Open http://localhost:5425 in your browser.

### 3. Convert SVG to G-code

```bash
bambucuts svg2gcode input.svg -o output.gcode
```

### 4. Convert DXF to SVG

```bash
bambucuts dxf2svg input.dxf -o output.svg
```

## CLI Commands

- `bambucuts server` - Start web interface
- `bambucuts svg2gcode INPUT` - Convert SVG to G-code
- `bambucuts dxf2svg INPUT` - Convert DXF to SVG

Run `bambucuts --help` for full options.

## My process 

- Create SVG in Inkscape
- create gcode in Kiri:Moto https://grid.space/kiri/  The machine settings for Kiri:Moto are at examples/kirimoto_settings.- I check my gcode with https://ncviewer.com
- In the webui set Z=0 with the jogger
- Move the print head to where the bottom left of the image is supposed to start.
- Load gcode in webui 
- Print 3mf


## Manual 3-point alignment

Instead of registering the job with a single point, you can probe **three corners
of the job's bounding box** on the real material. A 2D transform is then fitted
and applied to every XY coordinate, which compensates for translation, a rotated
sheet, scale error and (in `affine` mode) a non-square material.

Capture order — **P1 top right, P2 bottom right, P3 bottom left**. P3 is probed
last and stays the origin, so the existing `G92 X0 Y0` header and the normal
print workflow are unchanged: just park the head on P3 before printing.

1. Drop an SVG/DXF in the converter, `Home XY`, set `Z=0`.
2. Jog to the top right corner of the design on the material → `Capture` on P1.
3. Bottom right → `Capture` on P2. Bottom left → `Capture` on P3.
4. Pick a fit mode and press **Apply to G-code**. A report (rotation, scales,
   skew, measured vs. design size, squareness error, max residual) is shown and
   written into the file header as comments.
5. **Test Frame (no cut)** traces the bounding box at Z+5 so you can check the
   registration before the blade touches anything. Re-set `Z=0` afterwards.
6. `Go to` P3, then **Print 3MF**.

Fit modes: `affine` (6 DOF, exact through all three points, also corrects skew),
`similarity` (shift + rotation + one uniform scale), `rigid` (shift + rotation
only, keeps the SVG dimensions exactly).

The tracked head position is counted in software from the last `Home XY`, so use
the jog buttons for probing rather than typing `G1` into the manual G-code field.

Full documentation (in Russian): [ALIGNMENT_RU.md](ALIGNMENT_RU.md)


## SVG units

`svg-to-gcode` ignores the `viewBox`, so path coordinates arrive in SVG user
units rather than millimetres. `bambucuts/svg_units.py` computes the correct
`mm / user unit` factor from the root `width`/`height` and `viewBox`, and it is
applied once to the finished G-code. Together with the removal of the old
"stretch the drawing until it fills the viewBox" scaling in the path joiner, this
makes the output dimensionally correct: a 40 mm shape in a 100 mm Inkscape
document now comes out 40 mm wide, and the knife follows the SVG contour.


I use these cutting mats to hold the material https://amzn.to/3Ixn2xJ

## License

### Code License

This software is licensed under the **GNU General Public License v3.0 or later (GPL-3.0-or-later)**.

See [License.txt](License.txt) for the full license text.

You are free to use, modify, and distribute this software under the terms of the GPL-3.0 license.

### 3D Files License

All 3D model files (.3mf, .stl, .obj, etc.) in this repository are licensed under the **Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)**.

See [LICENSE-3D-FILES.txt](LICENSE-3D-FILES.txt) for the full license text.

- ✅ You may share and adapt the 3D files
- ✅ You must give appropriate credit
- ❌ You may not use the 3D files for commercial purposes
