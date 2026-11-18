#!/usr/bin/env python3
"""OpenStacks wallpaper generator.

Usage: python3 openstacks_wallpaper.py [output_dir]
       python3 openstacks_wallpaper.py --android [output_dir]   (default: patches/assets)

Writes a square SVG master (crops to any size with cover / xMidYMid slice) and
dithered, banding-free PNGs for several resolutions and orientations.

--android writes the in-guest wallpapers used by patches/all/15_wallpaper.py instead:
  wallpaper-<M>.png       Android 7/9. Launcher3-based launchers (Lawnchair) ask for a 2M x M
                          wallpaper (M = longer screen side) and these versions draw it 1:1,
                          so: 2:1, the square fits the height, the card grid continues
                          sideways; one per common M.
  wallpaper-land/port.png Android 10+ scale the wallpaper to the screen: plain cover crops
                          (16:9 / 9:16), like the desktop renders.

Needs: numpy, Pillow, and rsvg-convert on PATH or PySide6 (QtSvg) for the card layer.
"""
import os, random, re, subprocess, sys, tempfile
import numpy as np
from PIL import Image

ANDROID = '--android' in sys.argv
_args = [a for a in sys.argv[1:] if a != '--android']
OUT = _args[0] if _args else (os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'patches', 'assets')
                              if ANDROID else './wallpaper')
ANDROID_HEIGHTS = (1280, 1600, 1920)   # M: longer screen side of common instance sizes
S = 3000  # world size; every output is a "cover" crop of this square
SIZES = {
    '3840x2160': (3840, 2160), '2560x1440': (2560, 1440), '1920x1080': (1920, 1080),
    '2160x3840-portrait': (2160, 3840), '1080x1920-portrait': (1080, 1920),
    '3000x3000-square': (3000, 3000),
}
NOISE = ('<filter id="nz" x="0" y="0" width="100%" height="100%"><feTurbulence type="fractalNoise" '
         'baseFrequency="0.9" numOctaves="2" seed="3" result="n"/><feColorMatrix in="n" type="matrix" '
         'values="0 0 0 0 1  0 0 0 0 1  0 0 0 0 1  1 0 0 0 0"/></filter>'
         '<rect width="3000" height="3000" filter="url(#nz)" opacity="0.025"/>')

