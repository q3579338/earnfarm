"""启动脚本。

必须用顶层脚本而不是 `python -m earnfarm.ui.app`：NiceGUI 的 ui.run() 会用
runpy.run_path 重跑入口文件，那条路径不带包上下文，包内的相对导入会全部炸掉。

两种形态：
    python run.py                    界面模式（浏览器）
    python run.py --watch            守护模式（无界面，7×24 跑，有事才推送）
    python run.py --watch --status   问一句"它还活着吗"
    python run.py --watch --once     只跑一轮就退出（排错 / cron）

守护模式这条路径**一行 nicegui 都不导入**——服务器上根本不需要装它。
"""

import sys


def main() -> None:
    argv = sys.argv[1:]
    if "--watch" in argv:
        from earnfarm.watch import main as watch_main
        raise SystemExit(watch_main([a for a in argv if a != "--watch"]))
    from earnfarm.ui.app import main as ui_main
    ui_main()


if __name__ in {"__main__", "__mp_main__"}:
    main()
