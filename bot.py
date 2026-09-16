# -*- coding: utf-8 -*-
"""
斗鱼「魔法骑士」H5 小游戏自动挂机 bot
====================================
原理：
  1. Playwright 持久化 Chrome 直接打开游戏面板页
     https://www.douyu.com/pages/vibe-lab-act202608-game/home?rid=<房间号>
  2. 点击「开始游戏」（DOM 按钮）
  3. 向页面注入 JS bot：读取 Cocos 引擎运行时状态（fight/player/enemy/dropProp），
     直接写 keyboardInput.dir 实现走位（躲怪 + 吸经验宝石）
  4. 升级弹窗(fightUpLevel)自动选中间技能卡；死亡/结算等弹窗自动点确认
  5. 一局结束后自动开下一局；全程状态落盘到 logs/*.jsonl

用法：
  python bot.py                    # 默认挂机，直到手动 Ctrl+C 或免费次数用尽
  python bot.py --rounds 3         # 只打 3 局
  python bot.py --rid 2561707      # 指定房间号
  python bot.py --no-move          # 只开局不走位（对照用）
  python bot.py --god              # 无敌模式（真实免伤，血条不掉）
"""
import os
import re
import sys
import json
import time
import shutil
import argparse
import datetime
import tempfile
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

ROOT = os.path.dirname(os.path.abspath(__file__))
IS_FROZEN = getattr(sys, "frozen", False)          # PyInstaller 打包后为 True
APP_DIR = os.path.dirname(sys.executable) if IS_FROZEN else ROOT


def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, ".wtest")
        open(p, "w").close()
        os.remove(p)
        return True
    except Exception:
        return False


# 数据目录：优先放到 exe 旁边（便于查看 logs / 迁移 chrome_profile），
# 不可写时（如装在 Program Files）回退到 %LOCALAPPDATA%\MagicKnight
DATA_DIR = APP_DIR if _writable(APP_DIR) else os.path.join(
    os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "MagicKnight")
PROFILE = os.path.join(DATA_DIR, "chrome_profile")
LOGDIR = os.path.join(DATA_DIR, "logs")


