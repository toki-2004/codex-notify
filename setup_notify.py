# -*- coding: utf-8 -*-
"""codex-notify 一键配置工具。

用法：
    双击 setup.bat，或手动运行：python setup_notify.py

流程：
    1. 选择推送渠道（默认 WxPusher，免费）并按提示输入密钥；
    2. 写入本项目 config.json（已有密钥直接替换，其余配置保持不变）；
    3. 向 ~/.codex/config.toml 注入 notify 钩子（只新增/替换 notify 这一行，
       不修改文件中任何其他条目；文件不存在则新建）；
    4. 可选发送一条测试消息验证。

设计约定：
    - 支持 CODEX_HOME 环境变量（默认 ~/.codex），便于测试与多账号环境；
    - 钩子命令使用运行本脚本的 Python 解释器，不依赖机器上固定的 Python 路径。
"""

import json
import os
import re
import subprocess
import sys
import time


DEFAULT_CONFIG = {
    "service": "wxpusher",
    "wxpusher_apptoken": "",
    "wxpusher_uids": "",
    "wxpusher_api": "https://wxpusher.zjiecode.com/api/send/message",
    "serverchan_sendkey": "",
    "serverchan_api": "https://sctapi.ftqq.com/{key}.send",
    "pushplus_token": "",
    "pushplus_api": "https://www.pushplus.plus/send",
    "debounce_seconds": 0,
    "dedupe_seconds": 90,
    "max_message_chars": 600,
    "http_timeout_seconds": 8,
}


