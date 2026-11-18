"""Draw the original setup icon (rounded tile, equalizer bars, download arrow)."""
import sys
from PIL import Image, ImageDraw, ImageFilter
S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
# background tile with vertical gradient
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = y / S
    c = (int(0x30 + (0x1a - 0x30) * t), int(0x33 + (0x1b - 0x33) * t), int(0x4a + (0x2a - 0x4a) * t), 255)
    gd.line([(0, y), (S, y)], fill=c)
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([40, 40, S - 40, S - 40], radius=210, fill=255)
img.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(img)
# equalizer bars, orange->amber
bars = [0.42, 0.66, 0.52, 0.80, 0.60, 0.46]
bw, gap = 92, 40
x0 = (S - (len(bars) * bw + (len(bars) - 1) * gap)) // 2
base = 800
for i, h in enumerate(bars):
    top = base - int(h * 560)
    x = x0 + i * (bw + gap)
    col = [(255, 138, 0), (255, 160, 20), (255, 120, 0), (255, 176, 40), (255, 138, 0), (255, 160, 20)][i]
    d.rounded_rectangle([x, top, x + bw, base], radius=30, fill=col + (255,))
# download badge
bx, by, r = 760, 760, 170
shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(shadow).ellipse([bx - r, by - r + 16, bx + r, by + r + 16], fill=(0, 0, 0, 120))
img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(18)))
d = ImageDraw.Draw(img)
d.ellipse([bx - r, by - r, bx + r, by + r], fill=(46, 204, 113, 255), outline=(255, 255, 255, 255), width=18)
d.rectangle([bx - 30, by - 95, bx + 30, by + 20], fill="white")
d.polygon([(bx - 85, by + 5), (bx + 85, by + 5), (bx, by + 95)], fill="white")
out = img.resize((256, 256), Image.LANCZOS)
out.save(sys.argv[1])
