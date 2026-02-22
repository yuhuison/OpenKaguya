"""Phase 5 主动意识验证"""
import asyncio, os
os.environ["PYTHONUTF8"] = "1"

async def main():
    from kaguya.config import load_config
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

    # 1. 配置解析
    print("\n[1] 配置解析...")
    c = config.consciousness
    check("enabled 属性存在", hasattr(c, "enabled"))
    check("heartbeat 属性", hasattr(c, "heartbeat_interval_minutes"))
    check("quiet hours", hasattr(c, "quiet_hours_start"))
    print(f"    enabled={c.enabled}, heartbeat={c.heartbeat_interval_minutes}min")
    print(f"    quiet: {c.quiet_hours_start} - {c.quiet_hours_end}")

    # 2. Scheduler 初始化
    print("\n[2] Scheduler 初始化...")
    scheduler = ConsciousnessScheduler(
        config=config,
        chat_engine=None,  # 不实际调用
        send_callback=None,
    )
    check("初始化成功", scheduler is not None)
    check("静默时段解析", scheduler.quiet_start is not None)

    # 3. 静默时段判断
    print("\n[3] 静默时段...")
    is_quiet = scheduler._is_quiet_hours()
    from datetime import datetime
    now = datetime.now().strftime("%H:%M")
    print(f"    当前时间: {now}, 静默: {is_quiet}")
    check("静默判断可执行", isinstance(is_quiet, bool))

    # 4. 唤醒 Prompt
    print("\n[4] 唤醒 Prompt...")
    prompt = scheduler._build_wake_prompt()
    check("Prompt 包含时间", "当前时间" in prompt)
    check("Prompt 包含指导", "自由时间" in prompt)
    print(f"    Prompt 长度: {len(prompt)} 字符")

    # 5. 启动和停止
    print("\n[5] 启动/停止...")
    scheduler.enabled = False  # 禁用以避免实际唤醒
    await scheduler.start()
    check("禁用时可安全启动", True)
    await scheduler.stop()
    check("可安全停止", True)

    print(f"\n{'='*40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'='*40}")
    if failed == 0:
        print("\nAll tests passed!")
    else:
        import sys; sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
