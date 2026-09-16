# Release Checklist

## GitHub
- [ ] Public repository exists at https://github.com/AkmaulHoque/GIS-Work-Agent
- [ ] Source files are visible, not only a ZIP file
- [ ] README renders correctly
- [ ] Issues are enabled
- [ ] Create a Git tag/release for `v1.1.0`

## QGIS Plugin Repository
- [ ] `metadata.txt` links open successfully
- [ ] `icon.png` displays correctly
- [ ] ZIP has one top-level folder: `gis_work_agent/`
- [ ] No `.pyc`, `__pycache__`, binary executables or virtual environment
- [ ] Install ZIP in a clean QGIS profile and test open / preview / run
- [ ] Test at least one vector and one raster prompt
- [ ] Upload at https://plugins.qgis.org/
