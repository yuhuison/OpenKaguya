"""Phase 4 浏览器工具验证"""
import os
os.environ["PYTHONUTF8"] = "1"

from kaguya.tools.browser import BrowserToolkit

tk = BrowserToolkit(mode="local")
tools = tk.get_tools()
print(f"Browser tools: {len(tools)}")
for t in tools:
    schema = t.to_openai_schema()
    fn = schema["function"]
    print(f"  {fn['name']}: {fn['description'][:50]}")

print(f"\nAll {len(tools)} browser tools loaded OK!")
