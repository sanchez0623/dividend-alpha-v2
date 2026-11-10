"""部署版前端 bundle 终验：新真值字段已编入。"""
import httpx
import re

r = httpx.get("http://47.119.50.139/", timeout=15)
m = re.search(r'src="(/assets/[^"]+\.js)"', r.text)
js = m.group(1)
b = httpx.get("http://47.119.50.139" + js, timeout=20).text
print("bundle:", js)
print("daily_pnl 字段:", "daily_pnl" in b)
print("position_count 字段:", "position_count" in b)
print("盘后任务状态字样:", "盘后任务已完成" in b)
print("可用股数中文列头:", "可用股数" in b)
print("英文列头残留 shares_available:", '"shares_available"' in b)
print("无标注=实时 图例:", "无标注" in b)
print("dc-dot 渲染残留:", b.count("dc-dot"))

