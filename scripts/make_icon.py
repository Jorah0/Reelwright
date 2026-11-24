"""Draw the Reelwright logo (same geometry as static/logo.svg) into assets/reelwright.ico + .png for the .exe."""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "assets"
S = 1024
k = S / 64  # logo.svg uses a 64x64 viewBox


def gradient(size: int) -> Image.Image:
    """Diagonal #FF3D6E -> #FF7A3D -> #FFC247 (bottom-left to top-right), like the SVG."""
    stops = [(0.0, (255, 61, 110)), (0.55, (255, 122, 61)), (1.0, (255, 194, 71))]
    line = []
    for i in range(2 * size):
        t = i / (2 * size - 1)
        for (a, ca), (b, cb) in zip(stops, stops[1:]):
            if a <= t <= b:
                f = (t - a) / (b - a)
                line.append(tuple(round(ca[j] + (cb[j] - ca[j]) * f) for j in range(3)))
                break
    img = Image.new("RGB", (size, size))
    img.putdata([line[x + (size - 1 - y)] for y in range(size) for x in range(size)])
    return img


def main():
    OUT.mkdir(exist_ok=True)
    bg = (17, 18, 24, 255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=16 * k, fill=bg)
    mask = Image.new("L", (S, S), 0)
    m = ImageDraw.Draw(mask)
    m.pieslice([12 * k, 12 * k, 52 * k, 52 * k], start=-30, end=270, fill=255)  # reel minus the -90..-30 deg wedge
    m.line([37 * k, 22.5 * k, 50.5 * k, 9.5 * k], fill=255, width=round(3.2 * k))  # blade
    for cx, cy in ((37, 22.5), (50.5, 9.5)):
        r = 1.6 * k
        m.ellipse([cx * k - r, cy * k - r, cx * k + r, cy * k + r], fill=255)
    img.paste(gradient(S), (0, 0), mask)
    for cx, cy in ((41.5, 37.5), (30.1, 43), (21.2, 35.8), (22.5, 24.5)):  # reel holes
        r = 3.4 * k
        d.ellipse([cx * k - r, cy * k - r, cx * k + r, cy * k + r], fill=bg)
    d.polygon([(29 * k, 27.2 * k), (37.4 * k, 32 * k), (29 * k, 36.8 * k)], fill=bg)  # play
    r = 2 * k
    d.ellipse([53.2 * k - r, 7 * k - r, 53.2 * k + r, 7 * k + r], fill=(255, 194, 71, 255))  # spark
    img.save(OUT / "reelwright.png")
    img.save(OUT / "reelwright.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", OUT / "reelwright.ico")


if __name__ == "__main__":
    main()
