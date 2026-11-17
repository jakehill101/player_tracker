"""
Draws the month grid the scheduler bot posts in Discord.

Kept free of discord imports so it can be unit tested. Returns PNG bytes.
"""
import calendar
import io
from datetime import date
from typing import Dict, List, Tuple

from PIL import Image, ImageDraw, ImageFont

# Parchment palette: reads on both Discord light and dark themes.
BG = (244, 236, 216)
INK = (59, 47, 34)
MUTED = (128, 112, 92)
GRID = (196, 182, 156)
EMPTY_CELL = (251, 247, 238)
OPEN_FILL = (214, 232, 200)
OPEN_EDGE = (79, 125, 52)
BLOCKED_FILL = (226, 214, 210)
BLOCKED_EDGE = (150, 84, 76)
TODAY_EDGE = (176, 120, 30)
PAST_SHADE = (244, 236, 216, 150)

CELL_W, CELL_H = 160, 118
MARGIN = 28
HEADER_H = 70
WEEKDAY_H = 30
LEGEND_H = 46

_FONT_PATHS = {
    'regular': ['DejaVuSans.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'],
    'bold': ['DejaVuSans-Bold.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'],
    'serif': ['DejaVuSerif-Bold.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf'],
}


def _font(kind: str, size: int):
    for path in _FONT_PATHS[kind]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _fit(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + '…', font=font) > width:
        text = text[:-1]
    return text.rstrip() + '…'


def render_month(
    year: int,
    month: int,
    gm_days: Dict[date, Tuple[str, str]],
    player_days: Dict[date, Dict[str, List[str]]],
    today: date,
    title: str = 'Sessions',
) -> bytes:
    """Render one month.

    gm_days: {date: ('open' | 'blocked', note)}
    player_days: {date: {'free': [names], 'busy': [names]}}
    """
    weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(year, month)  # Monday first
    width = MARGIN * 2 + CELL_W * 7
    height = MARGIN + HEADER_H + WEEKDAY_H + CELL_H * len(weeks) + LEGEND_H + MARGIN

    img = Image.new('RGB', (width, height), BG)
    draw = ImageDraw.Draw(img, 'RGBA')

    f_title = _font('serif', 30)
    f_sub = _font('regular', 16)
    f_week = _font('bold', 14)
    f_day = _font('bold', 20)
    f_small = _font('regular', 13)
    f_tag = _font('bold', 12)

    # Header
    heading = f'{calendar.month_name[month]} {year}'
    draw.text((MARGIN, MARGIN), heading, font=f_title, fill=INK)
    draw.text((MARGIN, MARGIN + 40), title, font=f_sub, fill=MUTED)

    # Weekday row
    top = MARGIN + HEADER_H
    for i, name in enumerate(calendar.day_abbr):  # Mon..Sun
        x = MARGIN + i * CELL_W
        draw.text((x + 10, top + 6), name.upper(), font=f_week, fill=MUTED)

    grid_top = top + WEEKDAY_H
    for r, week in enumerate(weeks):
        for c, d in enumerate(week):
            x0 = MARGIN + c * CELL_W
            y0 = grid_top + r * CELL_H
            x1, y1 = x0 + CELL_W, y0 + CELL_H
            in_month = d.month == month
            gm = gm_days.get(d) if in_month else None
            players = player_days.get(d, {'free': [], 'busy': []}) if in_month else None

            if gm and gm[0] == 'open':
                fill, edge = OPEN_FILL, OPEN_EDGE
            elif gm and gm[0] == 'blocked':
                fill, edge = BLOCKED_FILL, BLOCKED_EDGE
            else:
                fill, edge = (EMPTY_CELL if in_month else BG), None

            draw.rectangle([x0, y0, x1, y1], fill=fill, outline=GRID)
            if edge:
                draw.rectangle([x0 + 1, y0 + 1, x0 + 5, y1 - 1], fill=edge)

            if not in_month:
                continue

            pad = 12
            draw.text((x0 + pad, y0 + 8), str(d.day), font=f_day, fill=INK)

            if gm:
                tag = 'GM free' if gm[0] == 'open' else 'GM away'
                tw = draw.textlength(tag, font=f_tag)
                draw.rounded_rectangle(
                    [x1 - tw - 18, y0 + 10, x1 - 8, y0 + 28], radius=6, fill=edge
                )
                draw.text((x1 - tw - 13, y0 + 12), tag, font=f_tag, fill=(255, 255, 255))
                if gm[1]:
                    note = _fit(draw, gm[1], f_small, CELL_W - pad * 2)
                    draw.text((x0 + pad, y0 + 38), note, font=f_small, fill=INK)

            if players and (players['free'] or players['busy']):
                free = players['free']
                if free:
                    line = f"{len(free)} free: " + ', '.join(free)
                    draw.text(
                        (x0 + pad, y1 - 44), _fit(draw, line, f_small, CELL_W - pad * 2),
                        font=f_small, fill=OPEN_EDGE,
                    )
                if players['busy']:
                    draw.text(
                        (x0 + pad, y1 - 24), f"{len(players['busy'])} busy",
                        font=f_small, fill=MUTED,
                    )

            if d < today:
                draw.rectangle([x0 + 1, y0 + 1, x1 - 1, y1 - 1], fill=PAST_SHADE)
            if d == today:
                draw.rectangle([x0 + 1, y0 + 1, x1 - 1, y1 - 1], outline=TODAY_EDGE, width=3)

    # Legend
    ly = grid_top + CELL_H * len(weeks) + 16
    x = MARGIN
    for label, colour in (
        ('GM free', OPEN_EDGE),
        ('GM away', BLOCKED_EDGE),
        ('Today', TODAY_EDGE),
    ):
        draw.rectangle([x, ly + 3, x + 14, ly + 17], fill=colour)
        draw.text((x + 22, ly + 1), label, font=f_small, fill=INK)
        x += 34 + int(draw.textlength(label, font=f_small)) + 20
    draw.text((x, ly + 1), 'Players: /available  ·  GM: /gm open, /gm block', font=f_small, fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def month_bounds(year: int, month: int) -> Tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def next_month(year: int, month: int) -> Tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)