def stack(x, y, s, teal=False):
    """Three offset rounded cards (the logo's stacked-phone motif)."""
    w, h, rx = 300 * s, 490 * s, 64 * s
    d, sw = 62 * s, max(4, 5 * s)
    col = '#2DD4BF' if teal else '#A5B4FC'
    base = 0.14 if teal else 0.18
    return (
        f'<rect x="{x-2*d:.0f}" y="{y-1.6*d:.0f}" width="{w:.0f}" height="{h:.0f}" rx="{rx:.0f}" fill="none" stroke="{col}" stroke-width="{sw:.1f}" opacity="{base*0.45:.3f}"/>'
        f'<rect x="{x-d:.0f}" y="{y-0.8*d:.0f}" width="{w:.0f}" height="{h:.0f}" rx="{rx:.0f}" fill="#0A0B33" fill-opacity="0.6" stroke="{col}" stroke-width="{sw:.1f}" opacity="{base*0.8:.3f}"/>'
        f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" rx="{rx:.0f}" fill="#12144F" fill-opacity="0.7" stroke="{col}" stroke-width="{sw:.1f}" opacity="{base:.3f}"/>')

def cards_svg(extend=0):
    random.seed(11)
    out, px, py = [], 620, 780
    for r, y in enumerate(range(-300, S + 400, py)):
        for x in range(-400 + (px // 2 if r % 2 else 0), S + 400, px):
            if random.random() < 0.2: continue
            s = random.choice([0.75, 0.95, 1.0, 1.25, 1.5])
            x += random.randint(-60, 60); y2 = y + random.randint(-70, 70)
            out.append(stack(x, y2, s, teal=random.random() < 0.22))
    out.append(stack(560, 2020, 1.0))  # fills the empty bottom-left gap
    if extend:
        # extra columns left/right of the square on the same grid (own seed, so the square
        # itself stays identical to the master)
        rnd = random.Random(23)
        for r, y in enumerate(range(-300, S + 400, py)):
            x0 = -400 + (px // 2 if r % 2 else 0)
            xs = [x0 - k * px for k in range(1, extend // px + 2)]
            xs += [x0 + k * px for k in range((S + 800) // px + 1, (S + 800 + extend) // px + 2)]
            for x in xs:
                if rnd.random() < 0.2: continue
                s = rnd.choice([0.75, 0.95, 1.0, 1.25, 1.5])
                x += rnd.randint(-60, 60); y2 = y + rnd.randint(-70, 70)
                out.append(stack(x, y2, s, teal=rnd.random() < 0.22))
    return ''.join(out)

# Background layers (also used by the SVG master as gradients)
DEFS = ('<defs>'
 '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#060724"/><stop offset="0.55" stop-color="#0A0B33"/><stop offset="1" stop-color="#0E1046"/></linearGradient>'
 '<radialGradient id="g1" cx="0.82" cy="0.86" r="0.55"><stop offset="0" stop-color="#4F46E5" stop-opacity="0.34"/><stop offset="1" stop-color="#4F46E5" stop-opacity="0"/></radialGradient>'
 '<radialGradient id="g2" cx="0.12" cy="0.1" r="0.45"><stop offset="0" stop-color="#0D9488" stop-opacity="0.20"/><stop offset="1" stop-color="#0D9488" stop-opacity="0"/></radialGradient>'
 '<radialGradient id="vg" cx="0.5" cy="0.5" r="0.75"><stop offset="0.55" stop-color="#04051A" stop-opacity="0"/><stop offset="1" stop-color="#04051A" stop-opacity="0.55"/></radialGradient>'
 '</defs>')
def rect(fill): return f'<rect width="{S}" height="{S}" fill="url(#{fill})"/>'

def master_svg(cards, w=S, h=S):
    body = DEFS + rect('bg') + rect('g2') + rect('g1') + cards + rect('vg') + NOISE
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {S} {S}" '
            f'preserveAspectRatio="xMidYMid slice">{body}</svg>\n')

def hexc(h): return np.array([int(h[i:i+2], 16) for i in (1, 3, 5)], np.float32)

def rasterize(svg, W, H):
    """SVG text -> float RGBA array (rsvg-convert if present, else Qt's SVG renderer)."""
    import shutil
    with tempfile.TemporaryDirectory() as t:
        open(f'{t}/c.svg', 'w').write(svg)
        if shutil.which('rsvg-convert'):
            subprocess.run(['rsvg-convert', f'{t}/c.svg', '-o', f'{t}/c.png'], check=True)
        else:
            from PySide6.QtCore import QRectF
            from PySide6.QtGui import QGuiApplication, QImage, QPainter
            from PySide6.QtSvg import QSvgRenderer
            app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841
            img = QImage(W, H, QImage.Format_ARGB32_Premultiplied); img.fill(0)
            p = QPainter(img); p.setRenderHint(QPainter.Antialiasing)
            QSvgRenderer(f'{t}/c.svg').render(p, QRectF(0, 0, W, H)); p.end()
            img.convertToFormat(QImage.Format_RGBA8888).save(f'{t}/c.png')
        return np.asarray(Image.open(f'{t}/c.png').convert('RGBA'), np.float32) / 255

def render_png(cards, W, H, path, rng, fit_height=False):
    """Background in float, cards composited on top, then triangular dither -> 8-bit.
    fit_height: the square spans the height and the world extends sideways (Android)."""
    sc = (H if fit_height else max(W, H)) / S
    vw, vh = W / sc, H / sc
    layer = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
             f'viewBox="{(S - vw) / 2:.3f} {(S - vh) / 2:.3f} {vw:.3f} {vh:.3f}">{cards}</svg>')
    c = rasterize(layer, W, H)
    ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
    u = (xs + 0.5 - W / 2) / sc + S / 2
    v = (ys + 0.5 - H / 2) / sc + S / 2
    t_ = (u + v) / (2 * S)
    stops = [(0, '#060724'), (0.55, '#0A0B33'), (1, '#0E1046')]
    img = np.stack([np.interp(t_, [s[0] for s in stops], [hexc(s[1])[ch] for s in stops]) for ch in range(3)], -1).astype(np.float32)
    for cx, cy, r, col, a0 in ((.12, .1, .45, '#0D9488', .20), (.82, .86, .55, '#4F46E5', .34)):
        d = np.sqrt((u - cx * S) ** 2 + (v - cy * S) ** 2) / (r * S)
        a = np.interp(d, [0, 1], [a0, 0])[..., None]
        img = img * (1 - a) + hexc(col) * a
    img = img * (1 - c[..., 3:4]) + c[..., :3] * 255 * c[..., 3:4]
    d = np.sqrt((u - S / 2) ** 2 + (v - S / 2) ** 2) / (.75 * S)
    a = np.interp(d, [0, .55, 1], [0, 0, .55])[..., None]
    img = img * (1 - a) + hexc('#04051A') * a
    noise = (rng.random((H, W, 1), dtype=np.float32) + rng.random((H, W, 1), dtype=np.float32) - 1) * 1.2
    Image.fromarray(np.clip(np.rint(img + noise), 0, 255).astype(np.uint8)).save(path, optimize=True)

if __name__ == '__main__' and ANDROID:
    os.makedirs(OUT, exist_ok=True)
    cards = cards_svg(extend=S)
    rng = np.random.default_rng(5)
    for m in ANDROID_HEIGHTS:
        render_png(cards, 2 * m, m, os.path.join(OUT, f'wallpaper-{m}.png'), rng, fit_height=True)
        print('wrote', f'wallpaper-{m}.png ({2 * m}x{m})')
    plain = cards_svg()
    for name, (w, h) in (('land', (1920, 1080)), ('port', (1080, 1920))):
        render_png(plain, w, h, os.path.join(OUT, f'wallpaper-{name}.png'), rng)
        print('wrote', f'wallpaper-{name}.png ({w}x{h})')
elif __name__ == '__main__':
    os.makedirs(OUT, exist_ok=True)
    cards = cards_svg()
    open(f'{OUT}/openstacks-wallpaper.svg', 'w').write(master_svg(cards))
    rng = np.random.default_rng(5)
    for name, (w, h) in SIZES.items():
        render_png(cards, w, h, f'{OUT}/openstacks-wallpaper-{name}.png', rng)
        print('wrote', name)
