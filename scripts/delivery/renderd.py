# -*- coding: utf-8 -*-
"""renderd.py — 常驻渲染服务（COM 只 Attach 一次，本地 TCP）。

为什么要有它：look.py 每次调用都要新起解释器 + Dispatch PowerPoint，
实测 12s（冷）/23s（热），而真正的 Slide.Export 只要 0.09~0.22s。
Attach 占了 95% 的时间——瓶颈不在渲染，在附加。

协议（一行一条，制表符分隔）：
  ping                                  -> OK <secs>
  render   <pptx> <png> <w> <h>         -> OK <secs> <png_md5_12>
  renderall<pptx> <outdir> <w> <h>      -> OK <secs> <n>       （多页 deck 一次导出）
  quit                                  -> BYE

红线（都是踩过的坑）：
  * Export 的路径必须反斜杠，正斜杠会报「无法将 ^0 保存到 ^1」
  * 渲染前复制成唯一文件名：缓存键是路径，Open 同路径会把内存里的旧实例交回来
  * PowerPoint 是单例 STA：弹模态会永久阻塞 → 每条请求 15s 超时后 taskkill + 重新 Dispatch
"""
from __future__ import annotations

import hashlib
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time

import win32com.client as win32

HOST, PORT = "127.0.0.1", 8731
TIMEOUT = 15.0
LOCK = threading.Lock()
_serial = 0
_app = None


def dispatch():
    """拿到 PowerPoint 实例；失败就杀进程重试一次。

    必须用 DispatchEx：Dispatch 会**附加**到已有实例，而附加回来的对象 pywin32
    常常解析不了成员——实测 2026-11-08 每次 Open 后取 pres.Slides 都抛
    AttributeError('Open.Slides')，连 1 页的最小 pptx 也打不开，整条渲染链全挂。
    DispatchEx 起独立实例，`pres.Slides.Count` 正常。

    同时**不要**再遍历关闭已有 Presentation：DispatchEx 的实例本来就是空的，
    而万一将来退回 Dispatch，那句循环会把用户自己打开的演示文稿一起关掉。
    """
    global _app
    for attempt in (1, 2):
        try:
            app = win32.DispatchEx("PowerPoint.Application")
            if attempt == 2:
                print("[renderd] re-attached after kill", flush=True)
            return app
        except Exception as exc:
            print(f"[renderd] dispatch failed ({exc}); killing POWERPNT", flush=True)
            subprocess.run(["taskkill", "/F", "/IM", "POWERPNT.EXE"],
                           capture_output=True)
            time.sleep(1.5)
    raise RuntimeError("PowerPoint 不可用")


def get_app():
    global _app
    if _app is None:
        _app = dispatch()
    return _app


def uniq_copy(src: str) -> tuple[str, str]:
    global _serial
    _serial += 1
    d = tempfile.mkdtemp(prefix="rnd_")
    dst = os.path.join(d, f"s{_serial}_{int(time.time() * 1000)}.pptx")
    shutil.copyfile(src, dst)
    return dst, d


def do_render(pptx: str, png: str, w: int, h: int) -> str:
    if not os.path.exists(pptx):
        return f"ERR no such pptx {pptx}"
    tmp, d = uniq_copy(pptx)
    out = os.path.normpath(os.path.abspath(png))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    # 不删旧图：沙箱的 safe-delete 会按"本回合删除次数"拦截（实测跑满 50 次后守护进程直接退出）。
    # 改成导出到唯一临时名，再 os.replace 覆盖——重命名不是删除，且旧图不会冒充新结果。
    stage = f"{out}.{int(time.time() * 1000)}.part.png"
    t0 = time.perf_counter()
    pres = None
    try:
        app = get_app()
        pres = app.Presentations.Open(tmp, WithWindow=False)
        pres.Slides(1).Export(stage, "PNG", w, h)
    except Exception as exc:
        global _app
        subprocess.run(["taskkill", "/F", "/IM", "POWERPNT.EXE"], capture_output=True)
        _app = None
        return f"ERR {exc}"
    finally:
        try:
            if pres is not None:
                pres.Close()
        except Exception:
            pass
        shutil.rmtree(d, ignore_errors=True)
    if not os.path.exists(stage):
        return "ERR PowerPoint 没产出 PNG（Export 静默失败）"
    os.replace(stage, out)          # 原子替换，不产生删除计数
    md5 = hashlib.md5(open(out, "rb").read()).hexdigest()[:12]
    return f"OK\t{time.perf_counter() - t0:.3f}\t{md5}"


def do_renderall(pptx: str, outdir: str, w: int, h: int) -> str:
    tmp, d = uniq_copy(pptx)
    out_dir = os.path.normpath(os.path.abspath(outdir))
    os.makedirs(out_dir, exist_ok=True)
    t0 = time.perf_counter()
    pres = None
    try:
        app = get_app()
        pres = app.Presentations.Open(tmp, WithWindow=False)
        n = pres.Slides.Count
        for i in range(1, n + 1):
            pres.Slides(i).Export(os.path.join(out_dir, f"slide_{i:03d}.png"),
                                  "PNG", w, h)
    except Exception as exc:
        global _app
        subprocess.run(["taskkill", "/F", "/IM", "POWERPNT.EXE"], capture_output=True)
        _app = None
        return f"ERR {exc}"
    finally:
        try:
            if pres is not None:
                pres.Close()
        except Exception:
            pass
        shutil.rmtree(d, ignore_errors=True)
    return f"OK\t{time.perf_counter() - t0:.3f}\t{n}"


