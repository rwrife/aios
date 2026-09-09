"""Translate the desktop's Theme.qml palette for X11 terminal windows."""
import colorsys
import os
from pathlib import Path
import subprocess

from .core import load_config

HUES = dict(blue=.57, teal=.48, sage=.30, amber=.11, copper=.055,
            rose=.96, violet=.73, slate=.60)
OCEAN = ('#101b27', '#354e60', '#172633', '#203340', '#b2c3cd', '#bde4e6', '#4c6574')


def palette(selected):
    if selected not in HUES or selected == 'blue':
        return OCEAN
    hue = HUES[selected]
    saturation = .055 if selected == 'slate' else .24
    def color(lightness, scale=1):
        rgb = colorsys.hls_to_rgb(hue, lightness, saturation * scale)
        return '#' + ''.join(f'{int(c * 255 + .5):02x}' for c in rgb)
    return (color(.10), color(.29), color(.145), color(.19),
            color(.74, .55), color(.81), color(.38))


def apply_chrome(selected):
    """Override the bundled theme for this desktop user only."""
    if not os.environ.get('DISPLAY') or os.environ.get('AIOS_PRINCIPAL'):
        return
    source = Path('/usr/share/themes/AIOS/openbox-3')
    if not source.is_dir():
        return
    target = Path.home() / '.themes/AIOS/openbox-3'
    try:
        target.mkdir(parents=True, exist_ok=True)
        theme = (source / 'themerc').read_text()
        for original, replacement in zip(OCEAN, palette(selected)):
            theme = theme.replace(original, replacement)
        temporary = target / 'themerc.tmp'
        temporary.write_text(theme)
        temporary.replace(target / 'themerc')
        for bitmap in source.glob('*.xbm'):
            (target / bitmap.name).write_bytes(bitmap.read_bytes())
        subprocess.run(['openbox', '--reconfigure'], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        # A display/theme failure must not prevent saving settings or opening a shell.
        pass


def main():
    try:
        selected = load_config().get('theme_color', 'blue')
    except (OSError, ValueError):
        selected = 'blue'
    apply_chrome(selected)
    print(' '.join(palette(selected)))


if __name__ == '__main__':
    main()
