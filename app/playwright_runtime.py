"""Playwright 运行环境配置（补上游缺失模块）"""
import os
import shutil
import subprocess


def configure_playwright_env():
    """配置 Playwright 环境变量，复用已安装的 Chromium。"""
    # 允许通过环境变量覆盖浏览器路径
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        default_path = os.path.expanduser("~/Library/Caches/ms-playwright")
        if os.path.isdir(default_path):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = default_path


def ensure_playwright_ready() -> bool:
    """检查 Playwright Chromium 是否可用。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    # 检查 chromium 可执行文件是否存在
    browsers_path = os.environ.get(
        "PLAYWRIGHT_BROWSERS_PATH",
        os.path.expanduser("~/Library/Caches/ms-playwright"),
    )
    if os.path.isdir(browsers_path):
        for entry in os.listdir(browsers_path):
            if entry.startswith("chromium"):
                return True
    # 尝试用 playwright CLI 检查
    try:
        r = subprocess.run(
            [shutil.which("playwright") or "playwright", "install", "--dry-run", "chromium"],
            capture_output=True, timeout=30,
        )
        return r.returncode == 0
    except Exception:
        return False