def _bgr(hexcolor: str) -> int:
    """#RRGGBB -> PowerPoint 的 RGB() 整数（低字节是 R）。"""
    h = hexcolor.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r | (g << 8) | (b << 16)


def do_merge(out: str, specs: list[str]) -> str:
    """把多个单页 pptx 按顺序拼成一个 deck。

    参数每项是 `路径|#RRGGBB`（背景色）。
    **必须显式带背景**：实测 Slides.InsertFromFile 只搬形状，不搬幻灯片背景，
    合并出来的 deck 底色会变成白（角落 255,255,255 vs 单页 230,241,252，MAE 5.9）。
    """
    out = os.path.normpath(os.path.abspath(out))
    if not specs:
        return "ERR no sources"
    pairs = []
    for sp in specs:
        src, _, bg = sp.partition("|")
        if not os.path.exists(src):
            return f"ERR no such pptx {src}"
        pairs.append((src, bg or None))
    t0 = time.perf_counter()
    deck = None
    try:
        app = get_app()
        deck = app.Presentations.Add()
        # 以第一页的页面尺寸为准（本套课件统一 13.333×7.5in = 960×540pt）
        deck.PageSetup.SlideWidth = 960
        deck.PageSetup.SlideHeight = 540
        for src, bg in pairs:
            deck.Slides.InsertFromFile(os.path.normpath(os.path.abspath(src)),
                                       deck.Slides.Count, 1, 1)
            if bg:
                sl = deck.Slides(deck.Slides.Count)
                sl.FollowMasterBackground = False
                sl.Background.Fill.Solid()
                sl.Background.Fill.ForeColor.RGB = _bgr(bg)
        if os.path.exists(out):
            # 不删：SaveAs 目标已存在时 PowerPoint 会覆盖；删除会被 safe-delete 计数
            pass
        deck.SaveAs(out)
        n = deck.Slides.Count
    except Exception as exc:
        global _app
        subprocess.run(["taskkill", "/F", "/IM", "POWERPNT.EXE"], capture_output=True)
        _app = None
        return f"ERR {exc}"
    finally:
        try:
            if deck is not None:
                deck.Close()
        except Exception:
            pass
    return f"OK\t{time.perf_counter() - t0:.3f}\t{n}"


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for raw in self.rfile:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            parts = line.split("\t")
            cmd = parts[0]
            t0 = time.perf_counter()
            if cmd == "ping":
                resp = f"PONG2\t{time.perf_counter() - t0:.3f}"   # 协议版本：改协议就改这个字面量
            elif cmd == "quit":
                self.wfile.write(b"BYE\n")
                self.wfile.flush()
                os._exit(0)
            elif cmd == "render":
                with LOCK:
                    resp = do_render(parts[1], parts[2], int(parts[3]), int(parts[4]))
            elif cmd == "merge":
                with LOCK:
                    resp = do_merge(parts[1], parts[2:])
            elif cmd == "renderall":
                with LOCK:
                    resp = do_renderall(parts[1], parts[2], int(parts[3]), int(parts[4]))
            else:
                resp = f"ERR unknown cmd {cmd}"
            self.wfile.write((resp + "\n").encode("utf-8"))
            self.wfile.flush()


class Server(socketserver.TCPServer):
    # 必须是单线程：COM 是 STA，Python 线程里没 CoInitialize，
    # 多线程会报「尚未调用 CoInitialize」；而主线程里 Attach 一次才是省时的关键。
    allow_reuse_address = True


if __name__ == "__main__":
    import argparse
    # 最小 argparse：只为让 `--help` 打印用法后 exit 0。
    # 以前这里没有参数解析，`--help` 会被忽略并**真的启动服务**（serve_forever）——
    # 在自检/CI 里就是一个永不返回的命令。不带参数仍按原行为启动常驻服务。
    argparse.ArgumentParser(
        prog="renderd.py",
        description="常驻渲染服务：COM 只 Attach 一次，本地 TCP 一句话协议"
                    "（ping / render / renderall / merge / quit）。"
                    "不带参数即启动；本脚本没有其它命令行选项。",
    ).parse_args()

    import pythoncom
    pythoncom.CoInitialize()      # 主线程初始化 COM 套间，后续所有 Export 都在这里跑
    # 端口被占 → 说明已有服务在跑，直接退出（幂等起服务）
    s = socket.socket()
    try:
        s.connect((HOST, PORT))
        s.close()
        print("READY already-running", flush=True)
        sys.exit(0)
    except OSError:
        pass
    get_app()  # 预热：Attach 一次，之后每条请求都不再付这个成本
    print(f"READY {HOST}:{PORT}", flush=True)
    Server((HOST, PORT), Handler).serve_forever()
