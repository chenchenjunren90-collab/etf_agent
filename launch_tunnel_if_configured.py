"""按 data/tunnel_config.json 的开关，可选地拉起内网穿透工具（cpolar/natapp/ngrok 等）。

设计目的：把「仪表盘做成公网可访问的网址」这一步做成可插拔配置——
默认不启用，不影响本地/局域网使用；用户注册好穿透工具、确认命令能跑通后，
只需把 tunnel_config.json 的 enabled 改成 true，start_auto.bat 每天生成
预测后就会自动挂载隧道，不用每天手动开。

本脚本只负责按配置里的 shell 命令拉起一个后台进程，不关心具体是哪个
穿透服务商——用户在 command 字段里填自己账号对应的启动命令即可。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "data" / "tunnel_config.json"


def main() -> int:
    if not CONFIG_PATH.exists():
        print("[tunnel] 未配置 data/tunnel_config.json，跳过（这是正常状态，未启用穿透）。")
        return 0

    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[tunnel] 配置文件解析失败，跳过：{exc}")
        return 0

    if not config.get("enabled"):
        print("[tunnel] enabled=false，跳过（如需启用公网访问，见 data/tunnel_config.example.json）。")
        return 0

    command = str(config.get("command") or "").strip()
    if not command:
        print("[tunnel] enabled=true 但 command 为空，跳过。")
        return 0

    print(f"[tunnel] 启动内网穿透: {command}")
    try:
        # 后台启动，不阻塞 start_auto.bat 后续步骤；具体公网地址请查看
        # 所选穿透工具自己的本地状态页（如 cpolar 默认 127.0.0.1:9200）。
        subprocess.Popen(
            command,
            shell=True,
            cwd=str(BASE_DIR),
            creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
        )
        print("[tunnel] 已在新窗口启动，请查看该窗口或穿透工具的状态页获取公网地址。")
    except Exception as exc:
        print(f"[tunnel] 启动失败：{exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
