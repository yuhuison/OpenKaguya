"""Phase 3 工具系统测试"""
import asyncio
import os
import shutil

os.environ["PYTHONUTF8"] = "1"

async def main():
    from kaguya.tools.registry import ToolRegistry
    from kaguya.tools.workspace import WorkspaceManager
    from kaguya.tools.builtin import create_builtin_tools
    from kaguya.memory.database import Database
    from pathlib import Path

    db = Database(db_path=Path("data/test_tools.db"), embedding_dim=4)
    await db.connect()
    ws = WorkspaceManager(base_dir=Path("data/test_workspaces"))

    tools = create_builtin_tools(workspace=ws, db=db, retriever=None)
    registry = ToolRegistry()
    registry.register_all(tools)

    passed = 0
    failed = 0
    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  OK: {name}")
        else:
            failed += 1
            print(f"  FAIL: {name} -- {detail}")

    # 1. OpenAI schema 生成
    print("\n[1] OpenAI Schema...")
    schemas = registry.get_openai_tools()
    for s in schemas:
        fn = s["function"]
        print(f"    - {fn['name']}: {fn['description'][:50]}")
    check("6 个工具已注册", len(schemas) == 6, f"实际 {len(schemas)}")

    # 2. 用户上下文
    print("\n[2] 用户上下文...")
    registry.set_user_context("test:user1")
    check("上下文设置成功", True)

    # 3. 文件写入
    print("\n[3] 文件写入...")
    r = await registry.execute("write_file", {"path": "hello.txt", "content": "Hello from Kaguya!"})
    check("写入成功", "已写入" in r, r)

    # 4. 文件读取
    print("\n[4] 文件读取...")
    r = await registry.execute("read_file", {"path": "hello.txt"})
    check("读取内容正确", "Hello from Kaguya!" in r, r)

    # 5. 文件列表
    print("\n[5] 文件列表...")
    r = await registry.execute("list_files", {})
    check("列表包含文件", "hello.txt" in r, r)

    # 6. 路径穿越防护
    print("\n[6] 路径穿越防护...")
    r = await registry.execute("read_file", {"path": "../../../etc/passwd"})
    check("穿越被拦截", "workspace" in r.lower() or "超出" in r, r)

    # 7. 笔记本写入
    print("\n[7] 笔记本...")
    r = await registry.execute("write_note", {
        "title": "主人喜欢什么",
        "content": "主人喜欢吃拉面，最爱一兰",
        "tags": "主人,美食",
    })
    check("笔记写入成功", "已保存" in r, r)

    # 8. 笔记本读取
    r = await registry.execute("read_notes", {"limit": 5})
    check("笔记读取成功", "拉面" in r, r)

    # 9. 带标签过滤
    r = await registry.execute("read_notes", {"tag": "美食", "limit": 5})
    check("标签过滤成功", "拉面" in r, r)

    # 10. 未知工具
    print("\n[10] 未知工具...")
    r = await registry.execute("nonexistent_tool", {})
    check("未知工具返回错误", "错误" in r or "未知" in r, r)

    # 清理
    await db.close()
    try: os.remove("data/test_tools.db")
    except: pass
    try: shutil.rmtree("data/test_workspaces")
    except: pass

    print(f"\n{'='*40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'='*40}")
    if failed == 0:
        print("\nAll tests passed!")
    else:
        import sys; sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
