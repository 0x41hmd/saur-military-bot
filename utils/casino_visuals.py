import io
import math
import random
from functools import lru_cache
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from utils import casino_engine as eng

RGB = Tuple[int, int, int]

GOLD: RGB = (255, 200, 40)
DARK_GOLD: RGB = (170, 120, 20)
WHITE: RGB = (250, 250, 250)
BG_TOP: RGB = (38, 14, 66)
BG_BOTTOM: RGB = (10, 6, 28)


_FONT_FILES = [
    "DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf", "seguibl.ttf",
    "LiberationSans-Bold.ttf", "Helvetica-Bold.ttf", "verdanab.ttf",
]


@lru_cache(maxsize=64)
def font(size: int) -> ImageFont.FreeTypeFont:
    for name in _FONT_FILES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def gradient(w: int, h: int, top: RGB, bottom: RGB) -> Image.Image:
    base = Image.new("RGB", (w, h), top)
    ov = Image.new("RGB", (w, h), bottom)
    mask = Image.linear_gradient("L").resize((w, h))
    return Image.composite(ov, base, mask)


def short_amount(n: int) -> str:
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"{v:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        v = n / 1_000
        return f"{v:.1f}K".replace(".0K", "K")
    return str(n)


def _text(d: ImageDraw.ImageDraw, xy, text, size, fill=WHITE, anchor="mm", stroke=0, stroke_fill=(0, 0, 0)):
    d.text(xy, text, font=font(size), fill=fill, anchor=anchor, stroke_width=stroke, stroke_fill=stroke_fill)


def _shared_palette(samples: Sequence[Image.Image]) -> Image.Image:
    w, h = samples[0].size
    sheet = Image.new("RGB", (w, h * len(samples)))
    for i, s in enumerate(samples):
        sheet.paste(s, (0, i * h))
    return sheet.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)


