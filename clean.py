"""清理所有运行数据（保留配置文件）"""
import shutil
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

targets = [
    DATA_DIR / "kaguya.db",       # 数据库
    DATA_DIR / "workspaces",      # workspace 文件
    DATA_DIR / "logs",            # 日志
]

cleaned = []
for t in targets:
    if t.exists():
        if t.is_file():
            t.unlink()
        else:
            shutil.rmtree(t)
        cleaned.append(str(t.relative_to(Path(__file__).parent)))

if cleaned:
    print(f"🧹 已清理: {', '.join(cleaned)}")
else:
    print("✨ 已经是干净的了")
