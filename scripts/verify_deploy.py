"""公网验证部署版前端：新 bundle 含探活逻辑、无双前缀、API 200。"""
import re
import httpx

B = "http://47.119.50.139"
r = httpx.get(B + "/", timeout=15)
m = re.search(r'src="(/assets/[^"]+\.js)"', r.text)
if not m:
    print("未找到 JS 引用, index 前 300 字节:", r.text[:300])
    raise SystemExit(1)
js = m.group(1)
print("bundle:", js)
b = httpx.get(B + js, timeout=20).text
print("含 datasources 探活:", "datasources" in b)
print("含状态字样(已连通/主源失败):", ("已连通" in b) and ("主源失败" in b))
print("双重前缀残留:", "/da/v1/da/v1" in b)
ov = httpx.get(B + "/da/v1/overview", timeout=15)
print("API /da/v1/overview:", ov.status_code)
