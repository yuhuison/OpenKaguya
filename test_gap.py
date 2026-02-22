"""Gap Analysis 补全 — 全功能集成测试"""
import asyncio, os
os.environ["PYTHONUTF8"] = "1"

async def main():
    from kaguya.config import load_config
    from kaguya.memory.database import Database
    from kaguya.tools.workspace import WorkspaceManager
    from kaguya.tools.builtin import create_builtin_tools
    from kaguya.tools.registry import ToolRegistry
    from kaguya.core.consciousness import ConsciousnessScheduler

    config = load_config()
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

    # 1. 数据库
    print("\n[1] 数据库新表...")
    db = Database(embedding_dim=128)  # 小维度测试
    await db.connect()
    
    # 检查 WAL 模式
    mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
    check("WAL 模式", mode == "wal", f"got: {mode}")
    
    # skills 表
    sid = await db.save_skill("web_search", "搜索互联网", "搜索,查找")
    check("skills: 插入成功", sid > 0)
    skills = await db.get_skills()
    check("skills: 查询成功", len(skills) >= 1)
    await db.delete_skill("web_search")
    s2 = await db.get_skills()
    check("skills: 删除成功", len(s2) < len(skills))
    
    # tasks 表
    tid = await db.save_task("测试任务", "这是个测试", priority=5)
    check("tasks: 创建成功", tid > 0)
    tasks = await db.get_tasks()
    check("tasks: 查询成功", len(tasks) >= 1 and tasks[0]["title"] == "测试任务")
    await db.update_task_status(tid, "done")
    done_tasks = await db.get_tasks(status="done")
    check("tasks: 更新状态", len(done_tasks) >= 1)
    await db.delete_task(tid)
    
    # timers 表
    tmid = await db.save_timer("提醒喝水", "喝水！", trigger_at="2020-01-01 12:00")
    check("timers: 创建成功", tmid > 0)
    timers = await db.get_active_timers()
    check("timers: 查询活跃", len(timers) >= 1)
    triggered = await db.get_triggered_timers()
    check("timers: 到期检查可执行", isinstance(triggered, list))
    await db.deactivate_timer(tmid)
    
    # 日志查询
    logs = await db.get_daily_logs()
    check("logs: 查询可执行", isinstance(logs, list))
    
    # 笔记
    nid = await db.save_note("测试标题", "测试内容", "test")
    check("notes: save_note 方法", nid > 0)
    notes = await db.get_notes()
    check("notes: get_notes 方法", len(notes) >= 1)

    # 2. 工具注册
    print("\n[2] 工具系统（13个工具）...")
    workspace = WorkspaceManager()
    
    class MockRetriever:
        async def retrieve(self, **_): return []
    
    tools = create_builtin_tools(workspace, db, MockRetriever())
    check(f"create_builtin_tools 返回 {len(tools)} 个工具", len(tools) == 13, f"got {len(tools)}")
    
    registry = ToolRegistry()
    registry.register_all(tools)
    schemas = registry.get_openai_tools()
    check("schema 生成数量", len(schemas) == 13, f"got {len(schemas)}")
    
    tool_names = [s["function"]["name"] for s in schemas]
    expected = [
        "read_file", "write_file", "delete_file", "list_files", "run_terminal",
        "search_memory", "query_messages", "query_logs",
        "write_note", "read_notes",
        "manage_tasks", "manage_skills", "set_timer"
    ]
    for name in expected:
        check(f"工具 {name} 已注册", name in tool_names, f"missing")

    # 3. 主动意识增强
    print("\n[3] 主动意识（tasks/timers 注入）...")
    # 创建测试数据
    await db.save_task("买菜", "西红柿和鸡蛋")
    await db.save_timer("日记", "写日记", trigger_at="2099-12-31 23:59")
    
    scheduler = ConsciousnessScheduler(
        config=config, chat_engine=None, send_callback=None, db=db,
    )
    scheduler.enabled = False
    
    prompt = await scheduler._build_wake_prompt()
    check("Prompt 包含待办任务", "买菜" in prompt)
    check("Prompt 包含定时器", "日记" in prompt)
    check("Prompt 包含时间", "当前时间" in prompt)

    # 4. Token 统计
    print("\n[4] Token 统计...")
    from kaguya.llm.client import LLMClient
    # 不实际调用，只检查属性存在
    class FakeConfig:
        api_key = "fake"
        base_url = "http://localhost:1234"
        model = "test"
        temperature = 0.7
        max_tokens = 1000
    
    client = LLMClient(FakeConfig(), name="test")
    check("total_prompt_tokens 属性", hasattr(client, "total_prompt_tokens"))
    check("total_completion_tokens 属性", hasattr(client, "total_completion_tokens"))
    check("total_requests 属性", hasattr(client, "total_requests"))

    await db.close()

    print(f"\n{'='*40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'='*40}")
    if failed == 0:
        print("\nAll tests passed! 🎉")
    else:
        import sys; sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
