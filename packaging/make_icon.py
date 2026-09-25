"""Draw packaging/icon.png (1024x1024). PyInstaller converts it to .icns/.ico at build time."""
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
# Background: rounded square with a vertical blue gradient.
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = y / S
    gd.line([(0, y), (S, y)], fill=(int(40 + 20 * t), int(110 - 40 * t), int(220 - 60 * t), 255))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([64, 64, S - 64, S - 64], radius=200, fill=255)
img.paste(grad, (0, 0), mask)
# Three stacked "web pages".
for i, off in enumerate((120, 60, 0)):
    x0, y0 = 250 + off, 210 + off
    shade = 225 + i * 15  # back pages darker, front page white
    d.rounded_rectangle([x0, y0, x0 + 420, y0 + 470], radius=36, fill=(shade, shade, shade, 255))
x0, y0 = 250, 210
d.rounded_rectangle([x0, y0, x0 + 420, y0 + 80], radius=36, fill=(225, 232, 245, 255))
for k, c in enumerate(((235, 90, 80), (245, 190, 60), (90, 190, 100))):
    d.ellipse([x0 + 40 + k * 50, y0 + 28, x0 + 68 + k * 50, y0 + 56], fill=c + (255,))
for row in range(4):
    d.rounded_rectangle([x0 + 45, y0 + 130 + row * 60, x0 + 375 - (row % 2) * 90, y0 + 155 + row * 60],
                        radius=12, fill=(190, 200, 220, 255))
# Download arrow badge.
cx, cy, r = 700, 700, 170
d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255))
d.ellipse([cx - r + 18, cy - r + 18, cx + r - 18, cy + r - 18], fill=(30, 150, 90, 255))
d.rectangle([cx - 28, cy - 95, cx + 28, cy + 20], fill=(255, 255, 255, 255))
d.polygon([(cx - 85, cy + 5), (cx + 85, cy + 5), (cx, cy + 95)], fill=(255, 255, 255, 255))
img.save(Path(__file__).with_name("icon.png"))
