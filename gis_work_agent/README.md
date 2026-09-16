# GIS Work Agent

**Prompt-driven GIS work assistant for QGIS**

GIS Work Agent translates simple, everyday text into safe QGIS Processing workflows. It is designed for both beginners and GIS professionals: users can ask for common vector, raster, table and multi-step tasks without memorizing Processing algorithm names.

## Authors

- **Mr. Akmaul Hoque**
- **Dr. Manish Kumar Naskar**
- **Dr. Debasish Chakraborty**

## Main features

- Beginner-friendly prompts such as `tell me the area in hectares` or `make a 500 m ring around roads`.
- Context-aware suggested prompts based on loaded QGIS layers.
- Active-layer fallback when the prompt says `this layer` or does not name a layer.
- Fuzzy layer-name matching.
- Multi-step workflows using `then`.
- Vector, raster, table and Processing Toolbox operations.
- Workflow preview before execution.
- Prompt history.
- Temporary or project-folder outputs.
- Detailed diagnostics for planning and Processing failures.
- Interactive toolbar status icon: ready, running, success and error.
- Prompt text is never executed as Python or a shell command.

## Examples

```text
Calculate the area in hectares
Make a 500 m ring around Roads
Cut Landuse using District_Boundary then calculate area in hectares
Fix geometries of Village_Boundary then dissolve
Calculate slope from DEM then create contours at 10 m interval
Calculate zonal mean of Rainfall using Districts
Run native:buffer on Roads DISTANCE=250 DISSOLVE=true
```

## Compatibility

- QGIS 3.28 and later
- QGIS 4.x / Qt 6 ready through `qgis.PyQt`

## Installation

### From ZIP
1. Download the release ZIP.
2. In QGIS open **Plugins → Manage and Install Plugins → Install from ZIP**.
3. Select the ZIP and install.
4. Open **GIS Work Agent** from the Plugins menu or toolbar.

### Development installation
Copy the `gis_work_agent` folder into your QGIS profile's `python/plugins` folder and restart QGIS.

## GitHub repository

https://github.com/AkmaulHoque/GIS-Work-Agent

## Issues

https://github.com/AkmaulHoque/GIS-Work-Agent/issues

## Publishing to the QGIS Plugin Repository

Before uploading to the official QGIS Plugin Repository:

1. Create this public GitHub repository first: `AkmaulHoque/GIS-Work-Agent`.
2. Upload the **contents** of the GitHub-source package, preserving the `gis_work_agent/` plugin folder.
3. Confirm the repository, README and Issues links work publicly.
4. Upload the QGIS release ZIP whose top-level folder is exactly `gis_work_agent`.
5. Do not include `__pycache__`, `.pyc`, binaries, virtual environments or build artifacts.

The `metadata.txt` in version 1.1.0 already points to the intended GitHub repository, tracker and README page.

## License

GNU General Public License v2.0 or later (GPL-2.0-or-later).