def project_dir():
    """本项目根目录：普通脚本用 __file__，冻结版（PyInstaller）用 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.join(
        os.path.expanduser("~"), ".codex")


def config_path():
    return os.path.join(project_dir(), "config.json")


def config_toml_path():
    return os.path.join(codex_home(), "config.toml")


def mask_key(key):
    if len(key) <= 8:
        return key[:3] + "****"
    return key[:6] + "****" + key[-4:]


def load_config_json():
    cfg = dict(DEFAULT_CONFIG)
    path = config_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                cfg.update({k: v for k, v in user.items() if v is not None})
        except Exception as e:
            print("[WARN] 读取 %s 失败，将按默认配置重建: %r" % (path, e))
    return cfg


def save_config_json(cfg):
    path = config_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print("[OK] 已写入 %s" % path)


def toml_replace_or_insert(lines, new_line):
    """替换根级 notify 行；没有则在第一个 section 之前插入（保持根级）。

    返回 (新行列表, 是否替换了已有 notify)。
    """
    found = False
    in_section = False
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not in_section and stripped.startswith("[") and not stripped.startswith("[["):
            in_section = True
        if not in_section and re.match(r"notify\s*=", stripped):
            found = True
            out.append(new_line)
            if "]" not in line:
                # 多行数组：跳过后续直到闭合的 ] 行
                i += 1
                while i < len(lines) and "]" not in lines[i]:
                    i += 1
            i += 1
            continue
        out.append(line)
        i += 1
    if not found:
        insert_at = len(out)
        for idx, line in enumerate(out):
            if line.strip().startswith("[") and not line.strip().startswith("[["):
                insert_at = idx
                break
        # 插到 section 前时补一个空行分隔；追加到文件末尾则不需要
        if (insert_at < len(out)
                and insert_at > 0
                and out[insert_at - 1].strip() != ""):
            out.insert(insert_at, "\n")
            insert_at += 1
        out.insert(insert_at, new_line)
    return out, found


def write_config_toml(python_path, notify_py):
    path = config_toml_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        lines = content.splitlines(keepends=True)
    else:
        lines = []
    nl = "\r\n" if any("\r\n" in line for line in lines) else "\n"
    esc = lambda s: s.replace("\\", "/")
    new_value = 'notify = ["%s", "%s"]' % (esc(python_path), esc(notify_py))
    new_line = new_value + nl
    out, found = toml_replace_or_insert(lines, new_line)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.writelines(out)
    if found:
        print("[OK] %s 中的 notify 条目已更新（其他配置原样保留）" % path)
    else:
        print("[OK] %s 已注入 notify 条目（其他配置原样保留）" % path)


def find_system_python():
    """冻结版（exe）运行时，从 PATH 找一个可执行 Python 给钩子用。"""
    for cmd in (["py", "-3"], ["python"], ["python3"]):
        try:
            out = subprocess.check_output(
                cmd + ["-c", "import sys; print(sys.executable)"],
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            candidate = out.decode("utf-8", "replace").strip()
            if candidate:
                return candidate
        except Exception:
            continue
    return None


def resolve_notify_python():
    if getattr(sys, "frozen", False):
        found = find_system_python()
        if not found:
            print("[ERROR] 未检测到可用的 Python，notify 钩子运行时需要 Python 3。")
            print("        请先安装 https://www.python.org/downloads/ 后重新运行本工具。")
            return None
        return found
    return sys.executable


def send_test_message(cfg):
    sys.path.insert(0, project_dir())
    try:
        import notify
    except Exception as e:
        print("[WARN] 无法加载 notify.py 发送测试消息: %r" % (e,))
        return
    st = {
        "cwd": project_dir(),
        "client": "setup",
        "ts": time.time(),
        "last_assistant_message": "这是一键配置工具发出的测试消息。",
    }
    (ok, detail), _title, _content = notify.send_notification(cfg, st)
    if ok:
        print("[OK] 测试消息已发送，请查看手机（渠道：%s）" % cfg.get("service"))
    else:
        print("[WARN] 测试消息发送失败: %s（可稍后查看 logs/notify.log）" % detail)


def ask_provider(cfg):
    """选渠道并收集密钥，写回 cfg；返回 False 表示用户放弃（不做任何修改）。"""
    cur = str(cfg.get("service") or "wxpusher")
    names = {"wxpusher": "WxPusher(免费,推荐)", "serverchan": "Server酱(订阅制)",
             "pushplus": "PushPlus(需实名)"}
    print("推送渠道：1 = WxPusher（免费，推荐）  2 = Server酱  3 = PushPlus")
    print("[INFO] 当前配置渠道：%s" % names.get(cur, cur))
    choice = input("请选择（1/2/3，默认 1）: ").strip() or "1"
    if choice not in ("1", "2", "3"):
        print("[ERROR] 无效选择，已退出（未做任何修改）")
        return False
    service = {"1": "wxpusher", "2": "serverchan", "3": "pushplus"}[choice]

    if service == "wxpusher":
        old = (cfg.get("wxpusher_apptoken") or "").strip()
        if old:
            print("[INFO] 检测到已有 appToken: %s，输入新值将直接替换" % mask_key(old))
        token = input("请输入 WxPusher appToken（AT 开头，见 https://wxpusher.zjiecode.com/admin/）: ").strip()
        if not token:
            print("[ERROR] appToken 不能为空，已退出（未做任何修改）")
            return False
        uid = input("请输入你的 UID（UID 开头，多个用逗号分隔；公众号 wxpusher「我的」里可查）: ").strip()
        if not uid:
            print("[ERROR] UID 不能为空，已退出（未做任何修改）")
            return False
        cfg["wxpusher_apptoken"] = token
        cfg["wxpusher_uids"] = uid
    elif service == "serverchan":
        old = (cfg.get("serverchan_sendkey") or "").strip()
        if old:
            print("[INFO] 检测到已有 SendKey: %s，输入新值将直接替换" % mask_key(old))
        key = input("请输入 Server酱 SendKey（形如 SCT...，见 https://sct.ftqq.com）: ").strip()
        if not key:
            print("[ERROR] SendKey 不能为空，已退出（未做任何修改）")
            return False
        cfg["serverchan_sendkey"] = key
    else:
        old = (cfg.get("pushplus_token") or "").strip()
        if old:
            print("[INFO] 检测到已有 token: %s，输入新值将直接替换" % mask_key(old))
        key = input("请输入 PushPlus token（见 https://www.pushplus.plus）: ").strip()
        if not key:
            print("[ERROR] token 不能为空，已退出（未做任何修改）")
            return False
        cfg["pushplus_token"] = key

    cfg["service"] = service
    return True


def main():
    print("=" * 52)
    print("codex-notify 一键配置")
    print("=" * 52)
    cfg = load_config_json()
    if not ask_provider(cfg):
        return 1
    save_config_json(cfg)

    python_path = resolve_notify_python()
    if not python_path:
        return 1
    notify_py = os.path.join(project_dir(), "notify.py")
    if not os.path.exists(notify_py):
        print("[ERROR] 找不到 %s，请确认项目文件完整" % notify_py)
        return 1
    write_config_toml(python_path, notify_py)

    answer = input("是否发送一条测试消息验证？（y/n，默认 y）: ").strip().lower()
    if answer not in ("n", "no"):
        send_test_message(cfg)
    print()
    print("完成！若 Codex 正在运行，请重启会话；新开的会话立即生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
