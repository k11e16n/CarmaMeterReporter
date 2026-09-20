"""依情緒從本地 Irasutoya（いらすとや）插圖庫隨機挑一張，去背後存成獨立 PNG。

圖庫由 illustration_pool_refresher.py 定期抽換維護（跟這支模組完全獨立、
不在每日報告的關鍵路徑上）。這裡只負責「挑圖 + 去背」，圖庫是空的或挑圖
失敗時一律 fallback 成本地固定備用圖，絕不讓插圖生成卡住整個每日報告的
推播。
"""

from __future__ import annotations

import glob
import os
import random
import re
import shutil
import sys

from PIL import Image, ImageDraw, ImageFont

# Noto Sans TC 字型檔不含 emoji 字符集，畫進合成圖前要先濾掉，不然會變成
# 方塊亂碼（純文字的 Flex body 不受影響，emoji 在那邊照樣正常顯示）。
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "️"
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    return _EMOJI_PATTERN.sub("", text).strip()

import composite_sticker

POOL_DIR = os.path.join(os.path.dirname(__file__), "assets", "irasutoya_pool")
FALLBACK_IMAGE_PATH = os.path.join(os.path.dirname(__file__), "assets", "fallback_mascot.png")

# 開源字型檔（Noto Sans TC，OFL 授權），不依賴系統字型——本機 macOS 有的
# 中文字型在 Linux VM 上不存在，統一用專案自帶的字型檔才能保證本機/VM
# 顯示結果一致。
_FONT_PATH = os.path.join(os.path.dirname(__file__), "assets", "fonts", "NotoSansTC-Bold.ttf")

# 合成圖的畫布尺寸，20:13 比例跟其他 hero 圖片的 aspectRatio 一致
_CANVAS_SIZE = (800, 520)
_MASCOT_BAND_RATIO = 0.35  # 插圖佔畫面的高度比例（放在上/下其中一條橫帶裡）
_PADDING = 24


def generate_mascot(mood: str, output_path: str) -> str:
    """從 POOL_DIR/<mood>/ 隨機挑一張圖，去背後存成 output_path。

    任何失敗（圖庫是空的、圖檔損毀、去背過程出錯）都會 log [WARN] 到
    stderr，並複製本地固定備用圖到 output_path 頂替，絕不 raise。回傳值
    永遠是 output_path，且該路徑永遠有一張可用的 PNG。
    """
    try:
        candidates = glob.glob(os.path.join(POOL_DIR, mood, "*.png"))
        if not candidates:
            raise FileNotFoundError(f"圖庫是空的，尚未執行過 illustration_pool_refresher.py：{mood}")

        chosen = random.choice(candidates)
        cutout = composite_sticker.cutout_sticker(chosen)
        cutout.save(output_path)
        return output_path
    except Exception as exc:  # 任何失敗都要 fallback，不能讓插圖生成擋住報告
        print(f"[WARN] illustration_generator: 插圖挑選失敗，改用備用圖：{exc}", file=sys.stderr)
        shutil.copyfile(FALLBACK_IMAGE_PATH, output_path)
        return output_path


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """逐字元換行（適合中文，不需要空白斷詞）：一行放到快超過 max_width
    就換下一行。"""
    lines = []
    current = ""
    for ch in text:
        trial = current + ch
        if not current or draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = ch
    if current:
        lines.append(current)
    return lines


def _paste_mascot_in_band(canvas: Image.Image, mascot: Image.Image, is_top: bool, is_left: bool, band_h: int) -> None:
    canvas_w, canvas_h = canvas.size
    max_h = band_h - 2 * _PADDING
    max_w = int(canvas_w * 0.4)
    scale = min(max_h / mascot.height, max_w / mascot.width)
    resized = mascot.resize(
        (max(1, int(mascot.width * scale)), max(1, int(mascot.height * scale))), Image.LANCZOS
    )
    x = _PADDING if is_left else canvas_w - _PADDING - resized.width
    y = _PADDING if is_top else canvas_h - _PADDING - resized.height
    canvas.paste(resized, (x, y), resized)


def compose_observation_card(observation: str | None, mascot_path: str, output_path: str) -> str:
    """把 Gemini 的情緒觀察句跟插圖合成一張圖：文字為主要內容置中，插圖
    縮小放在四個角落隨機一個當裝飾。用「畫面上/下各留一條橫帶給插圖」的
    版面，不管插圖比例、觀察句長度怎麼變，插圖跟文字都不會互相重疊。

    observation 是 None（Gemini 失敗）時，不畫文字，插圖直接置中放大。
    """
    canvas_w, canvas_h = _CANVAS_SIZE
    canvas = Image.new("RGB", (canvas_w, canvas_h), "white")
    mascot = Image.open(mascot_path).convert("RGBA")

    if not observation:
        max_h = canvas_h - 2 * _PADDING
        max_w = canvas_w - 2 * _PADDING
        scale = min(max_h / mascot.height, max_w / mascot.width)
        resized = mascot.resize(
            (max(1, int(mascot.width * scale)), max(1, int(mascot.height * scale))), Image.LANCZOS
        )
        canvas.paste(
            resized, ((canvas_w - resized.width) // 2, (canvas_h - resized.height) // 2), resized
        )
        canvas.save(output_path)
        return output_path

    corner = random.choice(["top-left", "top-right", "bottom-left", "bottom-right"])
    is_top = corner.startswith("top")
    is_left = corner.endswith("left")

    band_h = int(canvas_h * _MASCOT_BAND_RATIO)
    _paste_mascot_in_band(canvas, mascot, is_top, is_left, band_h)

    text_top = band_h if is_top else 0
    text_bottom = canvas_h if is_top else canvas_h - band_h
    text_area_h = text_bottom - text_top
    max_text_width = canvas_w - 2 * _PADDING

    text = _strip_emoji(observation)
    draw = ImageDraw.Draw(canvas)
    font_size = 48
    while font_size > 16:
        font = ImageFont.truetype(_FONT_PATH, font_size)
        lines = _wrap_text(draw, text, font, max_text_width)
        line_height = int(font_size * 1.4)
        if line_height * len(lines) <= text_area_h - 2 * _PADDING:
            break
        font_size -= 4
    else:
        font = ImageFont.truetype(_FONT_PATH, font_size)
        lines = _wrap_text(draw, text, font, max_text_width)
        line_height = int(font_size * 1.4)

    block_h = line_height * len(lines)
    start_y = text_top + (text_area_h - block_h) // 2
    for i, line in enumerate(lines):
        line_w = draw.textlength(line, font=font)
        x = (canvas_w - line_w) // 2
        y = start_y + i * line_height
        draw.text((x, y), line, font=font, fill="#333333")

    canvas.save(output_path)
    return output_path