def to_gif(frames: List[Image.Image], durations: List[int], width: Optional[int] = None) -> bytes:
    if width and frames[0].width > width:
        h = round(frames[0].height * width / frames[0].width)
        frames = [f.resize((width, h), Image.LANCZOS) for f in frames]
    step = max(1, len(frames) // 6)
    pal = _shared_palette(frames[::step * 2] + [frames[-1]])
    q = [f.convert("RGB").quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    buf = io.BytesIO()
    q[0].save(buf, format="GIF", save_all=True, append_images=q[1:], duration=durations, loop=0,
              disposal=1, optimize=False)
    return buf.getvalue()


def to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _glow(img: Image.Image, box, color: RGB, radius: int = 10, width: int = 5) -> Image.Image:
    layer = Image.new("RGB", img.size, (0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(box, radius=14, outline=color, width=width)
    layer = layer.filter(ImageFilter.GaussianBlur(radius))
    out = ImageChops.add(img, layer)
    ImageDraw.Draw(out).rounded_rectangle(box, radius=14, outline=color, width=3)
    return out


_SYM = 64
_CELL = 82
_SS = 4


def _star_points(cx, cy, r_out, r_in, n=5):
    pts = []
    for i in range(n * 2):
        ang = -math.pi / 2 + i * math.pi / n
        r = r_out if i % 2 == 0 else r_in
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


@lru_cache(maxsize=None)
def slot_symbol(key: str) -> Image.Image:
    S = _SYM * _SS
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = S // 2
    u = S / 100.0
    if key == "cherry":
        d.line([(c - 18 * u, c + 20 * u), (c + 4 * u, c - 34 * u), (c + 22 * u, c + 14 * u)], fill=(60, 140, 50), width=int(4 * u))
        d.ellipse([c - 40 * u, c + 4 * u, c - 4 * u, c + 40 * u], fill=(215, 30, 50), outline=(120, 10, 25), width=int(2 * u))
        d.ellipse([c + 2 * u, c - 2 * u, c + 38 * u, c + 34 * u], fill=(230, 40, 60), outline=(120, 10, 25), width=int(2 * u))
        d.ellipse([c - 32 * u, c + 10 * u, c - 22 * u, c + 20 * u], fill=(255, 190, 200))
        d.ellipse([c + 10 * u, c + 4 * u, c + 20 * u, c + 14 * u], fill=(255, 190, 200))
        d.polygon([(c + 4 * u, c - 34 * u), (c + 28 * u, c - 42 * u), (c + 20 * u, c - 22 * u)], fill=(70, 170, 60))
    elif key == "lemon":
        d.ellipse([c - 42 * u, c - 28 * u, c + 42 * u, c + 28 * u], fill=(250, 225, 50), outline=(190, 150, 10), width=int(3 * u))
        d.polygon([(c - 46 * u, c), (c - 36 * u, c - 8 * u), (c - 36 * u, c + 8 * u)], fill=(250, 225, 50))
        d.polygon([(c + 46 * u, c), (c + 36 * u, c - 8 * u), (c + 36 * u, c + 8 * u)], fill=(250, 225, 50))
        d.ellipse([c - 28 * u, c - 20 * u, c - 6 * u, c - 8 * u], fill=(255, 250, 170))
        d.polygon([(c + 6 * u, c - 30 * u), (c + 24 * u, c - 40 * u), (c + 16 * u, c - 24 * u)], fill=(70, 170, 60))
    elif key == "bell":
        gold, dark = (255, 205, 50), (180, 120, 10)
        d.pieslice([c - 32 * u, c - 42 * u, c + 32 * u, c + 22 * u], 180, 360, fill=gold, outline=dark, width=int(2 * u))
        d.polygon([(c - 32 * u, c - 10 * u), (c + 32 * u, c - 10 * u), (c + 42 * u, c + 22 * u), (c - 42 * u, c + 22 * u)], fill=gold, outline=dark)
        d.rounded_rectangle([c - 46 * u, c + 18 * u, c + 46 * u, c + 30 * u], radius=int(5 * u), fill=gold, outline=dark, width=int(2 * u))
        d.ellipse([c - 9 * u, c + 26 * u, c + 9 * u, c + 44 * u], fill=dark)
        d.ellipse([c - 6 * u, c - 52 * u, c + 6 * u, c - 40 * u], fill=dark)
        d.ellipse([c - 22 * u, c - 30 * u, c - 10 * u, c - 6 * u], fill=(255, 245, 190))
    elif key == "star":
        d.polygon(_star_points(c, c + 3 * u, 46 * u, 20 * u), fill=(255, 190, 30), outline=(190, 110, 0))
        d.line(_star_points(c, c + 3 * u, 46 * u, 20 * u) + [_star_points(c, c + 3 * u, 46 * u, 20 * u)[0]], fill=(190, 110, 0), width=int(3 * u))
        d.polygon(_star_points(c, c + 3 * u, 22 * u, 10 * u), fill=(255, 235, 140))
    elif key == "diamond":
        top = [(c - 28 * u, c - 28 * u), (c + 28 * u, c - 28 * u), (c + 44 * u, c - 6 * u), (c - 44 * u, c - 6 * u)]
        d.polygon(top, fill=(120, 225, 255))
        d.polygon([(c - 44 * u, c - 6 * u), (c + 44 * u, c - 6 * u), (c, c + 44 * u)], fill=(40, 170, 235))
        d.polygon([(c - 14 * u, c - 6 * u), (c + 14 * u, c - 6 * u), (c, c + 44 * u)], fill=(90, 205, 250))
        d.polygon([(c - 28 * u, c - 28 * u), (c - 14 * u, c - 6 * u), (c, c - 28 * u)], fill=(200, 245, 255))
        d.line(top + [top[0]], fill=(20, 110, 170), width=int(2 * u))
        d.line([(c - 44 * u, c - 6 * u), (c, c + 44 * u), (c + 44 * u, c - 6 * u)], fill=(20, 110, 170), width=int(2 * u))
    elif key == "bar":
        d.rounded_rectangle([c - 46 * u, c - 26 * u, c + 46 * u, c + 26 * u], radius=int(8 * u), fill=(20, 20, 24), outline=GOLD, width=int(4 * u))
        d.text((c, c + 2 * u), "BAR", font=font(int(30 * u)), fill=GOLD, anchor="mm")
    elif key == "seven":
        d.text((c, c), "7", font=font(int(104 * u)), fill=(235, 25, 40), anchor="mm", stroke_width=int(5 * u), stroke_fill=(255, 255, 255))
        d.text((c, c), "7", font=font(int(104 * u)), fill=(230, 20, 35), anchor="mm")
    return im.resize((_SYM, _SYM), Image.LANCZOS)


def _slot_background(w: int, h: int) -> Image.Image:
    img = gradient(w, h, BG_TOP, BG_BOTTOM)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 6, w - 7, h - 7], radius=26, outline=DARK_GOLD, width=6)
    d.rounded_rectangle([12, 12, w - 13, h - 13], radius=22, outline=GOLD, width=2)
    _text(d, (w // 2, 34), "S L O T S", 28, GOLD, stroke=2, stroke_fill=(70, 40, 0))
    for i in range(14):
        x = 34 + i * (w - 68) / 13
        d.ellipse([x - 4, 58, x + 4, 66], fill=(255, 235, 120) if i % 2 == 0 else (200, 90, 40))
    return img


@lru_cache(maxsize=4)
def _reel_shade(win_w: int, win_h: int) -> Image.Image:
    shade = Image.new("RGBA", (win_w, win_h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    for k in range(26):
        a = int(120 * (1 - k / 26))
        sd.line([(0, k), (win_w, k)], fill=(0, 0, 0, a))
        sd.line([(0, win_h - 1 - k), (win_w, win_h - 1 - k)], fill=(0, 0, 0, a))
    return shade


@lru_cache(maxsize=4)
def _reel_cell_bg(win_w: int, win_h: int) -> Image.Image:
    return gradient(win_w, win_h, (255, 252, 244), (225, 214, 196)).convert("RGBA")


def _render_slots_frame(bg: Image.Image, reels: List[List[str]], offsets: List[float],
                        winning: bool, flash: bool) -> Image.Image:
    img = bg.copy()
    w, h = img.size
    win_w, win_h = 128, _CELL * 3
    x0 = (w - (3 * win_w + 2 * 14)) // 2
    top = 84
    d = ImageDraw.Draw(img)
    for i in range(3):
        x = x0 + i * (win_w + 14)
        d.rounded_rectangle([x - 4, top - 4, x + win_w + 4, top + win_h + 4], radius=14, fill=(14, 10, 24), outline=DARK_GOLD, width=3)
        col = _reel_cell_bg(win_w, win_h).copy()
        off = offsets[i]
        syms = reels[i]
        for k, key in enumerate(syms):
            y = int((k - (1 if len(syms) == 4 else 0)) * _CELL + off + (_CELL - _SYM) / 2)
            col.alpha_composite(slot_symbol(key), ((win_w - _SYM) // 2, y))
        col.alpha_composite(_reel_shade(win_w, win_h))
        img.paste(col.convert("RGB"), (x, top))
        d = ImageDraw.Draw(img)
    ly = top + _CELL + _CELL // 2
    d.line([(x0 - 14, ly), (x0 + 3 * win_w + 28 + 14, ly)], fill=(255, 60, 60), width=3)
    d.polygon([(x0 - 22, ly - 9), (x0 - 22, ly + 9), (x0 - 6, ly)], fill=(255, 60, 60))
    d.polygon([(x0 + 3 * win_w + 28 + 22, ly - 9), (x0 + 3 * win_w + 28 + 22, ly + 9), (x0 + 3 * win_w + 28 + 6, ly)], fill=(255, 60, 60))
    if winning and flash:
        img = _glow(img, (x0 - 6, ly - _CELL // 2 - 2, x0 + 3 * win_w + 28 + 6, ly + _CELL // 2 + 2), (255, 230, 90), radius=12, width=6)
    return img


def _render_slots(reels: List[str], won: bool) -> Tuple[bytes, bytes, float]:
    rnd = random.Random()
    keys = eng.SLOT_KEYS
    W, H = 520, 380
    bg = _slot_background(W, H)

    def neighbours(k):
        return [rnd.choice(keys), k, rnd.choice(keys)]

    final = [neighbours(k) for k in reels]
    stop_frames = [10, 15, 20]
    total = 24
    frames, durs = [], []
    speed = 52.0
    for f in range(total):
        cols, offs = [], []
        for i in range(3):
            if f >= stop_frames[i]:
                cols.append(final[i]); offs.append(0.0)
            else:
                spin_state = [rnd.choice(keys) for _ in range(4)]
                cols.append(spin_state); offs.append((f * speed + i * 17) % _CELL)
        frames.append(_render_slots_frame(bg, cols, offs, False, False))
        durs.append(60)
    for k in range(4):
        frames.append(_render_slots_frame(bg, final, [0, 0, 0], won, k % 2 == 0))
        durs.append(200 if k < 3 else 1000)
    still = _render_slots_frame(bg, final, [0, 0, 0], won, True)
    return to_gif(frames, durs), to_png(still), sum(durs) / 1000.0 - 1.4


_TIER_COLORS = {
    "Bust": (200, 55, 55), "Nice": (60, 160, 235), "Great": (70, 200, 120),
    "Huge": (170, 90, 235), "PERFECT 100": (255, 200, 40),
}


def _roll_frame(value: int, settled: bool, flash: bool) -> Image.Image:
    W, H = 520, 300
    img = gradient(W, H, BG_TOP, BG_BOTTOM)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 6, W - 7, H - 7], radius=26, outline=DARK_GOLD, width=6)
    d.rounded_rectangle([12, 12, W - 13, H - 13], radius=22, outline=GOLD, width=2)
    _text(d, (W // 2, 36), "R O L L   1 - 1 0 0", 26, GOLD, stroke=2, stroke_fill=(70, 40, 0))

    mult, name = eng.roll_result(value)
    col = _TIER_COLORS[name] if settled else (210, 210, 225)

    cx, cy, bw = W // 2, 130, 150
    box = [cx - bw // 2, cy - 55, cx + bw // 2, cy + 55]
    d.rounded_rectangle(box, radius=22, fill=(250, 248, 240), outline=col, width=6)
    d.rounded_rectangle([box[0] + 6, box[1] + 6, box[2] - 6, box[3] - 6], radius=16, outline=(210, 205, 190), width=2)
    _text(d, (cx, cy + 2), str(value), 64 if value < 100 else 54, (30, 25, 45))
    for px, py in ((box[0] + 14, box[1] + 14), (box[2] - 14, box[1] + 14), (box[0] + 14, box[3] - 14), (box[2] - 14, box[3] - 14)):
        d.ellipse([px - 4, py - 4, px + 4, py + 4], fill=(190, 185, 170))

    gx0, gx1, gy = 50, W - 78, 232
    gw = gx1 - gx0
    prev = 0
    for top, m, n in eng.ROLL_TIERS:
        x_a = gx0 + gw * prev / 100
        x_b = gx0 + gw * top / 100
        d.rectangle([x_a, gy - 11, x_b, gy + 11], fill=_TIER_COLORS[n])
        label = "x0" if m == 0 else f"x{m:g}"
        if top == 100:
            d.text((gx1 + 10, gy + 30), label, font=font(14), fill=(255, 215, 90), anchor="lm")
        else:
            _text(d, ((x_a + x_b) / 2, gy + 30), label, 14, (225, 225, 240))
        prev = top
    d.rounded_rectangle([gx0 - 3, gy - 14, gx1 + 3, gy + 14], radius=6, outline=(240, 240, 255), width=2)
    mx = gx0 + gw * value / 100
    d.polygon([(mx, gy - 14), (mx - 10, gy - 32), (mx + 10, gy - 32)], fill=WHITE if not settled else col, outline=(0, 0, 0))
    if settled and flash:
        img = _glow(img, (box[0] - 6, box[1] - 6, box[2] + 6, box[3] + 6), col, radius=12, width=5)
    return img


def _render_roll(value: int) -> Tuple[bytes, bytes, float]:
    rnd = random.Random()
    frames, durs = [], []
    n = 16
    for i in range(n):
        shown = rnd.randint(1, 100)
        frames.append(_roll_frame(shown, False, False))
        t = i / (n - 1)
        durs.append(int(45 + 200 * t ** 3))
    for k in range(3):
        frames.append(_roll_frame(value, True, k % 2 == 0))
        durs.append(220 if k < 2 else 1000)
    still = _roll_frame(value, True, True)
    return to_gif(frames, durs), to_png(still), sum(durs) / 1000.0 - 1.4


_WHEEL_COLORS: Dict[int, RGB] = {
    0: (70, 70, 84), 20_000: (50, 120, 220), 30_000: (30, 170, 170), 50_000: (60, 180, 90),
    80_000: (140, 80, 220), 100_000: (240, 140, 40), 150_000: (230, 80, 150), 200_000: (255, 200, 40),
}
_WCANVAS = 460
_WDISC = 380
_WSS = 2


@lru_cache(maxsize=1)
def _wheel_disc() -> Image.Image:
    S = _WDISC * _WSS
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    n = len(eng.WHEEL_SLICES)
    step = 360 / n
    box = [4, 4, S - 5, S - 5]
    for i, prize in enumerate(eng.WHEEL_SLICES):
        a0 = -90 + i * step
        d.pieslice(box, a0, a0 + step, fill=_WHEEL_COLORS[prize], outline=(25, 15, 40), width=4)
    c = S / 2
    for i, prize in enumerate(eng.WHEEL_SLICES):
        label = "BUST" if prize == 0 else short_amount(prize)
        jackpot = prize == eng.WHEEL_JACKPOT
        if jackpot:
            label = "JACKPOT"
        f = font(38 if jackpot else 46)
        l, t, r, b = ImageDraw.Draw(im).textbbox((0, 0), label, font=f)
        tw = r - l
        pad = 24
        txt = Image.new("RGBA", (tw + pad * 2, 90), (0, 0, 0, 0))
        ImageDraw.Draw(txt).text((txt.width / 2, 45), label, font=f, anchor="mm",
                                 fill=(80, 30, 0) if jackpot else (255, 255, 255),
                                 stroke_width=3, stroke_fill=(255, 240, 170) if jackpot else (0, 0, 0))
        mid = -90 + (i + 0.5) * step
        rot = txt.rotate(-mid, resample=Image.BICUBIC, expand=True)
        radius = S / 2 - 30 - tw / 2
        cx_t = c + math.cos(math.radians(mid)) * radius
        cy_t = c + math.sin(math.radians(mid)) * radius
        im.alpha_composite(rot, (int(cx_t - rot.width / 2), int(cy_t - rot.height / 2)))
    d.ellipse(box, outline=(255, 215, 90), width=12)
    d.ellipse([c - 46, c - 46, c + 46, c + 46], fill=(40, 20, 70), outline=(255, 215, 90), width=8)
    d.ellipse([c - 18, c - 18, c + 18, c + 18], fill=(255, 215, 90))
    return im


@lru_cache(maxsize=1)
def _wheel_backdrop() -> Image.Image:
    img = gradient(_WCANVAS, _WCANVAS, BG_TOP, BG_BOTTOM)
    d = ImageDraw.Draw(img)
    c = _WCANVAS // 2 + 8
    d.ellipse([c - 214, c - 214 - 8, c + 214, c + 214 - 8], fill=(90, 50, 20), outline=(255, 215, 90), width=4)
    return img


@lru_cache(maxsize=1)
def _wheel_disc_small() -> Image.Image:
    return _wheel_disc().resize((_WDISC, _WDISC), Image.LANCZOS)


def _wheel_frame(cw_angle: float, bulb_phase: int, blur_span: float = 0.0,
                 highlight: Optional[float] = None) -> Image.Image:
    fast = blur_span > 0
    disc = _wheel_disc_small() if fast else _wheel_disc()
    samples = max(1, min(2, int(blur_span // 20) + 1))
    acc = None
    for s in range(samples):
        off = (s / max(1, samples - 1) - 0.5) * blur_span if samples > 1 else 0.0
        r = disc.rotate(-(cw_angle + off), resample=Image.BICUBIC)
        if not fast:
            r = r.resize((_WDISC, _WDISC), Image.LANCZOS)
        acc = r if acc is None else Image.blend(acc, r, 1 / (s + 1))
    img = _wheel_backdrop().copy()
    cx = cy = _WCANVAS // 2
    cy += 8
    img.paste(acc, (cx - _WDISC // 2, cy - _WDISC // 2 - 8), acc)
    d = ImageDraw.Draw(img)
    for k in range(24):
        a = math.radians(k * 15)
        bx, by = cx + math.cos(a) * 205, cy - 8 + math.sin(a) * 205
        on = (k + bulb_phase) % 2 == 0
        d.ellipse([bx - 6, by - 6, bx + 6, by + 6], fill=(255, 245, 150) if on else (150, 90, 30),
                  outline=(255, 215, 90) if on else (90, 50, 20))
    if highlight is not None:
        n = len(eng.WHEEL_SLICES)
        step = 360 / n
        a0 = -90 + highlight
        d.pieslice([cx - _WDISC // 2, cy - 8 - _WDISC // 2, cx + _WDISC // 2, cy - 8 + _WDISC // 2], a0, a0 + step,
                   fill=None, outline=(255, 255, 255), width=6)
    px, py = cx, cy - 8 - _WDISC // 2 - 2
    d.polygon([(px - 20, py - 26), (px + 20, py - 26), (px, py + 22)], fill=(235, 40, 50), outline=(255, 255, 255))
    d.ellipse([px - 6, py - 20, px + 6, py - 8], fill=(255, 255, 255))
    return img


def _render_wheel(slice_index: int) -> Tuple[bytes, bytes, float]:
    n = len(eng.WHEEL_SLICES)
    step = 360 / n
    wobble = random.choice((-0.3, -0.12, 0.0, 0.12, 0.3)) * step
    land = (360 - (slice_index + 0.5) * step + wobble) % 360
    total = 360 * 2 + land
    frames, durs = [], []
    N = 22
    prev = 0.0
    for i in range(1, N + 1):
        t = i / N
        ang = total * (1 - (1 - t) ** 3)
        blur = min(50.0, (ang - prev) * 0.9)
        frames.append(_wheel_frame(ang, i, blur))
        prev = ang
        durs.append(int(70 + 110 * t ** 4))
    for k in range(4):
        frames.append(_wheel_frame(total, k, 0.0, highlight=(slice_index * step + total % 360) % 360 if k % 2 == 0 else None))
        durs.append(240 if k < 3 else 1000)
    still = _wheel_frame(total, 0, 0.0, highlight=(slice_index * step + total % 360) % 360)
    return to_gif(frames, durs, width=320), to_png(still), sum(durs) / 1000.0 - 1.4


_CW, _CH = 92, 130
_CSS = 3


def _heart(d, cx, cy, s, fill):
    r = s * 0.27
    d.ellipse([cx - s * 0.5, cy - s * 0.46, cx, cy + r * 0.2 + s * 0.08], fill=fill)
    d.ellipse([cx, cy - s * 0.46, cx + s * 0.5, cy + r * 0.2 + s * 0.08], fill=fill)
    d.polygon([(cx - s * 0.49, cy - s * 0.12), (cx + s * 0.49, cy - s * 0.12), (cx, cy + s * 0.5)], fill=fill)


def _suit(d, suit: str, cx: float, cy: float, s: float):
    red, black = (205, 28, 40), (24, 24, 30)
    if suit == "H":
        _heart(d, cx, cy, s, red)
    elif suit == "D":
        d.polygon([(cx, cy - s * 0.52), (cx + s * 0.4, cy), (cx, cy + s * 0.52), (cx - s * 0.4, cy)], fill=red)
    elif suit == "S":
        d.ellipse([cx - s * 0.5, cy - s * 0.12, cx, cy + s * 0.34], fill=black)
        d.ellipse([cx, cy - s * 0.12, cx + s * 0.5, cy + s * 0.34], fill=black)
        d.polygon([(cx - s * 0.49, cy + s * 0.04), (cx + s * 0.49, cy + s * 0.04), (cx, cy - s * 0.54)], fill=black)
        d.polygon([(cx, cy + s * 0.1), (cx + s * 0.18, cy + s * 0.52), (cx - s * 0.18, cy + s * 0.52)], fill=black)
    else:
        r = s * 0.25
        for ox, oy in ((0, -0.26), (-0.27, 0.1), (0.27, 0.1)):
            d.ellipse([cx + ox * s - r, cy + oy * s - r, cx + ox * s + r, cy + oy * s + r], fill=black)
        d.polygon([(cx, cy - 0.05 * s), (cx + s * 0.17, cy + s * 0.52), (cx - s * 0.17, cy + s * 0.52)], fill=black)


@lru_cache(maxsize=128)
def card_image(rank: str, suit: str) -> Image.Image:
    W, H = _CW * _CSS, _CH * _CSS
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=12 * _CSS, fill=(252, 251, 246), outline=(90, 90, 100), width=2 * _CSS)
    color = (205, 28, 40) if suit in "HD" else (24, 24, 30)
    label = rank
    d.text((12 * _CSS, 20 * _CSS), label, font=font(24 * _CSS if len(label) == 1 else 21 * _CSS), fill=color, anchor="mm")
    _suit(d, suit, 12 * _CSS, 44 * _CSS, 15 * _CSS)
    corner = Image.new("RGBA", (30 * _CSS, 60 * _CSS), (0, 0, 0, 0))
    cd = ImageDraw.Draw(corner)
    cd.text((15 * _CSS, 20 * _CSS), label, font=font(24 * _CSS if len(label) == 1 else 21 * _CSS), fill=color, anchor="mm")
    _suit(cd, suit, 15 * _CSS, 44 * _CSS, 15 * _CSS)
    corner = corner.rotate(180)
    im.alpha_composite(corner, (W - 30 * _CSS - 0, H - 60 * _CSS - 0))
    if rank in ("J", "Q", "K"):
        d.rounded_rectangle([24 * _CSS, 30 * _CSS, W - 24 * _CSS, H - 30 * _CSS], radius=8 * _CSS, outline=color, width=2 * _CSS)
        d.text((W // 2, H // 2 - 10 * _CSS), rank, font=font(40 * _CSS), fill=color, anchor="mm")
        _suit(d, suit, W / 2, H / 2 + 22 * _CSS, 22 * _CSS)
    else:
        _suit(d, suit, W / 2, H / 2, 36 * _CSS if rank != "A" else 44 * _CSS)
    return im.resize((_CW, _CH), Image.LANCZOS)


@lru_cache(maxsize=1)
def card_back() -> Image.Image:
    W, H = _CW * _CSS, _CH * _CSS
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=12 * _CSS, fill=(250, 250, 250), outline=(90, 90, 100), width=2 * _CSS)
    inner = [7 * _CSS, 7 * _CSS, W - 7 * _CSS, H - 7 * _CSS]
    d.rounded_rectangle(inner, radius=8 * _CSS, fill=(150, 25, 45))
    step = 12 * _CSS
    for y in range(inner[1], inner[3], step):
        for x in range(inner[0], inner[2], step):
            d.polygon([(x + step / 2, y), (x + step, y + step / 2), (x + step / 2, y + step), (x, y + step / 2)], outline=(235, 190, 80))
    d.rounded_rectangle(inner, radius=8 * _CSS, outline=(235, 190, 80), width=2 * _CSS)
    return im.resize((_CW, _CH), Image.LANCZOS)


def _felt(w: int, h: int) -> Image.Image:
    img = gradient(w, h, (22, 112, 70), (10, 60, 38))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 6, w - 7, h - 7], radius=26, outline=(110, 70, 25), width=8)
    d.rounded_rectangle([14, 14, w - 15, h - 15], radius=20, outline=(235, 195, 90), width=2)
    return img


def _paste_card(img: Image.Image, card: Optional[eng.Card], x: int, y: int) -> None:
    sprite = card_back() if card is None else card_image(*card)
    shadow = Image.new("RGBA", sprite.size, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 90), mask=sprite.split()[3])
    img.paste(shadow, (x + 3, y + 4), shadow)
    img.paste(sprite, (x, y), sprite)


def _hand_row(img: Image.Image, cards: Sequence[Optional[eng.Card]], cx: int, y: int, max_w: int) -> Tuple[int, int]:
    n = len(cards)
    gap = min(_CW + 12, (max_w - _CW) // max(1, n - 1)) if n > 1 else 0
    total_w = _CW + gap * (n - 1)
    x0 = cx - total_w // 2
    for i, c in enumerate(cards):
        _paste_card(img, c, x0 + i * gap, y)
    return x0, x0 + total_w


def _tag(d: ImageDraw.ImageDraw, cx: int, y: int, text: str, fill=(0, 0, 0, 0), color=WHITE, size=20):
    f = font(size)
    bbox = d.textbbox((0, 0), text, font=f)
    w = bbox[2] - bbox[0] + 28
    d.rounded_rectangle([cx - w // 2, y, cx + w // 2, y + size + 14], radius=14, fill=(15, 30, 22), outline=(235, 195, 90), width=2)
    d.text((cx, y + (size + 14) // 2), text, font=f, fill=color, anchor="mm")


def render_blackjack(game: eng.Blackjack, reveal: bool, results: Optional[List[Dict]] = None) -> bytes:
    W = 640
    n_hands = len(game.hands)
    H = 490
    img = _felt(W, H)
    d = ImageDraw.Draw(img)
    _text(d, (W // 2, 40), "B L A C K J A C K", 24, (235, 195, 90), stroke=2, stroke_fill=(20, 40, 28))

    dealer_cards: List[Optional[eng.Card]] = list(game.dealer) if reveal else [game.dealer[0], None]
    _hand_row(img, dealer_cards, W // 2, 70, 360)
    d = ImageDraw.Draw(img)
    if reveal:
        dt = eng.hand_total(game.dealer)[0]
        _tag(d, W // 2, 212, f"DEALER  {dt}" + ("  BUST" if dt > 21 else ""))
    else:
        _tag(d, W // 2, 212, f"DEALER  {eng.card_value(game.dealer[0][0]) if game.dealer[0][0] != 'A' else 11}  +  ?")

    slot_w = W // n_hands
    for i, h in enumerate(game.hands):
        cx = slot_w * i + slot_w // 2
        _hand_row(img, h.cards, cx, 262, slot_w - 30)
        d = ImageDraw.Draw(img)
        total, soft = eng.hand_total(h.cards)
        label = f"{'HAND ' + str(i + 1) + '  ' if n_hands > 1 else 'YOU  '}{total}"
        if soft and total < 21 and not h.done:
            label += " (soft)"
        if h.busted:
            label += "  BUST"
        _tag(d, cx, 402, label)
        bet_txt = f"BET {short_amount(h.bet)}" + ("  DOUBLED" if h.doubled else "")
        _text(d, (cx, 454), bet_txt, 15, (225, 235, 225))
        if results:
            res = results[i]["outcome"].upper()
            color = {"WIN": (120, 255, 150), "BLACKJACK": (255, 220, 80), "PUSH": (210, 210, 230),
                     "SURRENDER": (255, 190, 120)}.get(res, (255, 110, 110))
            _text(d, (cx, 242), res, 24, color, stroke=3, stroke_fill=(10, 30, 20))
        elif n_hands > 1 and i == game.active and not game.finished:
            d.rounded_rectangle([cx - 100, 258, cx + 100, 262 + _CH + 4], radius=10, outline=(255, 235, 120), width=3)
    return to_png(img)


def render_poker(cards: Sequence[eng.Card], held: Sequence[bool], drawn: bool,
                 hand_name: Optional[str], payout: int, bet: int) -> bytes:
    W, H = 640, 330
    img = _felt(W, H)
    d = ImageDraw.Draw(img)
    _text(d, (W // 2, 38), "V I D E O   P O K E R", 24, (235, 195, 90), stroke=2, stroke_fill=(20, 40, 28))
    gap = 18
    total_w = 5 * _CW + 4 * gap
    x0 = (W - total_w) // 2
    for i, c in enumerate(cards):
        x = x0 + i * (_CW + gap)
        y = 82 - (14 if held[i] else 0)
        _paste_card(img, c, x, y)
        d = ImageDraw.Draw(img)
        if held[i]:
            d.rounded_rectangle([x - 4, y - 4, x + _CW + 4, y + _CH + 4], radius=14, outline=(255, 225, 90), width=4)
            d.rounded_rectangle([x + 8, 232, x + _CW - 8, 256], radius=10, fill=(255, 205, 50))
            d.text((x + _CW // 2, 244), "HELD", font=font(16), fill=(60, 30, 0), anchor="mm")
    if drawn:
        if hand_name:
            _tag(d, W // 2, 276, f"{hand_name.upper()}  \u2022  pays x{eng.POKER_PAYS[hand_name]}", color=(255, 225, 90), size=20)
        else:
            _tag(d, W // 2, 276, "NO WINNING HAND", color=(255, 130, 130), size=20)
    else:
        _tag(d, W // 2, 276, "Tap cards to HOLD, then press DRAW", size=18)
    return to_png(img)


import threading as _th
_CACHE: Dict[tuple, List[Tuple[bytes, bytes, float]]] = {}
_LOCK = _th.Lock()
_VARIANTS = 3


def _cached(key: tuple, build):
    with _LOCK:
        have = _CACHE.setdefault(key, [])
        if len(have) >= _VARIANTS:
            return random.choice(have)
    res = build()
    with _LOCK:
        have = _CACHE.setdefault(key, [])
        if len(have) < _VARIANTS:
            have.append(res)
    return res


def render_slots(reels: List[str], won: bool):
    return _cached(("slots", tuple(reels), bool(won)), lambda: _render_slots(reels, won))


def render_roll(value: int):
    return _cached(("roll", int(value)), lambda: _render_roll(value))


def render_wheel(slice_index: int):
    return _cached(("wheel", int(slice_index)), lambda: _render_wheel(slice_index))


def prewarm() -> None:
    for i in range(len(eng.WHEEL_SLICES)):
        render_wheel(i)
    for v in (1, 25, 50, 75, 100):
        render_roll(v)
