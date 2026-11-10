"""渲染 web/preview.html 的 8 个页签并截图（本地看效果）。"""
import os
from playwright.sync_api import sync_playwright

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
URL = "file:///" + os.path.join(ROOT, "web", "preview.html").replace("\\", "/")
OUT = os.path.join(ROOT, "web", "screenshots")
os.makedirs(OUT, exist_ok=True)

PAGES = ["overview", "positions", "signals", "attribution",
         "benchmarks", "withdraw", "config", "archive"]
LABELS = ["总览", "持仓", "信号", "归因", "基准对比", "取现流水", "配置", "历史归档"]


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.goto(URL)
        pg.wait_for_timeout(2500)  # 等 Babel 编译 + React 渲染
        for i, key in enumerate(PAGES):
            pg.evaluate(f"window.dispatchEvent(new CustomEvent('da:nav',{{detail:'{key}'}}))")
            pg.wait_for_timeout(400)
            path = os.path.join(OUT, f"{i+1}_{key}.png")
            pg.screenshot(path=path, full_page=True)
            print(f"[{i+1}/8] {LABELS[i]:<5} -> {path}")
        b.close()


if __name__ == "__main__":
    main()
