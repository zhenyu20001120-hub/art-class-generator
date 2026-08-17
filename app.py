# -*- coding: utf-8 -*-
"""
艺术课效果图生成器 · 后端
--------------------------------
交互问卷 -> 根据用户回答生成 8 张「论坛风实拍效果图」
（像家长/幼教在小红书、育儿论坛随手拍分享的真实作品照）。

支持把生成结果「保存为主题」到服务端，形成历史记录，
并可随时查看历史、一键重新修改。

生图调用打包进来的 buddy_cloud.py（腾讯云混元生图 3.0）。
"""

import os
import json
import uuid
import threading
from datetime import datetime, timezone

import requests
from flask import (
    Flask, request, render_template, jsonify, send_from_directory
)

# ---------------------------------------------------------------------------
# 路径与基础配置
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GENERATED_DIR = os.path.join(BASE_DIR, "static", "generated")
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
os.makedirs(GENERATED_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

SCRIPT = os.path.join(BASE_DIR, "buddy_cloud.py")
RESOLUTION = "768:1024"   # 竖版，适合手机/平板查看

app = Flask(__name__)

# 任务状态表（内存存储，部署在单实例下足够用）
JOBS = {}
JOBS_LOCK = threading.Lock()

# 历史记录锁（文件读写）
HISTORY_LOCK = threading.Lock()


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
# Prompt 构造：把 5 个回答 + 8 个活动类型 拼成 8 条「论坛风实拍效果图」提示词
# （已移除「课堂效果图」，只保留论坛风真实作品照风格）
# ---------------------------------------------------------------------------
def build_prompts(answers: dict) -> list:
    """返回 8 条 prompt，结构：{kind, idx, title, prompt}"""
    theme = (answers.get("theme") or "海洋小动物").strip() or "海洋小动物"
    kids = (answers.get("kids") or "一群").strip()
    fmt = (answers.get("format") or "幼儿园班级课").strip()
    personality = (answers.get("personality") or "活泼混合").strip()
    effect = (answers.get("effect") or "培养动手与专注").strip()

    prompts = []
    for i, (act_name, act_desc) in enumerate(ACTIVITIES, start=1):
        # 论坛风实拍效果图：像真实家长/老师随手拍分享的成品照
        prompt = (
            f"论坛风实拍效果图，像家长或幼教在小红书、育儿论坛随手拍分享的成品照："
            f"4到5岁幼儿园小朋友（约{kids}）在「{fmt}」上，"
            f"用「{act_name}」完成的「{theme}」主题手工作品，"
            f"适合{personality}的孩子，目标：{effect}。"
            f"具体做法：{act_desc}。"
            f"自然光，手机原图质感，未修图，稚拙童趣笔触清晰可见，"
            f"背景是普通家庭或教室桌面，照片级真实感，无文字，无精细塑形"
        )
        prompts.append({
            "kind": "forum",
            "idx": i,
            "title": f"论坛风实拍效果图 {i} · {act_name}",
            "prompt": prompt,
        })
    return prompts


# ---------------------------------------------------------------------------
# 历史记录（服务端 JSON 文件存储）
# ---------------------------------------------------------------------------
def load_history() -> list:
    with HISTORY_LOCK:
        if not os.path.exists(HISTORY_FILE):
            return []
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []


def write_history(lst: list):
    with HISTORY_LOCK:
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(lst, f, ensure_ascii=False, indent=2)
        os.replace(tmp, HISTORY_FILE)


def add_history_record(rec: dict) -> dict:
    lst = load_history()
    lst.insert(0, rec)          # 最新在最前
    write_history(lst)
    return rec


def get_history_record(rid: str):
    for r in load_history():
        if r.get("id") == rid:
            return r
    return None


def delete_history_record(rid: str) -> bool:
    lst = load_history()
    new = [r for r in lst if r.get("id") != rid]
    if len(new) != len(lst):
        write_history(new)
        return True
    return False


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
            "image": None,        # 本地下载路径（部署环境可能因重启/重部署而失效）
            "remote_url": None,   # 混元返回的原始远程地址，作为本地图失效后的回退
            "error": None,
        }
        try:
            url = generate_image_url(item["prompt"], token)
            record["remote_url"] = url   # 先存远程地址，保证历史记录里的图有兜底
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
            "answers": answers,
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


@app.route("/save", methods=["POST"])
def save_theme():
    """把一次已完成的生成结果保存为主题历史记录。"""
    data = request.get_json(silent=True) or {}
    job_id = (data.get("job_id") or "").strip()
    name = (data.get("name") or "").strip()

    with JOBS_LOCK:
        job = JOBS.get(job_id)

    if not job:
        return jsonify({"error": "任务不存在，请先生成再保存"}), 400
    if job.get("status") != "finished":
        return jsonify({"error": "生成尚未完成，请稍候再保存"}), 400

    answers = job.get("answers", {})
    results = job.get("results", [])

    if not name:
        name = (answers.get("theme") or "").strip() or "未命名主题"

    rec = {
        "id": uuid.uuid4().hex,
        "name": name,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "answers": answers,
        "results": results,
    }
    add_history_record(rec)
    return jsonify({"id": rec["id"], "name": rec["name"]})


@app.route("/history", methods=["GET"])
def history_list():
    """返回历史主题列表（不含完整 results，仅摘要信息）。"""
    items = []
    for r in load_history():
        res = r.get("results", [])
        cover = next((x.get("image") for x in res if x.get("image")), None)
        remote_cover = next((x.get("remote_url") for x in res if x.get("remote_url")), None)
        items.append({
            "id": r.get("id"),
            "name": r.get("name"),
            "created_at": r.get("created_at"),
            "theme": r.get("answers", {}).get("theme", ""),
            "cover": cover,
            "remote_cover": remote_cover,
            "count": len([x for x in res if x.get("image") or x.get("remote_url")]),
        })
    return jsonify(items)


@app.route("/history/<rid>", methods=["GET"])
def history_detail(rid):
    """返回某条历史主题的完整记录（含 5 个回答与生成结果）。"""
    r = get_history_record(rid)
    if not r:
        return jsonify({"error": "主题不存在"}), 404
    return jsonify(r)


@app.route("/history/<rid>", methods=["DELETE"])
def history_delete(rid):
    ok = delete_history_record(rid)
    return jsonify({"ok": ok})


@app.route("/static/generated/<path:filename>")
def generated_file(filename):
    return send_from_directory(GENERATED_DIR, filename)


# 本地直接运行
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