def find_chrome():
    """自动定位本机 Chrome / Edge，找不到返回空串"""
    cands = []
    env = os.environ.get("CHROME_PATH")
    if env:
        cands.append(env)
    try:
        import winreg
        for root, sub in ((winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
                          (winreg.HKEY_CURRENT_USER,
                           r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
                          (winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe")):
            try:
                with winreg.OpenKey(root, sub) as k:
                    cands.append(winreg.QueryValue(k, None))
            except Exception:
                pass
    except Exception:
        pass
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if not base:
            continue
        cands.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
        cands.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    for name in ("chrome", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    return ""


CHROME = find_chrome()

# 技能体系硬上限（源码 SKILL_CONFIG 实测：maxLevel:6, maxActiveNum:5, maxPassiveNum:2）
MAX_SKILL_LEVEL = 6      # 单技能满级（卡片显示 MAX / 终极形态）
MAX_ACTIVE_NUM = 5       # 主动技能槽（武器）上限
MAX_PASSIVE_NUM = 2      # 被动技能槽上限

# ---------------- 注入的走位 bot ----------------
# 策略：出现即拾取（用户要求：经验宝石绿/红出现就立刻去吃，血瓶/磁铁同吃）
#   1) 找 800 内最近的掉落物(dropProp)作为目标，全力奔袭
#   2) 仅被贴身(<150)或近(<330)敌人威胁时叠加逃逸向量，避免撞怪掉血
#   3) 升级/其它弹窗打开（游戏暂停）时停走
MOVE_BOT_JS = r"""
(opt) => {
  if (window.__BOT__) clearInterval(window.__BOT__);
  const G = window.__BOT_STATE = {dir:{x:0,y:0}, battles:0, notes:[],
                                  bprev:{}, bullets:[], lastBulletScan:0,
                                  god: !!(opt && opt.god)};
  const find = (pred) => { const r=[]; const w=n=>{ if(!n||!n.components) return;
      for(const c of n.components) if(pred(c)) r.push(c);
      for(const ch of (n.children||[])) w(ch); };
    const s=cc.director.getScene(); if(s) w(s); return r; };
  const getFight = () => find(c=>c.__classname__==='fight')[0] || null;
  window.__BOT_HELPERS = {find, getFight};

  window.__BOT__ = setInterval(() => {
    try {
      const f = getFight(); if (!f) { G.dir.x=0; G.dir.y=0; G.fight=false; return; }
      G.fight = true;
      // 弹窗（暂停）检测
      let popOpen = false;
      const s = cc.director.getScene();
      (function w(n){ if(!n || popOpen) return;
        if (n.name==='popupParent') {
          for (const c of n.children) if (c.activeInHierarchy && c.name!=='mask') popOpen = true;
          return;
        }
        for (const ch of n.children) w(ch);
      })(s);

      const now = Date.now();
      const pp = f.player.node.worldPosition;
      // 存活过滤（对齐游戏内判定：node 激活 + 未失败 + HP>0），
      // 否则对象池里已回收的敌人位置会残留，导致 bot 追着"幽灵怪"越跑越远
      const enemies = find(c=>c.__classname__==='enemy' && c.isInit).filter(e =>
        e.node && e.node.activeInHierarchy
        && !(e.state && e.state.lose)
        && !(e.data && typeof e.data.HP === 'number' && e.data.HP <= 0));
      // 精英 / Boss：源码判定为 data.bossLevel > 0（小 Boss），
      // >= spawn.finalBossLevel 为最终 Boss；isChallengeBoss 为挑战 Boss
      const isBossLike = (e) => (e.data && (e.data.bossLevel || 0) > 0)
                                || e.isChallengeBoss || !!e.finalBossDash;
      // ---- 敌方伤害弹道（实时抓取+躲避）----
      // 自动学习（每 ~100ms 扫描一次，跨帧缓存）：任何带 roleUid 且非玩家、
      // 非本体类的活动组件都视为敌方伤害实体（bulletItem/stonyItem/Boss 冲刺自动纳入）
      if (now - G.lastBulletScan > 100 || !G.bullets) {
        G.lastBulletScan = now;
        const SKIP_CLS = {'enemy':1, 'player':1, 'fight':1, 'dropProp':1,
                          'gameAnimation':1, 'animExt':1, 'colliderExt':1, 'rvo':1};
        const pUid2 = f.player.uid;
        const seenCls = G.threatSeen = G.threatSeen || {};
        const next = [];
        (function scan(n){ if(!n || !n.components) return;
          for (const c of n.components) {
            const cls = c.__classname__;
            if (!cls || SKIP_CLS[cls]) continue;
            if (!c.roleUid || c.roleUid === pUid2) continue;   // 我方实体忽略
            if (!c.node || !c.node.activeInHierarchy) continue;
            seenCls[cls] = (seenCls[cls] || 0) + 1;
            const q = c.node.worldPosition;
            next.push({uid:c.roleUid, cls:cls, x:q.x, y:q.y, node:c.node});
          }
          for (const ch of (n.children || [])) scan(ch);
        })(cc.director.getScene());
        G.bullets = next;
      }
      G.evBullets = G.bullets.length;
      // 敌弹逃逸向量：按"上一帧位置"算速度，对靠近玩家的弹做垂直切向规避
      let bx=0, by=0, bThreat=0;
      const prevMap = G.bprev;
      G.threats = [];                       // 实时弹道数据（供抓取落盘）
      for (const b of G.bullets) {
        const dx = b.x-pp.x, dy = b.y-pp.y, d = Math.hypot(dx,dy)||1;
        const pr = prevMap[b.uid];
        let vx=0, vy=0;
        if (pr) { vx = (b.x-pr.x)/0.12; vy = (b.y-pr.y)/0.12; }
        G.threats.push({cls:b.cls, x:Math.round(b.x), y:Math.round(b.y),
                        vx:Math.round(vx), vy:Math.round(vy),
                        d:Math.round(d)});
        if (d > 300) continue;
        // 来向接近判定（点积>0 → 朝向玩家）
        const closing = (vx*dx + vy*dy)/d > 40 || !pr;   // 无历史也当威胁（保守）
        if (!closing) continue;
        const w = (300-d)/300;
        // 切向规避：垂直弹道方向，取使玩家离开弹道的一侧
        const len = Math.hypot(vx,vy)||1;
        let tx=-vy/len, ty=vx/len;
        if (tx*dx + ty*dy < 0) { tx=-tx; ty=-ty; }
        bx += tx * w * w * 2.6; by += ty * w * w * 2.6;
        if (d < 260) bThreat = Math.max(bThreat, (260-d)/260);
      }
      G.bprev = {};
      for (const b of G.bullets) G.bprev[b.uid] = {x:b.x, y:b.y};

      // 本体排斥：挑战级大 Boss 强排斥；普通精英交给猎杀逻辑；
      // 普通小怪仅极端贴身(<90)轻排斥（用户：拾取经验优先于躲普通小怪）
      let ex=0, ey=0, nearEnemy=1e9;
      const elites = [];
      for (const e of enemies) {
        const p = e.node.worldPosition;
        const dx=pp.x-p.x, dy=pp.y-p.y, d=Math.hypot(dx,dy)||1;
        if (d < nearEnemy) nearEnemy = d;
        const isBoss = isBossLike(e);
        const isChal = e.isChallengeBoss;
        if (isBoss) elites.push(e);
        let k=0;
        if (isChal && d < 520) { k = 3.2 * (520-d)/520; }
        else if (!isBoss && d < 90) { k = 0.9 * (90-d)/90; }   // 普通小怪：贴着也优先捡经验
        else continue;
        ex += dx/d*k; ey += dy/d*k;
      }
      // ---- 掉落目标（分类独立，不互相短路）----
      // 分类依据 = 游戏内「带提示标签」的特殊掉落集合：
      //   dropBox/dropEquipBox/dropRoleChip/dropSkillChip/dropFireJet/healPotion/pickup
      // 优先级：宝箱(box/chest) > 稀有随机buff(磁铁pickup·火喷fireJet·血瓶healPotion·碎片chip)
      //         > 经验(exp1~3)/金币(gold)
      // ⚠️ 抽搐根因：玩家 moveSpeed 290~350，注入 tick 120ms -> 单帧位移 35~42 单位，
      //   比拾取判定半径还大。贴身时每帧都会冲过头再回头 -> 原地来回摆。
      //   对策：① 贴身死区 ② 卡住黑名单 ③ 软带降权 ④ 目标粘滞
      const drops = find(c=>c.__classname__==='dropProp' && c.isInit !== false);
      const dropTypes = G.dropTypes = G.dropTypes || {};
      const RE_CHEST = /box|chest/i;
      const RE_RARE  = /pickup|magnet|firejet|fire_jet|healpotion|heal_potion|chip|buff/i;
      const DZ = 40;                          // 贴身死区：进入即停手，交给游戏自身拾取
      const nearMap = G.nearMap = G.nearMap || {};
      const skipMap = G.skipMap = G.skipMap || {};
      // 卡住判定：贴到 70 以内 900ms 仍未消失 -> 认为该掉落捡不到，忽略 3 秒后再试
      const reachable = (type, x, y, d) => {
        const k = type + ':' + Math.round(x/60) + ',' + Math.round(y/60);
        if (skipMap[k] && now < skipMap[k]) return false;
        if (d > 70) { delete nearMap[k]; return true; }
        const s = nearMap[k] || (nearMap[k] = now);
        if (now - s > 900) { skipMap[k] = now + 3000; delete nearMap[k]; return false; }
        return true;
      };
      let cd=1e9, cx=0, cy=0;                 // 宝箱
      let rd=1e9, rx=0, ry=0;                 // 稀有（随机buff 类）
      let ggD=1e9, ggx=0, ggy=0;              // 经验/金币
      for (const dr of drops) {
        const p=dr.node.worldPosition;
        const ddx=p.x-pp.x, ddy=p.y-pp.y, d=Math.hypot(ddx,ddy);
        const t = String(dr.type || (dr.node && dr.node.name) || '?');
        dropTypes[t] = (dropTypes[t]||0) + 1;
        if (!(d > DZ)) continue;              // 贴身死区：不再推进
        const ux = ddx/d, uy = ddy/d;
        if (RE_CHEST.test(t)) {
          if (reachable('chest', p.x, p.y, d) && d < cd) { cd=d; cx=ux; cy=uy; }
        } else if (RE_RARE.test(t)) {
          if (reachable('rare', p.x, p.y, d) && d < rd) { rd=d; rx=ux; ry=uy; }
        } else {
          if (d < ggD) { ggD=d; ggx=ux; ggy=uy; }
        }
      }
      G.chest = cd < 1e9 ? Math.round(cd) : 0;
      G.rare  = rd < 1e9 ? Math.round(rd) : 0;
      G.buff  = G.rare;                       // 兼容旧字段名（GUI/日志）
      G.nearestDrop = ggD < 1e9 ? Math.round(ggD) : 0;
      if (Object.keys(skipMap).length > 300) {  // 防内存无限增长
        for (const k in skipMap) if (skipMap[k] < now) delete skipMap[k];
      }
      const hasChest = cd < 1e9, hasRare = rd < 1e9, hasExp = ggD < 1e9;
      // 软带降权：d>=180 全速，越近权重越低（配合死区，逼近过程不飘）
      const near = (d) => Math.min(1, d / 180);
      // 目标粘滞：同一目标 800ms 内锁定，避免两个等距目标来回切换
      const goalDir = (type, ux, uy, d) => {
        const gx0 = pp.x + ux*d, gy0 = pp.y + uy*d;
        const prev = G.goal;
        let g;
        if (prev && prev.type === type && now < prev.until
            && Math.hypot(prev.x - gx0, prev.y - gy0) < 240) g = prev;
        else { g = {type: type, x: gx0, y: gy0, until: now + 800}; G.goal = g; }
        const vx = g.x - pp.x, vy = g.y - pp.y, vd = Math.hypot(vx, vy) || 1;
        return {ux: vx/vd, uy: vy/vd, d: vd};
      };

      // ---- 精英猎杀向量：主动近身环绕输出清精英（仍受弹幕/贴脸保护优先）----
      let mdx=0, mdy=0, eliteMode=false, eliteD=1e9;
      if (elites.length && !popOpen) {
        let e0=null, ed=1e9;
        for (const e of elites) {
          const p=e.node.worldPosition;
          const d=Math.hypot(p.x-pp.x, p.y-pp.y)||1;
          if (d<ed) { ed=d; e0=p; }
        }
        if (e0) {
          eliteMode = true; eliteD = ed;
          const rx=pp.x-e0.x, ry=pp.y-e0.y;         // 背离精英方向
          const rd=Math.hypot(rx,ry)||1;
          const ux=rx/rd, uy=ry/rd;                 // 径向（向外）
          const tx=-uy, tyy=ux;                     // 切向
          if (ed > 300)      { mdx = -ux*1.6 + tx*0.8; mdy = -uy*1.6 + tyy*0.8; } // 靠近
          else if (ed < 170) { mdx = ux*1.8 + tx*0.6;  mdy = uy*1.8 + tyy*0.6; } // 退开
          else               { mdx = tx*2.0 - ux*0.25; mdy = tyy*2.0 - uy*0.25; } // 环绕输出
          if (bThreat > 0.42 || nearEnemy < 120) { mdx *= 0.3; mdy *= 0.3; }      // 紧急先撤
        }
      }
      G.eliteMode = eliteMode; G.eliteD = Math.round(eliteD);

      let dx, dy;
      if (G.god && !popOpen) {
        // ---- 无敌模式（v2）：不再无脑追小怪 ----
        // 规则：只主动找 精英/Boss(bossLevel>0)；其余时间专心吃经验/宝箱
        //  1) 场上有 Boss：螺旋逼近 -> 环绕输出（保持 130~240 的输出距离）
        //  2) 无 Boss：奔向最近的经验/血瓶掉落（不限距离，优先于任何走位）
        //  3) 宝箱永远最高优先
        let bx2=0, by2=0, bd=1e9, bosses=0;
        for (const e of enemies) {
          if (!isBossLike(e)) continue;
          bosses++;
          const p = e.node.worldPosition;
          const d = Math.hypot(p.x-pp.x, p.y-pp.y)||1;
          if (d < bd) { bd = d; bx2 = p.x; by2 = p.y; }
        }
        G.bosses = bosses; G.bossD = bd < 1e9 ? Math.round(bd) : 0;
        G.elites = bosses;
        dx = 0; dy = 0;
        if (bosses) {
          const rx = pp.x-bx2, ry = pp.y-by2, rd = Math.hypot(rx,ry)||1;
          const ux = rx/rd, uy = ry/rd;                 // 径向（背离 Boss）
          if (bd > 240)      { dx = -ux*1.8 - uy*0.9; dy = -uy*1.8 + ux*0.9; } // 螺旋逼近
          else if (bd < 130) { dx =  ux*1.3 - uy*0.7;  dy =  uy*1.3 + ux*0.7; } // 稍退防推挤
          else               { dx = -uy*1.6 - ux*0.2;  dy =  ux*1.6 - uy*0.2; } // 环绕输出
        }
        // 掉落物主动吃（分级 + 粘滞 + 近距衰减）：
        //   宝箱 > 稀有随机buff(磁铁/火喷/血瓶/碎片) > 经验/金币
        const gW = bosses ? 1.4 : 2.8;
        let hunt = 'idle';
        if (hasChest) {
          const g = goalDir('chest', cx, cy, cd), k = 2.6*near(g.d);
          dx += g.ux*k; dy += g.uy*k; hunt = 'chest';
        } else if (hasRare && rd < 1600) {
          const g = goalDir('rare', rx, ry, rd), k = gW*near(g.d);
          dx += g.ux*k; dy += g.uy*k; hunt = 'rare';
        } else if (hasExp && ggD < 4000) {
          const g = goalDir('exp', ggx, ggy, ggD), k = gW*near(g.d);
          dx += g.ux*k; dy += g.uy*k; hunt = 'exp';
        }
        if (bosses) hunt = 'boss+' + hunt;
        G.hunt = {enemies: enemies.length, nearest: Math.round(nearEnemy),
                  bosses: bosses, bossD: G.bossD,
                  chest: G.chest, rare: G.rare, exp: G.nearestDrop, target: hunt};
      }
      else if (popOpen) { dx=0; dy=0; }
      else if (bThreat > 0.42) {
        // 弹幕接近：侧移为主（伤害性技能必须躲）
        dx = bx*1.1 + ex*0.3; dy = by*1.1 + ey*0.3;
      }
      else if (eliteMode) {
        // 主动猎杀精英（解锁升级）
        dx = mdx + ex*0.2 + bx*0.3; dy = mdy + ey*0.2 + by*0.3;
      }
      else if (hasChest) {
        // 宝箱：最高优先（粘滞 + 近距衰减，避免贴身抽搐）
        const g = goalDir('chest', cx, cy, cd);
        dx = g.ux*3.0*near(g.d) + ex*0.3; dy = g.uy*3.0*near(g.d) + ey*0.3;
      }
      else if (hasRare && rd < 1600) {
        // 稀有随机buff（磁铁/火喷/血瓶/碎片）：专程去拿，优先级高于经验
        const g = goalDir('rare', rx, ry, rd);
        dx = g.ux*2.8*near(g.d) + ex*0.3 + bx*0.5;
        dy = g.uy*2.8*near(g.d) + ey*0.3 + by*0.5;
      }
      else if (hasExp && ggD < 1100) {
        // 经验/金币：优先于躲普通小怪，直接奔（近的贴脸也吃）
        const g = goalDir('exp', ggx, ggy, ggD);
        dx = g.ux*3.0*near(g.d) + ex*0.3 + bx*0.4;
        dy = g.uy*3.0*near(g.d) + ey*0.3 + by*0.4;
      } else {
        // 无目标：被大群围(<90)才轻规避，否则等待
        dx = ex + bx; dy = ey + by;
      }
      const m=Math.hypot(dx,dy);
      if (m>0.001) { G.dir.x=dx/m; G.dir.y=dy/m; } else { G.dir.x=0; G.dir.y=0; }
      if (f.keyboardInput) { f.keyboardInput.dir.x=G.dir.x; f.keyboardInput.dir.y=G.dir.y; }
      G.enemies=enemies.length; G.nearest=Math.round(nearEnemy);
      G.minute=f.minute;
      G.battleId=f.launchData && f.launchData.battleId;
      G.leftReviveNum=f.launchData && f.launchData.leftReviveNum;
    } catch(e) { G.err=String(e).slice(0,120); }
  }, 120);
  return 'ok';
}
"""

# ---------------- 注入的无敌模式 ----------------
# 机制（2026-09-09 逆向 player 原型链源码确认）：
#   setInvincible(t){ state.invincibleUntil = max(state.invincibleUntil||0, Date.now()+1000*t) }
#   isInvincible(){ return state.invincibleUntil > Date.now() }
#   setHP() 内首行判定：if (减血 && target.isInvincible()) return 0;   -> 伤害被完全吞掉
# 因此持续维持 state.invincibleUntil 即可真免伤（不是改血量，HP 条仍是正常数值）。
# 实测：5.5 分钟、敌人贴脸 8 距离，HP 恒定不掉，未死亡；可越过原本 4-5 分钟的暴毙点。
GOD_INTERVAL_MS = 300
GOD_MODE_JS = r"""
(opt) => {
  const on = !(opt && opt.god === false);
  if (window.__GOD_T__) { clearInterval(window.__GOD_T__); window.__GOD_T__ = null; }
  window.__GOD = null;
  if (!on) return 'off';
  const findFight = () => { let r=null; const w=n=>{ if(r||!n) return;
      for(const c of (n.components||[])) if(c.__classname__==='fight'){r=c;return;}
      for(const ch of (n.children||[])) w(ch); };
    w(cc.director.getScene()); return r; };
  const G = window.__GOD = {on:false, calls:0, hpMin:null, diedTicks:0, err:null};
  if (window.__GOD_T__) clearInterval(window.__GOD_T__);
  window.__GOD_T__ = setInterval(() => {
    try {
      const f = findFight(); if (!f || !f.player) return;
      const p = f.player;
      if (typeof p.setInvincible === 'function') p.setInvincible(60);
      else if (p.state) p.state.invincibleUntil = Date.now() + 60000;
      G.on = true; G.calls++;
      const hp = p.data && p.data.HP;
      if (typeof hp === 'number' && (G.hpMin === null || hp < G.hpMin)) G.hpMin = hp;
      if (p.state && p.state.lose) G.diedTicks++;
    } catch(e) { G.err = String(e).slice(0,120); }
  }, %d);
  return 'ok';
}
""" % GOD_INTERVAL_MS

# 节点世界坐标 -> 页面 CSS 坐标
JS_WORLD_TO_CSS = r"""
(pos) => {
  const vis = cc.view.getVisibleSize();
  const canvas = document.querySelector('canvas');
  const r = canvas.getBoundingClientRect();
  return [r.left + pos.x / vis.width * r.width,
          r.top + (vis.height - pos.y) / vis.height * r.height];
}
"""

# 列出弹窗内全部按钮（含文字），由 Python 决策，避免误点付费项
JS_POPUP_TARGET = r"""
(args) => {
  const s = cc.director.getScene(); let pp = null;
  (function w(n){ if(pp) return; if(n.name==='popupParent'){pp=n;return;}
    for(const c of n.children) w(c); })(s);
  if (!pp) return null;
  const pops = pp.children.filter(c=>c.activeInHierarchy);
  if (!pops.length) return null;
  let node = pops.find(c=>c.name===args.popup);
  if (!node) node = pops[0];
  if (args.child) {
    const dig=(n,d)=>{ if(!n.children||!n.children.length) return null;
      for(const c of n.children){ if(c.name===args.child && c.activeInHierarchy) return c;
        const r=dig(c,d+1); if(r) return r; } return null; };
    const ch = dig(node,0); if (ch) node = ch;
    const wp = node.worldPosition;
    return {buttons:[{name:node.name, wp:{x:wp.x,y:wp.y}, label:childLabel(node)}]};
  }
  const buttons = [];
  function childLabel(n){ let s='';
    (function w3(m){for(const c of (m.components||[])) if(typeof c.string==='string') s+=c.string;
      for(const c of (m.children||[])) w3(c);})(n); return s.trim(); }
  (function w2(n){
    const hasBtn=(n.components||[]).some(c=>c.__classname__==='cc.Button' || c instanceof cc.Button);
    if (hasBtn && n.activeInHierarchy) {
      const wp=n.worldPosition;
      buttons.push({name:n.name, wp:{x:wp.x,y:wp.y}, label:childLabel(n)});
    }
    for(const c of (n.children||[])) w2(c);
  })(node);
  return {buttons: buttons};
}
"""

JS_ACTIVE_POPUPS = r"""
() => {
  const s = cc.director.getScene(); let pp=null;
  (function w(n){ if(pp) return; if(n.name==='popupParent'){pp=n;return;}
    for(const c of n.children) w(c); })(s);
  if (!pp) return [];
  return pp.children.filter(c=>c.activeInHierarchy).map(c=>c.name);
}
"""

# 战斗内关键节点世界坐标（stopBtn/finalSkillButton/击杀金币/计时）
JS_HUD = r"""
() => {
  let fight=null;const w=n=>{if(fight)return;for(const c of(n.components||[]))
    if(c.__classname__==='fight'){fight=n;return;}
    for(const ch of(n.children||[]))w(ch);};w(cc.director.getScene());
  if(!fight) return null;
  const find=(name)=>{let r=null;(function w2(n){if(r)return;if(n.name===name){r=n;return;}
    for(const c of (n.children||[])) w2(c);})(fight);return r;};
  const labelStr=(n)=>{ if(!n) return ''; let s='';
    (function w3(m){for(const c of (m.components||[])) if(typeof c.string==='string') s+=c.string;
      for(const c of (m.children||[])) w3(c);})(n); return s.trim(); };
  const wp=(n)=>{ if(!n) return null; const p=n.worldPosition; return {x:p.x,y:p.y}; };
  return {
    stopBtn: wp(find('stopBtn')),
    finalSkillButton: wp(find('finalSkillButton')),
    finalSkillCount: labelStr(find('countBadge')),
    kills: labelStr(find('killCount')),
    gold: labelStr(find('goldCount')),
    timer: labelStr(find('secondLabel')),
    level: labelStr(find('levelLabel')),
  };
}
"""

JS_HAS_FIGHT = """() => {
  if (!window.cc || !cc.director.getScene) return false;
  let found = false;
  const w = (n) => { if (found || !n) return;
    for (const c of (n.components || [])) if (c.__classname__ === 'fight') { found = true; return; }
    for (const ch of (n.children || [])) w(ch); };
  const s = cc.director.getScene(); if (s) w(s);
  return found;
}"""

# 技能实时状态（技能档案抓取）：已拥有技能 + 本轮候选
JS_SKILLS_STATE = r"""
() => {
  let fc=null; const w=n=>{if(fc)return;for(const c of(n.components||[]))
    if(c.__classname__==='fight'){fc=c;return;}for(const ch of(n.children||[]))w(ch);};
  w(cc.director.getScene());
  if(!fc) return null;
  return {active: (fc.serverActiveSkills||[]).map(a=>({type:a.skillType, name:a.skillName||null,
           level:a.level, num:a.num, max:a.max})),
          choices: (fc.serverSkillChoices||[]).map(c=>({type:c.skillType, num:c.num}))};
}
"""

# 升级弹窗三张候选卡的文字
JS_SKILL_CARDS = r"""
() => {
  const s = cc.director.getScene(); let pp = null;
  (function w(n){ if(pp) return; if(n.name==='popupParent'){pp=n;return;}
    for(const c of n.children) w(c); })(s);
  if (!pp) return null;
  const pop = pp.children.find(c=>c.name==='fightUpLevel' && c.activeInHierarchy);
  if (!pop) return null;
  const textOf = (n) => { let t='';
    (function w2(m){ for(const c of (m.components||[])) if(typeof c.string==='string') t+=c.string;
      for(const c of (m.children||[])) w2(c); })(n); return t.trim(); };
  const bg = pop.getChildByName('bg');
  const cards = bg && bg.getChildByName('cards');
  if (!cards) return null;
  // 卡片等级状态（源码 fightUpLevel.renderLevelStatus）：
  //   statusNewNode 节点名 = 'new'  -> 新技能（level 0）
  //   statusMaxNode 节点名 = 'max'  -> level 已达 maxLevel-1（选后到 6 级）
  //   starNodes     节点名 = 'star0..starN' -> 点亮颗数 = level+1（level 1~4）
  const badgeOf = (n) => {
    let badge = null, stars = 0;
    (function w3(m){
      for (const c of (m.children||[])) {
        if (!c.activeInHierarchy) continue;
        if (c.name === 'new' || c.name === 'statusNew') badge = badge || 'new';
        else if (c.name === 'max' || c.name === 'statusMax') badge = 'max';
        else if (/^star-?\d+$/.test(c.name)) stars++;
        w3(c);
      }
    })(n);
    return {badge: badge, stars: stars};
  };
  const out = [];
  for (const card of ['skillCard0','skillCard1','skillCard2']) {
    const node = cards.getChildByName(card);
    if (node && node.activeInHierarchy) {
      const wp = node.worldPosition;
      const b = badgeOf(node);
      out.push({idx: parseInt(card.slice(-1), 10), text: textOf(node),
                max: b.badge === 'max', badge: b.badge, stars: b.stars,
                wp: {x: wp.x, y: wp.y}});
    }
  }
  return out;
}
"""

# 技能上下文：已有主动技能(名/等级) + 被动数量 + 本轮候选
# 用于把「卡片文本」映射到「该技能当前等级」——这是判定满级浪费/升6质变的关键
JS_CHOICES = r"""
() => {
  let fc=null; const w=n=>{if(fc)return;for(const c of(n.components||[]))
    if(c.__classname__==='fight'){fc=c;return;}for(const ch of(n.children||[]))w(ch);};
  w(cc.director.getScene());
  if(!fc) return null;
  const actives = (fc.serverActiveSkills||[]).map(a=>({
    name: a.skillName || null, type: a.skillType, level: a.level || 0}));
  let passiveNum = 0;
  try { passiveNum = Object.keys(fc.player.passiveSkill||{}).length; } catch(e) {}
  const chs = (fc.serverSkillChoices||[]).map(c => ({
    type: c.skillType, num: c.num,
    have: actives.some(a=>a.type===c.skillType)}));
  return {choices: chs, actives: actives, passiveNum: passiveNum};
}
"""

JS_SNAPSHOT = r"""
() => {
  const G = window.__BOT_STATE || {};
  const f = window.__BOT_HELPERS && __BOT_HELPERS.getFight();
  const o = {ts: Date.now()/1000, enemies: G.enemies, nearest: G.nearest,
             nearestDrop: G.nearestDrop,
             minute: G.minute, battleId: G.battleId, leftReviveNum: G.leftReviveNum,
             dir: G.dir, popups: [], fight: !!f, threats: (G.threats||[]).slice(0,20),
             threatSeen: G.threatSeen ? Object.assign({}, G.threatSeen) : {},
             dropTypes: G.dropTypes ? Object.assign({}, G.dropTypes) : {},
             eliteMode: !!G.eliteMode, eliteD: G.eliteD, elites: G.elites,
             chest: G.chest, buff: G.buff, rare: G.rare,
             bosses: G.bosses, bossD: G.bossD, hunt: G.hunt};
  try {
    if (f) {
      const p = f.player.node.worldPosition;
      o.pos = [Math.round(p.x), Math.round(p.y)];
      o.hp = f.player.data ? f.player.data.HP : null;
      o.maxhp = (f.player.data && f.player.data.default) ? f.player.data.default.HP : null;
      try { o.inv = f.player.isInvincible ? f.player.isInvincible() : null; } catch(e) {}
      if (window.__GOD) o.god = {on: window.__GOD.on, calls: window.__GOD.calls,
                                 hpMin: window.__GOD.hpMin, died: window.__GOD.diedTicks};
      o.skills = f.activeSkills ? f.activeSkills.length : null;
      o.stage = f.launchData ? f.launchData.stage : null;
      // 经验条进度(0-100) 与 待处理升级轮（诊断 12 级卡升级问题）
      try {
        o.exp = f.expProgressBar ? Math.round((f.expProgressBar.progress || 0) * 100) : null;
      } catch(e) { o.exp = null; }
      try {
        o.upgradeRoundId = f.upgradeRoundId;
        o.skillChoiceRoundId = f.skillChoiceRoundId;
      } catch(e) {}
    }
    // 敌方/全弹道实体统计（bulletItem/stonyItem 疑似弹道，用于调试躲避）
    const find2 = (pred) => { const r=[]; const w=n=>{ if(!n||!n.components) return;
        for(const c of n.components) if(pred(c)) r.push(c);
        for(const ch of (n.children||[])) w(ch); };
      const s=cc.director.getScene(); if(s) w(s); return r; };
    const bis = find2(c=>c.__classname__==='bulletItem');
    const sis = find2(c=>c.__classname__==='stonyItem');
    o.bullets = {bi: bis.length, si: sis.length};
    if (bis.length || sis.length) {
      const it = (bis[0] || sis[0]);
      const ch=[]; let e=it.node; while(e && ch.length<5){ ch.push(e.name); e=e.parent; }
      const fd={}; for(const k of Object.keys(it)){ if(k.startsWith('_')) continue;
        const v=it[k]; if(typeof v==='number'||typeof v==='boolean'||typeof v==='string') fd[k]=v; }
      const q=it.node.worldPosition;
      o.bulletSample = {cls: it.__classname__, chain: ch.join('>'),
                        pos:[Math.round(q.x),Math.round(q.y)], f: JSON.stringify(fd).slice(0,200)};
    }
    const s = cc.director.getScene(); let pp=null;
    (function w(n){ if(pp) return; if(n.name==='popupParent'){pp=n;return;}
      for(const c of n.children) w(c); })(s);
    if (pp) o.popups = pp.children.filter(c=>c.activeInHierarchy).map(c=>c.name);
  } catch(e) { o.err = String(e).slice(0,80); }
  return o;
}
"""

# 开始游戏按钮状态：免费 or 骑士币价格（免费次数用尽后显示 1000）
JS_START_INFO = r"""
() => {
  [...document.querySelectorAll('*')]
    .filter(e=>e.scrollHeight>e.clientHeight+50 && e.clientHeight>200)
    .forEach(e=>e.scrollTop=e.scrollHeight);
  const items = [...document.querySelectorAll('button,a')].filter(e=>
    /Action-module_item/.test(e.className||''));
  if (!items.length) return null;
  const btn = items.find(e => e.tagName === 'BUTTON') || items[items.length-1];
  const t = (btn.innerText||'').replace(/\s+/g,'');
  if (/免费/.test(t)) return {free: true};
  const m = t.match(/(\d+)/);
  if (m) return {free: false, cost: parseInt(m[1], 10), text: t.slice(0,20)};
  return {free: false, cost: 0, text: t.slice(0,20)};
}
"""

CLICK_START_JS = r"""
() => {
  [...document.querySelectorAll('*')]
    .filter(e=>e.scrollHeight>e.clientHeight+50 && e.clientHeight>200)
    .forEach(e=>e.scrollTop=e.scrollHeight);
  const items = [...document.querySelectorAll('button,a')].filter(e=>
    /Action-module_item/.test(e.className||''));
  const btn = items.find(e => e.tagName === 'BUTTON') || (items.length ? items[items.length-1] : null);
  if (!btn) return 'nf';
  btn.click();
  return 'ok';
}
"""


def ts():
    return datetime.datetime.now().strftime("%H:%M:%S")


# GUI / 外部日志订阅（线程安全：回调由调用方自行入队）
_LOG_SINKS = []


def add_log_sink(fn):
    if fn not in _LOG_SINKS:
        _LOG_SINKS.append(fn)


def log(*a):
    s = "[%s] %s" % (ts(), " ".join(str(x) for x in a))
    try:
        if sys.stdout is not None:      # 打包为窗口程序时 stdout 为 None
            print(s, flush=True)
    except Exception:
        pass
    for fn in list(_LOG_SINKS):
        try:
            fn(s)
        except Exception:
            pass


class MagicKnightBot:
    def __init__(self, rid=2561707, rounds=0, headless=False, no_move=False, god=False):
        self.panel_url = ("https://www.douyu.com/pages/vibe-lab-act202608-game/home"
                          "?rid=%d&isAnchorSide=0" % rid)
        self.rounds = rounds
        self.headless = headless
        self.no_move = no_move
        self.god = god
        self._stop = False            # GUI 停止按钮置 True，主循环各检查点退出
        os.makedirs(LOGDIR, exist_ok=True)

    def request_stop(self):
        self._stop = True

    # ---------- 基础 ----------
    @staticmethod
    def _kill_stale_chrome():
        return kill_stale_chrome()

    def start_browser(self):
        if not CHROME:
            raise RuntimeError("未检测到 Chrome/Edge 浏览器，请先安装 Google Chrome，"
                               "或设置环境变量 CHROME_PATH 指向 chrome.exe")
        self.pw = sync_playwright().start()
        last_err = None
        for attempt in range(3):
            try:
                self.ctx = self.pw.chromium.launch_persistent_context(
                    user_data_dir=PROFILE, executable_path=CHROME, headless=self.headless,
                    viewport={"width": 1500, "height": 940},
                    args=["--disable-blink-features=AutomationControlled", "--no-first-run",
                          "--no-default-browser-check"],
                    ignore_default_args=["--enable-automation"])
                break
            except Exception as e:
                last_err = e
                log("Chrome 启动失败（第 %d 次）: %s，清理残留后重试" % (attempt + 1, str(e)[:80]))
                self._kill_stale_chrome()
                time.sleep(3)
        else:
            raise RuntimeError("Chrome 启动失败: %s" % last_err)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.panel_state = {"body": ""}

        def on_resp(r):
            if "panelInfo" in r.url:
                try:
                    self.panel_state["body"] = r.text()[:200]
                    j = r.json()
                    d = (j or {}).get("data") or {}
                    self.coin = d.get("coin")
                    self.coin_at = time.time()
                    cg = d.get("curGameInfo") or {}
                    self.free_left = cg.get("leftDayFreeGameNum")
                except Exception:
                    pass
        self.ctx.on("response", on_resp)
        self.coin = None
        self.coin_at = 0
        self.free_left = None

    def stop(self):
        try:
            self.ctx.close()
        except Exception:
            pass
        try:
            self.pw.stop()
        except Exception:
            pass

    def open_panel(self):
        pg = self.page
        pg.goto(self.panel_url, wait_until="domcontentloaded", timeout=60000)
        for i in range(40):
            time.sleep(2)
            try:
                if pg.evaluate("() => !!(window.cc && cc.director.getScene && cc.director.getScene())"):
                    break
            except Exception:
                pass
        else:
            raise RuntimeError("Cocos 引擎加载超时（未登录？网络？）")
        time.sleep(3)
        if "用户未登录" in self.panel_state.get("body", ""):
            raise RuntimeError("斗鱼未登录：请先运行 probe/login2.py 扫码登录")
        log("面板就绪")

    def click_start(self, wait=30):
        """等「开始游戏」按钮出现；返回 'ok'(免费点了) / ('cost', 单价) / 'nf'(无按钮)"""
        for _ in range(wait * 2):
            info = self.page.evaluate(JS_START_INFO)
            if info:
                if info.get("free"):
                    self.page.evaluate(CLICK_START_JS)
                    log("点击开始游戏: 免费局")
                    return "ok"
                if info.get("cost"):
                    return ("cost", info["cost"])
                return "nf"
            time.sleep(0.5)
        return "nf"

    def refresh_coin(self):
        """重载面板刷新骑士币余额（panelInfo 明文 coin），返回最新余额或 None"""
        log("刷新骑士币余额（重载面板）...")
        self.coin = None
        try:
            self.page.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log("reload 失败:", str(e)[:80])
            return self.coin
        try:
            for i in range(30):
                time.sleep(1)
                if self.coin is not None:
                    break
                if self.page.evaluate(
                        "() => !!(window.cc && cc.director.getScene && cc.director.getScene())"):
                    time.sleep(3)
                    if self.coin is not None:
                        break
        except Exception:
            pass
        log("余额: coin=%s 免费次数=%s" % (self.coin, self.free_left))
        return self.coin

    def wait_fight(self, timeout=30):
        for _ in range(timeout * 2):
            time.sleep(0.5)
            try:
                if self.page.evaluate(JS_HAS_FIGHT):
                    return True
            except Exception:
                pass
        return False

    # ---------- 弹窗处理 ----------
    # 涉及付费（鱼翅）的按钮文字特征 —— 一律回避
    PAY_WORDS = ("鱼翅", "充值", "购买", "支付", "¥", "元", "元/", "获得", "升级", "购买次数")

    def popup_click(self, popup, child=None, prefer="free"):
        """prefer: free=优先非付费按钮; primary=点名 primaryButton; secondary=点名 secondaryButton"""
        t = self.page.evaluate(JS_POPUP_TARGET, {"popup": popup, "child": child})
        if not t or not t.get("buttons"):
            return False
        btns = t["buttons"]

        def pick():
            if child:
                return btns[0]
            if prefer == "secondary":
                for b in btns:
                    if b["name"] == "secondaryButton":
                        return b
            # 免费优先：label 不含付费词
            for b in btns:
                if not any(w in (b.get("label") or "") for w in self.PAY_WORDS):
                    return b
            if prefer == "free":
                # 全是付费按钮：宁可不点，也不能花钱
                log("  !! 弹窗[%s] 只有付费按钮，跳过不点: %s" %
                    (popup, [(b["name"], b.get("label")) for b in btns]))
                return None
            return btns[0]

        b = pick()
        if not b:
            return False
        css = self.page.evaluate(JS_WORLD_TO_CSS, b["wp"])
        self.page.mouse.click(css[0], css[1])
        self._last_btn = b["name"]
        log("  点击弹窗[%s] 按钮=%s 文字='%s'" % (popup, b["name"], b.get("label", "")))
        return True

    # ---- 升级三选一策略 ----
    # 依据（2026-09-08 采集 8 次弹窗 24 张候选卡 + 斗鱼规则帖）：
    #   卡文字 = 技能名 + 描述/升级描述。技能分两类：武器技（火球/剑/龙卷风/炸弹/飞镖/
    #   毒液/反射粒子/陨石...）与属性技（攻击力/生命值上限/经验拾取范围 +5%/10%/20%）。
    #   武器技再次出现时是升级（+数量/冷却降低），叠加输出 > 开新武器。
    # 技能强度分：依据 ACTIVE_SKILL_GROWTH 的 6 级(MAX)成长表（源码实测）
    #   6 级几乎是质变：闪电/炸弹/胡萝卜 num 5→10、激光 num 3→6+scale5、
    #   陨石 cd→0+num5、龙卷风 num 1→5、魔法球 cd→0.1、飞镖/寒冰箭 pierce 无限…
    #   所以「能把技能顶到 6 级的卡」永远是最优解（用户 2026-09-09 指定）。
    SKILL_TIER = {
        "闪电": 95, "炸弹": 95, "胡萝卜": 95, "激光": 95, "陨石": 95,
        "龙卷风": 92, "魔法球": 92, "飞镖": 92, "寒冰箭": 90,
        "火球": 86, "剑": 86, "月光斩": 86, "毒液": 84, "立场": 84,
        "反射粒子": 84, "宝石": 82,
    }
    # 升级类型加成（同一技能内比较用）
    UPGRADE_BONUS = (
        ("终极形态", 40),      # 满级后的终极进化，战力大幅提升
        ("数量翻倍", 30), ("增加数量", 26), ("数量提升", 26), ("数量", 22),
        ("冷却降低", 18), ("冷却小幅降低", 10),
        ("伤害提升", 16), ("伤害大幅提升", 22),
        ("穿透", 14), ("区域扩大", 12), ("范围扩大", 12),
        ("长度升级", 14), ("格挡子弹", 10), ("环绕", 10),
    )
    PASSIVE_ATK = "攻击力"
    PASSIVE_HP = ("生命值上限",)
    SKILL_BLACKLIST = ("经验拾取范围", "拾取范围", "移动速度", "移速", "速度提升")

    @staticmethod
    def match_skill_name(text):
        """卡片文本 -> 技能中文名（取最长前缀匹配）"""
        best = None
        for name in MagicKnightBot.SKILL_TIER:
            if text.startswith(name) and (best is None or len(name) > len(best)):
                best = name
        return best

    def score_card(self, text, level, is_new, n_active, n_passive, max_flag):
        """
        升级三选一 v3（2026-09-09）：
        1) MAX 卡（选后技能直接到 6 级）= 无条件最高优先，立刻选
        2) 已满级(6)的技能卡 = 纯浪费，永不选
        3) 新技能：主动槽 5 个满后无效 -> 降权；否则按技能强度给分
        4) 已有技能：等级越高越值得继续推（集中资源顶满，收益远大于平均升级）
        5) 被动(攻击力/生命/移速/拾取范围)：最多 2 个槽，只留攻击力
        """
        name = self.match_skill_name(text)
        # 被动技能（属性卡）
        if name is None and any(k in text for k in ("攻击力", "生命值上限", "移速", "拾取范围")):
            if any(w in text for w in self.SKILL_BLACKLIST):
                return -100
            if n_passive >= MAX_PASSIVE_NUM:
                return -800
            if self.PASSIVE_ATK in text:
                return 60
            if any(w in text for w in self.PASSIVE_HP):
                return 8
            return -50
        if any(w in text for w in self.SKILL_BLACKLIST):
            return -100
        if level >= MAX_SKILL_LEVEL:
            return -900                    # 已满级(6)，选了没收益
        if max_flag:
            return 100000 + self.SKILL_TIER.get(name, 70)   # 出现就立刻选（用户指定）
        s = self.SKILL_TIER.get(name, 70)
        if level == MAX_SKILL_LEVEL - 1:
            return 10000 + s               # 服务器等级 5->6（即 MAX）亦最优先
        if is_new:
            if n_active >= MAX_ACTIVE_NUM:
                return -800                   # 主动槽已满，新技能无处安放
            s += 25
        else:
            s = s * 0.55 + level * 14         # 等级越高越优先继续推
        for kw, sc in self.UPGRADE_BONUS:
            if kw in text:
                s += sc
                break
        return s

    def pick_skill_card(self, battle_log):
        cards = None
        cho = None
        self._skill_archive = getattr(self, "_skill_archive", {})
        try:
            cards = self.page.evaluate(JS_SKILL_CARDS)
        except Exception as e:
            log("选卡读取失败:", str(e)[:60])
        try:
            cho = self.page.evaluate(JS_CHOICES)
        except Exception as e:
            log("候选结构读取失败:", str(e)[:60])
        if not cards:
            if self.popup_click("fightUpLevel", child="skillCard1"):
                battle_log.append({"ev": "levelup", "pick": "skillCard1(fallback)", "ts": time.time()})
            return

        actives = (cho or {}).get("actives") or []
        n_active = len(actives)
        n_passive = (cho or {}).get("passiveNum") or 0
        lvl_by_name = {}
        lvl_by_type = {}
        for a in actives:
            if a.get("name"):
                lvl_by_name[a["name"]] = a.get("level") or 0
            if a.get("type") is not None:
                lvl_by_type[a["type"]] = a.get("level") or 0
        chs = (cho or {}).get("choices") or []

        scored = []
        for i, c in enumerate(cards):
            text = c["text"]
            name = self.match_skill_name(text)
            badge = c.get("badge")
            sv = 0
            if i < len(chs):
                sv = lvl_by_type.get(chs[i].get("skillType"), 0) or 0
            # 卡片自带等级状态优先（不受 title2 改名影响）：
            #   new -> 0；stars -> 点亮数-1（level 1~4）；max -> level 5（选后 6）
            if badge == "new":
                level = 0
            elif badge == "max":
                level = sv if sv >= MAX_SKILL_LEVEL - 1 else MAX_SKILL_LEVEL - 1
            elif badge == "stars" and c.get("stars"):
                level = max(1, c["stars"] - 1)
            else:
                level = lvl_by_name.get(name, 0) if name else sv
            is_new = (badge == "new") or (bool(i < len(chs)) and not chs[i].get("have"))
            s = self.score_card(text, level, is_new, n_active, n_passive,
                                c.get("max") or ("终极形态" in text))
            scored.append((s, -c["idx"], c, is_new, level, name, badge))
        scored.sort(key=lambda x: -x[0])
        top = scored[0]
        idx = top[2]["idx"]
        css = self.page.evaluate(JS_WORLD_TO_CSS, top[2]["wp"])
        self.page.mouse.click(css[0], css[1])
        tag = "MAX" if top[0] >= 10000 else ("新" if top[3] else "升%d" % top[4])
        log("  升级选卡 #%d [%s] %s  (score=%d lv=%s %s)" %
            (idx, tag, top[2]["text"][:36], top[0], top[4], top[6] or "-"))
        battle_log.append({"ev": "levelup", "pick": idx, "text": top[2]["text"],
                           "score": top[0], "max": top[0] >= 10000,
                           "all": [c["text"][:40] for c in cards], "ts": time.time()})
        for c in cards:
            key = c["text"][:16]
            ar = self._skill_archive.setdefault(
                key, {"type": None, "seen": 0, "picked": 0, "maxLevel": 0, "levels": []})
            ar["seen"] += 1
            if c["idx"] == idx:
                ar["picked"] += 1
        self._log_data({"ev": "skill_option", "ts": time.time(),
                        "options": [{"card": c["text"][:60],
                                     "score": next(x[0] for x in scored if x[2]["idx"] == c["idx"]),
                                     "picked": c["idx"] == idx} for c in cards],
                        "chosen": idx})

    def handle_popups(self, pops, battle_log):
        self._popup_clicks = getattr(self, "_popup_clicks", {})
        for pop in pops:
            if pop == "fightUpLevel":
                self.pick_skill_card(battle_log)
                continue
            self._popup_clicks[pop] = self._popup_clicks.get(pop, 0) + 1
            n = self._popup_clicks[pop]
            if n > 12:
                # 同一弹窗连点 12 次仍未关闭（如结算网络慢/交互失效）-> 兜底重载
                log("  弹窗[%s] %d 次点击未关闭，重载面板兜底" % (pop, n))
                self.sh("popup_stuck_%s" % re.sub(r"\W", "_", pop)[:20])
                self.recover_home()
                battle_log.append({"ev": "popup_stuck_reload", "popup": pop, "ts": time.time()})
                return
            if re.search(r"revive|relive|fuhuo|revive", pop, re.I):
                ok = self.popup_click(pop, prefer="secondary")
                if not ok:
                    self.popup_click(pop, prefer="free")
                battle_log.append({"ev": "revive_decline", "popup": pop, "ts": time.time()})
            else:
                if n <= 2 and pop not in self._seen_popups:
                    self._seen_popups.add(pop)
                    log("  处理弹窗:", pop)
                    self.sh("popup_%s" % re.sub(r"\W", "_", pop)[:24])
                ok = self.popup_click(pop, prefer="free")
                if ok and n % 3 == 0:
                    log("  弹窗[%s] 持续未关（第 %d 次点击），放慢节奏" % (pop, n))
                time.sleep(1.5 if n > 2 else 0.2)
                battle_log.append({"ev": "popup", "popup": pop, "click": n,
                                   "btn": self._last_btn, "ts": time.time()})

    def sh(self, name):
        try:
            self.page.screenshot(path=os.path.join(LOGDIR, name + ".png"))
        except Exception:
            pass

    def resume_battle(self):
        """计时器卡死时：点暂停按钮 -> 点「继续游戏」唤醒（均为免费操作）"""
        try:
            hud = self.page.evaluate(JS_HUD)
            if hud and hud.get("stopBtn"):
                css = self.page.evaluate(JS_WORLD_TO_CSS, hud["stopBtn"])
                self.page.mouse.click(css[0], css[1])
                time.sleep(2)
                pops = self.page.evaluate(JS_ACTIVE_POPUPS)
                if pops:
                    self.handle_popups(pops, [])
        except Exception as e:
            log("resume 失败:", str(e)[:80])

    def recover_home(self):
        """弹窗卡死兜底：重载面板页回主页（放弃当前结算展示，避免永久卡死）"""
        log("重载面板页面...")
        try:
            self.page.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log("reload 失败:", str(e)[:80])
        try:
            for i in range(40):
                time.sleep(2)
                if self.page.evaluate(
                        "() => !!(window.cc && cc.director.getScene && cc.director.getScene())"):
                    break
        except Exception:
            pass
        time.sleep(4)
        self._popup_clicks = {}
        self._seen_popups = set()

    # ---------- 单局 ----------
    def _log_data(self, obj):
        """游戏实时数据抓取：技能档案 + 伤害弹道，追加写 logs/gamedata_*.jsonl"""
        if not getattr(self, "_dfn", None):
            return
        try:
            with open(self._dfn, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def run_battle(self, idx):
        log("=== 第 %d 局 ===" % idx)
        tsname = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        logfn = os.path.join(LOGDIR, "battle_%s.jsonl" % tsname)
        self._dfn = os.path.join(LOGDIR, "gamedata_%s.jsonl" % tsname)
        blog = []
        self._seen_popups = set()
        self._popup_clicks = {}
        self._skill_archive = {}     # 技能档案：name/type -> {seen, picked, maxLevel}
        self._bmax = {"kills": 0, "gold": 0}   # 局内击杀/金币峰值（局末 hud 会清空）
        if self.god:
            try:
                self.page.evaluate(GOD_MODE_JS, {"god": True})
                log("无敌模式已开启（setInvincible 常驻，真实免伤）")
            except Exception as e:
                log("无敌注入失败:", str(e)[:80])
        self._god_injected = self.god
        if not self.no_move:
            try:
                self.page.evaluate(MOVE_BOT_JS, {"god": bool(self.god)})
                log("走位 bot 已注入（模式=%s）" % ("无敌猎杀·不躲避" if self.god else "常规躲避"))
            except Exception as e:
                log("注入 bot 失败:", e)
        t0 = time.time()
        last_dump = 0
        last_progress = time.time()
        last_timer = None
        end_reason = "timeout"
        while time.time() - t0 < 900:          # 单局上限 15 分钟
            if self._stop:
                end_reason = "user-stop"
                break
            # 运行中热切换无敌（GUI 按钮）：下个 tick 生效，避免跨线程调 Playwright
            if self.god != getattr(self, "_god_injected", None):
                self._god_injected = self.god
                try:
                    self.page.evaluate(GOD_MODE_JS, {"god": bool(self.god)})
                    if not self.no_move:
                        self.page.evaluate(MOVE_BOT_JS, {"god": bool(self.god)})
                    log("模式切换 -> %s" % ("无敌猎杀·不躲避" if self.god else "常规躲避"))
                except Exception as e:
                    log("切换失败:", str(e)[:80])
            time.sleep(1.0)
            try:
                st = self.page.evaluate(JS_SNAPSHOT)
                st["hud"] = self.page.evaluate(JS_HUD)
            except Exception as e:
                log("快照失败:", str(e)[:80])
                continue
            now = time.time()
            if now - last_dump >= 5:
                last_dump = now
                try:
                    with open(logfn, "a", encoding="utf-8") as f:
                        f.write(json.dumps(st, ensure_ascii=False) + "\n")
                except Exception:
                    pass
                # ---- 实时数据抓取落盘（技能状态 + 敌弹道轨迹）----
                try:
                    sk = self.page.evaluate(JS_SKILLS_STATE)
                except Exception:
                    sk = None
                rec = {"ev": "state", "ts": now, "battleId": st.get("battleId"),
                       "minute": st.get("minute"),
                       "hud": st.get("hud"),
                       "threats": st.get("threats", []),
                       # 全量诊断字段（精英/经验/掉落/威胁类别/宝箱buff）
                       "threatSeen": st.get("threatSeen"),
                       "enemies": st.get("enemies"), "nearest": st.get("nearest"),
                       "nearestDrop": st.get("nearestDrop"), "pos": st.get("pos"),
                       "exp": st.get("exp"), "upgradeRoundId": st.get("upgradeRoundId"),
                       "eliteMode": st.get("eliteMode"), "eliteD": st.get("eliteD"),
                       "elites": st.get("elites"),
                       "chest": st.get("chest"), "buff": st.get("buff"),
                       "rare": st.get("rare"),
                       "dropTypes": st.get("dropTypes")}
                if sk:
                    self._last_skills = sk       # 局末搭配快照用
                    rec["skills"] = sk
                    for a in (sk.get("active") or []):
                        key = a.get("name") or ("type%d" % a.get("type"))
                        ar = self._skill_archive.setdefault(
                            key, {"type": a.get("type"), "seen": 0, "picked": 0,
                                  "maxLevel": 0, "levels": []})
                        ar["maxLevel"] = max(ar["maxLevel"], a.get("level") or 0)
                        if a.get("level") not in ar["levels"]:
                            ar["levels"].append(a.get("level"))
                self._log_data(rec)
            if st.get("popups"):
                self.handle_popups(st["popups"], blog)
            if not st.get("fight"):
                end_reason = "fight-gone"
                break
            # ⚠️ 绝技(finalSkillButton)消耗鱼翅，永不触碰
            # 卡死检测：计时器 90 秒不动且无弹窗 -> 暂停/继续唤醒
            timer = (st.get("hud") or {}).get("timer")
            if timer and timer == last_timer and not st.get("popups"):
                if now - last_progress > 90:
                    log("  计时器卡住（%s），尝试暂停/继续唤醒" % timer)
                    self.resume_battle()
                    blog.append({"ev": "stall_resume", "timer": timer, "ts": now})
                    last_progress = now
            else:
                last_progress = now
                last_timer = timer
            if int(now) % 30 == 0:
                hud = st.get("hud") or {}
                try:
                    if hud.get("kills"):
                        self._bmax["kills"] = max(self._bmax["kills"],
                                                  int(str(hud["kills"]).replace(",", "")))
                    if hud.get("gold"):
                        self._bmax["gold"] = max(self._bmax["gold"],
                                                 int(str(hud["gold"]).replace(",", "")))
                except Exception:
                    pass
                log("  局内 分钟=%s 击杀=%s 金币=%s 敌人=%s 最近=%s Boss=%s(%s) 箱=%s 稀有=%s 距=%s 血=%s 无敌=%s 弹窗=%s" %
                    (hud.get("timer"), hud.get("kills"), hud.get("gold"),
                     st.get("enemies"), st.get("nearest"),
                     st.get("bosses"), st.get("bossD"), st.get("chest"),
                     st.get("rare"), st.get("nearestDrop"), st.get("hp"),
                     st.get("inv"), st.get("popups")))
        else:
            end_reason = "timeout"

        self.sh("battle_end_%d" % idx)
        log("本局结束（%s），用时 %.0f 秒；事件: %s" %
            (end_reason, time.time() - t0, [e["ev"] for e in blog]))
        with open(logfn, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ev": "battle_end", "reason": end_reason, "events": blog,
                                "ts": time.time()}, ensure_ascii=False) + "\n")
        # 技能档案汇总（本局出现过的所有技能：出现/被选/最高级）
        arc = [{"skill": k, "type": v["type"], "seen": v["seen"],
                "picked": v["picked"], "maxLevel": v["maxLevel"],
                "levels": sorted(v["levels"])} for k, v in self._skill_archive.items()]
        self._log_data({"ev": "skill_archive", "battleId": st.get("battleId"),
                        "reason": end_reason, "archive": arc, "ts": time.time()})
        # ---- 每局技能搭配记录：build + 结局，跨局累积到 logs/builds.jsonl ----
        last_skills = getattr(self, "_last_skills", None) or {}
        active_now = {(a.get("name") or ("type%d" % a.get("type"))): (a.get("level") or 0)
                      for a in (last_skills.get("active") or [])}
        kills = self._bmax["kills"] or (st.get("hud") or {}).get("kills") or ""
        gold = self._bmax["gold"] or (st.get("hud") or {}).get("gold") or ""
        hud_t = (st.get("hud") or {}).get("timer") or ""
        # 胜利判定：结算弹窗点到 collectButton「收下」= 通关奖励
        is_victory = any(e.get("ev") == "popup" and e.get("btn") == "collectButton"
                         for e in blog)
        build_rec = {
            "battleId": st.get("battleId"), "ts": time.time(),
            "duration": round(time.time() - t0), "end": end_reason,
            "victory": bool(is_victory), "stage": st.get("stage"),
            "kills": kills, "gold": gold, "finalTimer": hud_t,
            "build": active_now,
            "picks": [e.get("text", "") for e in blog if e.get("ev") == "levelup"],
        }
        try:
            with open(os.path.join(LOGDIR, "builds.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(build_rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
        log("搭配存档: %s | %s 击杀=%s 金币=%s | %s" % (
            "胜" if is_victory else "败" if end_reason == "fight-gone" else end_reason,
            round(time.time() - t0), kills, gold,
            ", ".join("%s(%d)" % (k, v) for k, v in sorted(active_now.items(),
                                                           key=lambda x: -x[1]))[:160]))
        time.sleep(6)   # 等结算弹窗弹出
        # 清残留弹窗（点各弹窗主按钮直到清空）
        for _ in range(8):
            try:
                pops = self.page.evaluate(JS_ACTIVE_POPUPS)
            except Exception:
                break
            if not pops:
                break
            self.handle_popups(pops, blog)
            time.sleep(1.5)

    # ---------- 主循环 ----------
    def run(self):
        self.start_browser()
        try:
            self.open_panel()
            idx = 0
            fail = 0
            while True:
                if self._stop:
                    log("收到停止信号，退出")
                    break
                if self.rounds and idx >= self.rounds:
                    log("已完成 %d 局，退出" % self.rounds)
                    break
                res = self.click_start()
                if res == "ok":
                    fail = 0
                elif isinstance(res, tuple) and res[0] == "cost":
                    need = res[1]
                    coin = self.coin if self.coin is not None else self.refresh_coin()
                    if coin is None:
                        log("无法获取骑士币余额，重载刷新")
                        self.refresh_coin()
                        coin = self.coin
                    if coin is not None and coin >= need:
                        log("免费次数已用完，用骑士币开一局（余额 %s ≥ %d）" % (coin, need))
                        self.page.evaluate(CLICK_START_JS)
                        fail = 0
                    else:
                        log("骑士币不足（余额 %s < 所需 %d），自动停止挂机" % (coin, need))
                        self.sh("coin_not_enough")
                        break
                else:
                    # 按钮未找到：战斗中接管 / 界面异常恢复 / 次数耗尽
                    if self.wait_fight(2):
                        log("检测到战斗进行中，接管")
                        idx += 1
                        self.run_battle(idx)
                        time.sleep(5)
                        continue
                    fail += 1
                    if fail == 5:
                        log("开始按钮多次未找到，重载面板恢复")
                        self.sh("start_fail_%d" % idx)
                        self.recover_home()
                    elif fail >= 12:
                        log("连续 %d 次无法开始，判定异常，退出" % fail)
                        break
                    time.sleep(8)
                    continue
                if not self.wait_fight(35):
                    log("35 秒未进入战斗（弹窗拦截/卡界面），截图排查")
                    self.sh("no_fight_%d" % idx)
                    try:
                        pops = self.page.evaluate(JS_ACTIVE_POPUPS)
                        if pops:
                            self.handle_popups(pops, [])
                    except Exception:
                        pass
                    time.sleep(10)
                    continue
                idx += 1
                self.run_battle(idx)
                time.sleep(5)
        except KeyboardInterrupt:
            log("手动停止")
        finally:
            self.stop()


def kill_stale_chrome():
    """按命令行精确匹配本 profile 的残留 chrome，避免 profile 被占用导致启动秒退"""
    import subprocess
    ps1 = os.path.join(tempfile.gettempdir(), "kill_mf_chrome.ps1")
    try:
        with open(ps1, "w", encoding="utf-8-sig") as f:
            f.write("$p = Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                    "Where-Object { $_.CommandLine -like '*%s*' };"
                    "if ($p) { $p | ForEach-Object { "
                    "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }" % PROFILE)
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", ps1], capture_output=True, timeout=30)
    except Exception:
        pass


def open_login(timeout=900):
    """打开持久化浏览器到斗鱼首页供手动登录；用户关闭浏览器后返回。

    登录态保存在 PROFILE（chrome_profile），之后挂机自动复用。
    """
    if not CHROME:
        raise RuntimeError("未检测到 Chrome/Edge 浏览器，请先安装 Google Chrome")
    kill_stale_chrome()
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE, executable_path=CHROME, headless=False,
        viewport={"width": 1360, "height": 900},
        args=["--disable-blink-features=AutomationControlled", "--no-first-run",
              "--no-default-browser-check"],
        ignore_default_args=["--enable-automation"])
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.douyu.com", timeout=60000)
        log("已打开登录窗口：请在浏览器里登录斗鱼，登录后直接关闭浏览器窗口即可")
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                # 等 context close 事件（用户关掉浏览器窗口即触发），5 秒一轮
                ctx.wait_for_event("close", timeout=5000)
                break
            except PWTimeoutError:
                continue                       # 还没关，继续等
            except Exception:
                break                          # 连接断了（浏览器被杀）
    finally:
        try:
            ctx.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
    log("登录窗口已关闭，登录态已保存到 %s" % PROFILE)


def has_login():
    """粗判：profile 里是否已存在斗鱼登录 cookie（acf_uid）"""
    # Chrome 新旧版本路径不同：Default/Cookies 或 Default/Network/Cookies
    for p in (os.path.join(PROFILE, "Default", "Cookies"),
              os.path.join(PROFILE, "Default", "Network", "Cookies")):
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "rb") as f:
                if b"acf_uid" in f.read():
                    return True
        except Exception:
            pass
    return False


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rid", type=int, default=2561707, help="斗鱼房间号")
    ap.add_argument("--rounds", type=int, default=0, help="局数上限，0=不限")
    ap.add_argument("--headless", action="store_true", help="无头模式")
    ap.add_argument("--no-move", action="store_true", help="只开局不走位")
    ap.add_argument("--god", action="store_true", help="无敌模式（setInvincible 常驻，真实免伤）")
    args = ap.parse_args()
    MagicKnightBot(rid=args.rid, rounds=args.rounds,
                   headless=args.headless, no_move=args.no_move,
                   god=args.god).run()
