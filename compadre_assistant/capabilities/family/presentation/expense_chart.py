"""Рисует диаграмму категорий семейных расходов в памяти."""

from io import BytesIO
import os

from PIL import Image, ImageDraw, ImageFont


def render_chart(period: str, total: str, rows: list[tuple[str, int, str]]) -> bytes:
    """Возвращает PNG с общей шкалой для положительных и отрицательных сумм.

    Args:
        period: Подпись выбранного периода.
        total: Отформатированный общий итог.
        rows: Подпись категории, сумма в копейках и сумма в рублях для вывода.
    """
    font_name = "arial.ttf" if os.name == "nt" else "DejaVuSans.ttf"
    body = ImageFont.truetype(font_name, 30)
    heading = ImageFont.truetype(font_name, 42)
    width, margin = 920, 40
    image = Image.new("RGB", (width, 140 + 94 * len(rows)), "#fafaf7")
    draw = ImageDraw.Draw(image)
    draw.text((margin, 22), f"Расходы семьи · {period}", font=body, fill="#334744")
    draw.text((margin, 64), total, font=heading, fill="#152e2b")
    values = [0, *(amount for _, amount, _ in rows)]
    low, high = min(values), max(values)
    scale = (width - 2 * margin) / max(1, high - low)
    zero = margin + round(-low * scale)
    for index, (label, amount, display) in enumerate(rows):
        y = 143 + 94 * index
        available = width - 2 * margin - draw.textlength(display, font=body) - 24
        if draw.textlength(label, font=body) > available:
            while label and draw.textlength(label + "…", font=body) > available:
                label = label[:-1]
            label += "…"
        draw.text((margin, y), label, font=body, fill="#152e2b")
        draw.text((width-margin, y), display, font=body, anchor="ra", fill="#152e2b")
        end = zero + round(amount * scale)
        draw.rectangle((margin, y+49, width-margin, y+60), fill="#dfe6e2")
        if amount:
            draw.rectangle((min(zero, end), y+49, max(zero, end), y+60),
                           fill="#127463" if amount > 0 else "#a45136")
        draw.line((zero, y+45, zero, y+64), fill="#415451", width=2)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
