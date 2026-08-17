# -*- coding: utf-8 -*-
"""一次性生成 16 张示例图到 samples/，用于静态预览页（无需服务器）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as appmod
from concurrent.futures import ThreadPoolExecutor, as_completed

SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
os.makedirs(SAMPLES, exist_ok=True)

token = appmod.get_token()
if not token:
    print("NO_TOKEN")
    sys.exit(1)

answers = {
    "kids": "8 个",
    "format": "幼儿园班级课",
    "personality": "活泼好动",
    "effect": "培养专注力与亲子互动",
    "theme": "海洋小动物",
}
prompts = appmod.build_prompts(answers)


def work(item):
    fname = f"{item['kind']}_{item['idx']}.jpg"
    path = os.path.join(SAMPLES, fname)
    if os.path.exists(path) and os.path.getsize(path) > 5000:
        return f"{fname}: skip"
    try:
        url = appmod.generate_image_url(item["prompt"], token)
        appmod.download_image(url, path)
        return f"{fname}: ok"
    except Exception as e:
        return f"{fname}: ERR {e}"


with ThreadPoolExecutor(max_workers=4) as ex:
    futs = [ex.submit(work, it) for it in prompts]
    for f in as_completed(futs):
        print(f.result(), flush=True)

print("DONE", flush=True)
