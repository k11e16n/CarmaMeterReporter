"""插圖去背 + 裁切成獨立圖片檔。

只保留「去背 + 裁到內容邊界」這段可重用邏輯，供 illustration_generator.py
把 Cloudflare 生成的插圖處理成 LINE Flex Message 卡3的 hero 圖片。

(舊版「在圖表右側開留白區、把插圖貼進去」的合成邏輯已移除——這次插圖是
獨立圖片，不用配圖表。)
"""

from PIL import Image

WHITE_THRESHOLD = 235


def remove_white_background(img, threshold=WHITE_THRESHOLD):
    img = img.convert('RGBA')
    datas = img.getdata()
    new_data = []
    for r, g, b, a in datas:
        if r > threshold and g > threshold and b > threshold:
            new_data.append((r, g, b, 0))
        else:
            new_data.append((r, g, b, a))
    img.putdata(new_data)
    return img


def cutout_sticker(image_path, threshold=WHITE_THRESHOLD):
    """讀圖 -> 去白底 -> 裁到 bounding box，回傳裁切後的 RGBA Image，
    可以直接存檔當獨立的 hero 圖片，不用再做任何合成。"""
    img = Image.open(image_path)
    mascot = remove_white_background(img, threshold=threshold)
    bbox = mascot.getbbox()
    if bbox:
        mascot = mascot.crop(bbox)
    return mascot


if __name__ == "__main__":
    import sys

    result = cutout_sticker(sys.argv[1])
    result.save(sys.argv[2])
    print(f"已存成 {sys.argv[2]}")
