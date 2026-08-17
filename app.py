# -*- coding: utf-8 -*-
"""
艺术课效果图生成器 · 后端
--------------------------------
交互问卷 -> 根据用户回答生成 16 张图：
  · 8 张「论坛风参考图」（像家长/幼教在小红书随手拍的真实作品照）
  · 8 张「对应课堂效果图」（按主题落地的 4-5 岁幼儿成品效果图）
生图调用打包进来的 buddy_cloud.py（腾讯云混元生图 3.0）。
"""

import os
import io
import json
import time
import uuid
import threading

import requests
from flask import (
    Flask, request, render_template, jsonify, send_from_directory, abort
)

# ---------------------------------------------------------------------------
# 路径与基础配置
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GENERATED_DIR = os.path.join(BASE_DIR, "static", "generated")
os.makedirs(GENERATED_DIR, exist_ok=True)

SCRIPT = os.path.join(BASE_DIR, "buddy_cloud.py")
RESOLUTION = "768:1024"   # 竖版，适合手机/平板查看

app = Flask(__name__)

# 任务状态表（内存存储，部署在单实例下足够用）
JOBS = {}
JOBS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Token 解析：环境变量 > token.txt 文件
# （部署到 Render 等平台时，把 BUDDY_CLOUD_TOKEN 设为环境变量即可，
#   token 只存在于服务端，不会下发给访问网页的人）
# ---------------------------------------------------------------------------
def get_token() -> str:
    t = os.getenv("BUDDY_CLOUD_TOKEN", "").strip()
    if t:
        return t
    txt = os.path.join(BASE_DIR, "token.txt")
    if os.path.exists(txt):
        with open(txt, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


# ---------------------------------------------------------------------------
# 8 个适配 4-5 岁动手能力的通用手工活动类型（与主题解耦，可套任意主题）
# ---------------------------------------------------------------------------
ACTIVITIES = [
    ("底板涂鸦", "用粗蜡笔在底板上自由涂鸦打底，画出主题的大色块与线条"),
    ("撕纸拼贴", "用手把彩纸撕成不规则小块，拼贴出主题的主要形状"),
    ("手指点画", "用手指蘸颜料，点出主题上的圆点、斑点等细节装饰"),
    ("简单图形剪纸", "用钝头剪刀剪出三角形/正方形/半圆形等极简多边形进行拼接"),
    ("纸盘纸杯变身", "把纸盘或纸杯简单改造，变成主题里的主角造型"),
    ("泡泡膜拓印", "用泡泡膜蘸颜料拓印，压出有趣的肌理与花纹"),
    ("综合大拼贴", "把前面几种做法综合起来，完成一幅半立体大拼贴"),
    ("作品展示墙", "把作品布置成小型展示墙/迷你展览，拍照留念"),
]


# ---------------------------------------------------------------------------
# Prompt 构造：把 5 个回答 + 8 个活动类型 拼成 16 条生图提示词
# ---------------------------------------------------------------------------
def build_prompts(answers: dict) -> list:
    """返回 16 条 prompt，结构：{kind, idx, title, prompt}"""
    theme = (answers.get("theme") or "海洋小动物").strip() or "海洋小动物"
    kids = (answers.get("kids") or "一群").strip()
    fmt = (answers.get("format") or "幼儿园班级课").strip()
    personality = (answers.get("personality") or "活泼混合").strip()
    effect = (answers.get("effect") or "培养动手与专注").strip()

    prompts = []
    for i, (act_name, act_desc) in enumerate(ACTIVITIES, start=1):
        # (a) 论坛风参考图：像真实家长/老师随手拍的作品照
        ref_prompt = (
            f"真实照片，像家长或幼教在小红书、育儿论坛随手拍的，"
            f"4到5岁幼儿园小朋友用「{act_name}」做的「{theme}」主题手工作品。"
            f"自然光，手机原图，未修图，能看到稚拙笔触与童趣，"
            f"背景是普通家庭或教室桌面，照片真实感，无文字，无精细塑形"
        )
        # (b) 对应课堂效果图：按主题落地的成品效果图
        eff_prompt = (
            f"课堂成品效果图：{theme}主题创意美术课，第{i}课「{act_name}」。"
            f"面向{kids}个小朋友，教学形式：{fmt}，"
            f"适合{personality}的孩子，目标：{effect}。"
            f"具体做法：{act_desc}。A3卡纸底板，明显稚拙童趣手作质感，"
            f"边缘不整齐、手工痕迹明显，明亮均匀布光，45度俯拍，"
            f"照片真实感，无文字，无精细塑形"
        )
        prompts.append({
            "kind": "reference",
            "idx": i,
            "title": f"参考图 {i} · {act_name}",
            "prompt": ref_prompt,
        })
        prompts.append({
            "kind": "effect",
            "idx": i,
            "title": f"效果图 {i} · {act_name}",
            "prompt": eff_prompt,
        })
    return prompts


# ---------------------------------------------------------------------------
# 单张生图：调用 buddy_cloud.py，返回图片 URL
# ---------------------------------------------------------------------------
def generate_image_url(prompt: str, token: str) -> str:
    import subprocess
    import sys

    proc = subprocess.run(
        [
            sys.executable, SCRIPT, "image", prompt,
            "--resolution", RESOLUTION, "--revise", "1", "--token-stdin",
        ],
        input=token,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "生图失败").strip().splitlines()
        raise RuntimeError(err[-1][:300] if err else "生图失败")

    try:
        out = json.loads(proc.stdout)
    except Exception:
        raise RuntimeError("生图返回格式异常")

    url = out.get("result_url")
    if isinstance(url, list):
        url = url[0] if url else None
    if not url:
        raise RuntimeError("生图未返回图片地址")
    return url


def download_image(url: str, save_path: str):
    r = requests.get(url, timeout=90)
    r.raise_for_status()
    with open(save_path, "wb") as f:
        f.write(r.content)


# ---------------------------------------------------------------------------
# 后台生成任务
# ---------------------------------------------------------------------------
def run_job(job_id: str, answers: dict, token: str):
    prompts = build_prompts(answers)
    total = len(prompts)

    with JOBS_LOCK:
        JOBS[job_id]["total"] = total
        JOBS[job_id]["status"] = "running"

    results = []
    done_count = 0

    for item in prompts:
        record = {
            "kind": item["kind"],
            "idx": item["idx"],
            "title": item["title"],
            "prompt": item["prompt"],
            "image": None,
            "error": None,
        }
        try:
            url = generate_image_url(item["prompt"], token)
            ext = ".jpg"
            fname = f"{job_id}_{item['kind']}_{item['idx']}{ext}"
            save_path = os.path.join(GENERATED_DIR, fname)
            download_image(url, save_path)
            record["image"] = f"/static/generated/{fname}"
        except Exception as e:
            record["error"] = str(e)

        done_count += 1
        results.append(record)

        with JOBS_LOCK:
            JOBS[job_id]["done"] = done_count
            JOBS[job_id]["results"] = results

    with JOBS_LOCK:
        JOBS[job_id]["status"] = "finished"


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json(silent=True) or {}
    answers = {
        "kids": data.get("kids", ""),
        "format": data.get("format", ""),
        "personality": data.get("personality", ""),
        "effect": data.get("effect", ""),
        "theme": data.get("theme", ""),
    }
    token = get_token()
    if not token:
        return jsonify({"error": "未配置生图 token，请在部署环境变量 BUDDY_CLOUD_TOKEN 或本地 token.txt 中填入。"}), 400

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "pending",
            "total": 0,
            "done": 0,
            "results": [],
        }
    t = threading.Thread(target=run_job, args=(job_id, answers, token), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        return jsonify({
            "status": job["status"],
            "total": job["total"],
            "done": job["done"],
            "results": job["results"],
        })


@app.route("/static/generated/<path:filename>")
def generated_file(filename):
    return send_from_directory(GENERATED_DIR, filename)


# 本地直接运行
if __name__ == "__main__":
    # 把 token 写到 token.txt 方便本地调试（仅本地，已被 .gitignore 忽略）
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
