# -*- coding: utf-8 -*-
"""魔法骑士 · 挂机助手（桌面 GUI）

用法：
    python knight_gui.py          或双击「启动桌面助手.bat」
"""
import os
import re
import sys
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import bot as botmod                       # noqa: E402

LOG_Q = queue.Queue()
FONT = ("Microsoft YaHei UI", 10)
FONT_B = ("Microsoft YaHei UI", 10, "bold")


class App:
    def __init__(self, root):
        self.root = root
        self.god = False
        self.running = False
        self.thread = None
        self.bot = None
        self.last_hud = ""
        root.title("魔法骑士 · 挂机助手")
        root.geometry("1000x640")
        root.minsize(900, 540)
        self._build()
        botmod.add_log_sink(LOG_Q.put)
        root.after(150, self._pump)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._log("就绪。点「无敌模式」开关键，再点「开始挂机」。")
        self._log("首次使用请先点「登录斗鱼」登录账号（登录态会保存在本机）。")
        self._log("数据目录：%s" % botmod.DATA_DIR, "dim")
        if not botmod.CHROME:
            self._log("!! 未检测到 Chrome/Edge，请先安装 Google Chrome", "warn")
        elif botmod.has_login():
            self._log("已检测到斗鱼登录态（acf_uid）", "dim")
        else:
            self._log("未检测到登录态，建议先点「登录斗鱼」", "warn")

    # ---------- UI ----------
    def _build(self):
        top = ttk.Frame(self.root, padding=(10, 10, 10, 6))
        top.pack(fill="x")

        ttk.Label(top, text="房间号", font=FONT).grid(row=0, column=0, sticky="w")
        self.rid = tk.StringVar(value="2561707")
        ttk.Entry(top, textvariable=self.rid, width=12, font=FONT).grid(row=0, column=1, padx=(4, 14))

        ttk.Label(top, text="局数(0=不限)", font=FONT).grid(row=0, column=2, sticky="w")
        self.rounds = tk.StringVar(value="0")
        ttk.Entry(top, textvariable=self.rounds, width=7, font=FONT).grid(row=0, column=3, padx=(4, 14))

        self.god_btn = tk.Button(top, text="无敌模式：关", width=15, font=FONT_B,
                                 bg="#e0e0e0", relief="raised", command=self.toggle_god)
        self.god_btn.grid(row=0, column=4, padx=(0, 10))

        self.start_btn = tk.Button(top, text="开始挂机", width=12, font=FONT_B,
                                   bg="#43a047", fg="white", command=self.start)
        self.start_btn.grid(row=0, column=5, padx=(0, 8))

        self.stop_btn = tk.Button(top, text="停止", width=8, font=FONT_B,
                                  state="disabled", command=self.stop)
        self.stop_btn.grid(row=0, column=6, padx=(0, 10))

        ttk.Button(top, text="登录斗鱼", width=10, command=self.login)\
            .grid(row=0, column=7, padx=(0, 6))
        ttk.Button(top, text="打开数据目录", width=12, command=self.open_dir)\
            .grid(row=0, column=8)

        # ---- 两局之间的随机等待（分钟）----
        ttk.Label(top, text="下一把随机间隔(分钟)", font=FONT)\
            .grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.gap_min = tk.StringVar(value="")
        self.gap_max = tk.StringVar(value="")
        gap_box = ttk.Frame(top)
        gap_box.grid(row=2, column=2, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Entry(gap_box, textvariable=self.gap_min, width=6, font=FONT).pack(side="left")
        ttk.Label(gap_box, text="~", font=FONT).pack(side="left", padx=4)
        ttk.Entry(gap_box, textvariable=self.gap_max, width=6, font=FONT).pack(side="left")
        ttk.Label(gap_box, text="分钟   （留空 / 0 = 立即开下一把）",
                  font=("Microsoft YaHei UI", 9), foreground="#666").pack(side="left", padx=(8, 0))

        tips = ttk.Label(top, text="无敌=真实免伤+只主动打精英/Boss(其余时间专心吃经验)；关闭=常规躲避流",
                         font=("Microsoft YaHei UI", 9), foreground="#666")
        tips.grid(row=3, column=0, columnspan=9, sticky="w", pady=(6, 0))

        self.status = tk.StringVar(value="状态：未运行")
        ttk.Label(self.root, textvariable=self.status, anchor="w", padding=(10, 4),
                  background="#f0f0f0", font=FONT_B).pack(fill="x", pady=(4, 0))

        frame = ttk.Frame(self.root, padding=(10, 8, 10, 10))
        frame.pack(fill="both", expand=True)
        self.text = tk.Text(frame, wrap="none", height=22, font=("Consolas", 10))
        sb = ttk.Scrollbar(frame, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        for tag, color in (("god", "#c62828"), ("win", "#2e7d32"),
                           ("warn", "#ef6c00"), ("dim", "#888")):
            self.text.tag_config(tag, foreground=color)

    # ---------- 日志 ----------
    def _log(self, msg, tag=None):
        self.text.insert("end", msg + "\n", tag)
        self.text.see("end")
        if int(self.text.index("end-1c").split(".")[0]) > 3000:
            self.text.delete("1.0", "500.0")

    def _pump(self):
        try:
            while True:
                line = LOG_Q.get_nowait()
                tag = None
                if "无敌" in line or "MAX" in line:
                    tag = "god"
                elif "胜" in line and "搭配存档" in line:
                    tag = "win"
                elif "失败" in line or "异常" in line or "不足" in line or "!!" in line:
                    tag = "warn"
                self._log(line, tag)
                m = re.search(r"=== 第 (\d+) 局 ===", line)
                if m:
                    self.last_hud = "第 %s 局" % m.group(1)
                m2 = re.search(r"局内 分钟=(\S+) 击杀=(\S+) 金币=(\S+).*血=(\S+) 无敌=(\S+)", line)
                if m2:
                    self.last_hud = ("剩余 %s | 击杀 %s | 金币 %s | 血 %s | 无敌 %s"
                                     % (m2.group(1), m2.group(2), m2.group(3),
                                        m2.group(4), m2.group(5)))
                m3 = re.search(r"距下一把还有 (\S+) 分 (\S+) 秒", line)
                if m3:
                    self.last_hud = "等待下一把 · 还有 %s 分 %s 秒" % (m3.group(1), m3.group(2))
                if "随机间隔" in line and "本局等待" in line:
                    self.last_hud = line.split("] ")[-1].strip()
                if "立即开下一把" in line:
                    self.last_hud = "立即开下一把"
                if self.running:
                    self.status.set("运行中 · %s | 无敌 %s"
                                    % (self.last_hud or "启动中", "开" if self.god else "关"))
        except queue.Empty:
            pass
        self.root.after(150, self._pump)

    # ---------- 控制 ----------
    def toggle_god(self):
        self.god = not self.god
        if self.god:
            self.god_btn.config(text="无敌模式：开", bg="#e53935", fg="white")
            self._log("[系统] 无敌模式：开启（真实免伤；只主动打精英/Boss，其余时间吃经验）", "god")
        else:
            self.god_btn.config(text="无敌模式：关", bg="#e0e0e0", fg="black")
            self._log("[系统] 无敌模式：关闭（常规躲避流）")
        # 运行中：只改标志，由 bot 线程在下一个 tick 重新注入（Playwright 不可跨线程调用）
        if self.bot:
            self.bot.god = self.god
            self._log("[系统] 切换将在 1 秒内生效")

    def start(self):
        if self.running:
            return
        if not botmod.CHROME:
            messagebox.showerror("缺少浏览器",
                                 "未检测到 Chrome/Edge。\n请先安装 Google Chrome，或设置环境变量 CHROME_PATH 指向 chrome.exe")
            return
        try:
            rid = int((self.rid.get() or "2561707").strip())
            rounds = int((self.rounds.get() or "0").strip())
        except ValueError:
            messagebox.showwarning("输入错误", "房间号和局数必须是数字")
            return
        # 随机间隔（分钟）：留空 = 0 = 立即开下一把
        try:
            gap_min = float((self.gap_min.get() or "0").strip() or 0)
            gap_max = float((self.gap_max.get() or "0").strip() or 0)
        except ValueError:
            messagebox.showwarning("输入错误", "随机间隔必须是数字（分钟），可以留空")
            return
        if gap_min < 0 or gap_max < 0:
            messagebox.showwarning("输入错误", "随机间隔不能是负数")
            return
        if gap_max <= 0 and gap_min > 0:      # 只填了前面一格 -> 当成固定间隔
            gap_max = gap_min
        if gap_min > gap_max:                 # 填反了就自动交换
            gap_min, gap_max = gap_max, gap_min
            self.gap_min.set("%g" % gap_min)
            self.gap_max.set("%g" % gap_max)
        self.running = True
        self.last_hud = ""
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status.set("启动中… 房间 %d | 无敌 %s" % (rid, "开" if self.god else "关"))
        gap_txt = ("立即开下一把" if gap_max <= 0
                   else "随机 %.1f~%.1f 分钟" % (gap_min, gap_max))
        self._log("[系统] 开始挂机：房间 %d，局数 %s，无敌 %s，间隔 %s"
                  % (rid, rounds or "不限", "开" if self.god else "关", gap_txt),
                  "god" if self.god else None)
        self.thread = threading.Thread(target=self._run,
                                       args=(rid, rounds, gap_min, gap_max), daemon=True)
        self.thread.start()

    def _run(self, rid, rounds, gap_min=0.0, gap_max=0.0):
        try:
            botmod.MagicKnightBot._kill_stale_chrome()
            self.bot = botmod.MagicKnightBot(rid=rid, rounds=rounds, god=self.god,
                                             gap_min=gap_min, gap_max=gap_max)
            self.bot.run()
        except Exception as e:
            self._log("[异常] %s" % e, "warn")
        finally:
            self.running = False
            self.root.after(0, self._on_finished)

    def _on_finished(self):
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status.set("状态：已停止")
        self._log("[系统] 已停止")
        self.bot = None

    def login(self):
        if self.running:
            messagebox.showinfo("提示", "挂机运行中，请先停止再登录")
            return
        self._log("[系统] 正在打开登录窗口…")
        threading.Thread(target=self._login_run, daemon=True).start()

    def _login_run(self):
        msg = None
        tag = "dim"
        try:
            botmod.open_login()
            ok = botmod.has_login()
            msg = "[系统] 登录流程结束（%s）" % ("已登录" if ok else "未检测到登录态")
            tag = "dim" if ok else "warn"
        except Exception as e:
            msg = "[异常] 登录失败：%s" % e
            tag = "warn"
        self.root.after(0, lambda m=msg, t=tag: self._log(m, t))

    def open_dir(self):
        try:
            os.makedirs(botmod.LOGDIR, exist_ok=True)
            os.startfile(botmod.DATA_DIR)
        except Exception as e:
            messagebox.showwarning("打不开", str(e))

    def stop(self):
        if self.bot:
            self.bot.request_stop()
            self._log("[系统] 停止中…（等待当前局收尾）")
        self.stop_btn.config(state="disabled")

    def on_close(self):
        if self.running and self.bot:
            if not messagebox.askokcancel("退出", "挂机还在运行，确定退出？"):
                return
            self.bot.request_stop()
        self.root.destroy()


def selftest():
    """打包自检：验证 Playwright driver 是否随包带上、Chrome 能否拉起。结果写入 selftest.txt"""
    out = []

    def p(s):
        out.append(str(s))
    ok = True
    try:
        p("frozen=%s" % botmod.IS_FROZEN)
        p("app_dir=%s" % botmod.APP_DIR)
        p("data_dir=%s" % botmod.DATA_DIR)
        p("chrome=%s" % (botmod.CHROME or "(none)"))
        cands = botmod.chrome_candidates()
        p("browser_candidates(rank 越小越优先):")
        for c in cands:
            p("  rank=%d prio=%d %s %s" % (c["rank"], c["prio"], c["exe"],
                                           ("[" + "、".join(c["marks"]) + "]") if c["marks"] else ""))
        if not cands:
            raise RuntimeError("未检测到 Chrome/Edge")
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        p("driver=started")
        ctx, exe = botmod.launch_persistent(pw, botmod.PROFILE + "_selftest", headless=False)
        p("launched_with=%s" % exe)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto("about:blank", timeout=30000)
        p("page=ok")
        ctx.close()
        pw.stop()
        p("driver=stopped")
    except Exception as e:
        ok = False
        p("FAIL: %r" % e)
    p("SELFTEST %s" % ("OK" if ok else "FAIL"))
    txt = "\n".join(out)
    dest = os.path.join(botmod.APP_DIR, "selftest.txt")
    try:
        with open(dest, "w", encoding="utf-8") as f:
            f.write(txt + "\n")
    except Exception:
        pass
    return ok


def cli_run(argv):
    """无窗口命令行模式：魔法骑士挂机助手.exe --run --god --rounds 1
    输出写入 <数据目录>/run.log（GUI 版无控制台，故落文件）"""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--god", action="store_true")
    ap.add_argument("--rid", type=int, default=2561707)
    ap.add_argument("--rounds", type=int, default=0)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--gap-min", type=float, default=0.0, help="两局之间随机等待下限（分钟）")
    ap.add_argument("--gap-max", type=float, default=0.0, help="两局之间随机等待上限（分钟）")
    a = ap.parse_args(argv)
    os.makedirs(botmod.DATA_DIR, exist_ok=True)
    logf = os.path.join(botmod.DATA_DIR, "run.log")

    def sink(s):
        try:
            with open(logf, "a", encoding="utf-8") as f:
                f.write(s + "\n")
        except Exception:
            pass

    botmod.add_log_sink(sink)
    botmod.log("=== CLI 启动 rid=%s rounds=%s god=%s gap=%s~%s ==="
               % (a.rid, a.rounds, a.god, a.gap_min, a.gap_max))
    botmod.MagicKnightBot._kill_stale_chrome()
    botmod.MagicKnightBot(rid=a.rid, rounds=a.rounds, god=a.god,
                          headless=a.headless,
                          gap_min=a.gap_min, gap_max=a.gap_max).run()


def cli_login():
    """命令行登录模式：魔法骑士挂机助手.exe --login（结果写 run.log）"""
    os.makedirs(botmod.DATA_DIR, exist_ok=True)
    logf = os.path.join(botmod.DATA_DIR, "run.log")

    def sink(s):
        try:
            with open(logf, "a", encoding="utf-8") as f:
                f.write(s + "\n")
        except Exception:
            pass

    botmod.add_log_sink(sink)
    botmod.log("=== 登录模式 ===")
    try:
        botmod.open_login()
    except Exception as e:
        botmod.log("登录失败：%s" % e)
        return 1
    botmod.log("登录态：%s" % ("已登录" if botmod.has_login() else "未检测到"))
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    if "--login" in sys.argv:
        sys.exit(cli_login())
    if "--run" in sys.argv:
        cli_run(sys.argv[1:])
        sys.exit(0)
    root = tk.Tk()
    App(root)
    root.mainloop()
