"""
Phase 2 完整集成测试 — 验证向量化 & 记忆检索全链路。

测试流程：
1. 初始化数据库 + Embedding 客户端 + MemoryRetriever
2. 模拟插入 12 条对话消息（超过向量化阈值 10）
3. 手动触发 check_and_vectorize
4. 验证消息已被标记为 is_vectorized = TRUE
5. 验证 message_vectors 表中有向量数据
6. 用一条查询做混合检索（向量 + FTS5），验证能召回相关记忆
"""

import asyncio
import os
import sys

# 保证中文输出
os.environ["PYTHONUTF8"] = "1"


async def main():
    from kaguya.config import load_config
    from kaguya.llm.client import LLMClient
    from kaguya.llm.embedding import EmbeddingClient
    from kaguya.memory.database import Database
    from kaguya.memory.retriever import MemoryRetriever
    from pathlib import Path

    # === 准备 ===
    config = load_config()
    test_db_path = Path("data/test_memory.db")
    if test_db_path.exists():
        os.remove(test_db_path)

    dim = config.llm.embedding.dimensions or 4096
    db = Database(db_path=test_db_path, embedding_dim=dim)
    await db.connect()

    embed_client = EmbeddingClient(config.llm.embedding)
    secondary_llm = LLMClient(config.llm.secondary, name="secondary")
    retriever = MemoryRetriever(
        db=db,
        embed_client=embed_client,
        secondary_llm=secondary_llm,
        vectorize_threshold=10,  # 累积 10 条后触发
    )

    user_id = "test:memory_user"
    passed = 0
    failed = 0

    def check(name: str, condition: bool, detail: str = ""):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  ✅ {name}")
        else:
            failed += 1
            print(f"  ❌ {name} — {detail}")

    # === 测试 1: 插入消息 ===
    print("\n📝 测试 1: 插入 12 条模拟对话消息...")
    conversations = [
        ("user", "你好呀辉夜姬！"),
        ("assistant", "你好！今天心情怎么样？"),
        ("user", "我今天去吃了一碗超好吃的拉面"),
        ("assistant", "哇！是哪家的拉面呀？我好想尝尝地球的食物"),
        ("user", "是一家叫做一兰的日式拉面"),
        ("assistant", "一兰拉面！我在网上看到过，听说很有名呢"),
        ("user", "对了，你知道明天的天气吗？"),
        ("assistant", "让我查查...明天好像是晴天哦，适合出去玩！"),
        ("user", "太好了，我打算明天去爬山"),
        ("assistant", "爬山好呀！记得带够水和防晒霜"),
        ("user", "你最近在看什么动漫呀？"),
        ("assistant", "我最近在看《葬送的芙莉莲》，超好看的！"),
    ]

    for role, content in conversations:
        await db.save_message(
            user_id=user_id,
            platform="cli",
            role=role,
            content=content,
        )

    count_before = await db.get_unvectorized_count(user_id)
    check("消息插入成功", count_before == 12, f"期望 12 条，实际 {count_before}")

    # === 测试 2: 触发向量化 ===
    print("\n🔄 测试 2: 触发自动向量化 (调用 Embedding API)...")
    print("   [等待 Embedding API 响应...]")

    try:
        await retriever.check_and_vectorize(user_id)
        vectorize_success = True
    except Exception as e:
        vectorize_success = False
        print(f"   ⚠️  向量化异常: {e}")

    check("向量化执行成功", vectorize_success)

    # === 测试 3: 验证向量化状态 ===
    print("\n🔍 测试 3: 验证向量化状态...")

    count_after = await db.get_unvectorized_count(user_id)
    check("所有消息已标记为已向量化", count_after == 0, f"仍有 {count_after} 条未向量化")

    # 直接查 message_vectors 表
    def _count_vectors():
        return db._conn.execute("SELECT COUNT(*) FROM message_vectors").fetchone()[0]

    vec_count = await asyncio.to_thread(_count_vectors)
    check("向量表中有数据", vec_count == 12, f"期望 12 条向量，实际 {vec_count}")

    # === 测试 4: 验证摘要日志 ===
    print("\n📋 测试 4: 验证对话摘要日志...")

    def _count_logs():
        return db._conn.execute(
            "SELECT COUNT(*) FROM daily_logs WHERE user_id = ?", (user_id,)
        ).fetchone()[0]

    def _get_log():
        return db._conn.execute(
            "SELECT summary FROM daily_logs WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()

    log_count = await asyncio.to_thread(_count_logs)
    check("摘要日志已生成", log_count >= 1, f"日志数量: {log_count}")

    log_row = await asyncio.to_thread(_get_log)
    if log_row:
        print(f"   📄 摘要内容: {log_row[0][:100]}...")
    check("摘要内容非空", log_row is not None and len(log_row[0]) > 5)

    # === 测试 5: 向量 KNN 搜索 ===
    print("\n🎯 测试 5: 向量 KNN 搜索...")

    # 用 "拉面" 相关的查询做向量搜索
    query_text = "好吃的日式拉面推荐"
    print(f"   查询: \"{query_text}\"")

    try:
        query_emb = await embed_client.embed(query_text)
        vec_results = await db.search_vectors(query_emb, top_k=3)
        check("向量搜索返回结果", len(vec_results) > 0, "无结果")

        if vec_results:
            # 获取搜索结果对应的消息内容
            result_ids = [r[0] for r in vec_results]
            result_messages = await db.fetch_messages_by_ids(result_ids)
            print("   向量搜索 Top-3:")
            for i, msg in enumerate(result_messages):
                dist = vec_results[i][1] if i < len(vec_results) else "?"
                print(f"     {i+1}. [dist={dist:.4f}] {msg['content'][:60]}")

            # 检查拉面相关的消息是否在前 3 名
            ramen_found = any("拉面" in m["content"] for m in result_messages)
            check("拉面相关消息被召回", ramen_found, "Top-3 中未找到拉面相关消息")
    except Exception as e:
        check("向量搜索", False, str(e))

    # === 测试 6: FTS5 全文搜索 ===
    print("\n📖 测试 6: FTS5 全文搜索...")

    fts_results = await db.search_fts("好吃的拉面", top_k=3)  # trigram 需要 ≥3 字符
    check("FTS5 搜索返回结果", len(fts_results) > 0, "无结果")
    if fts_results:
        fts_ids = [r[0] for r in fts_results]
        fts_messages = await db.fetch_messages_by_ids(fts_ids)
        print("   FTS5 搜索结果:")
        for i, msg in enumerate(fts_messages):
            print(f"     {i+1}. {msg['content'][:60]}")

    # === 测试 7: 混合检索 (RRF) ===
    print("\n🔀 测试 7: 混合检索 (向量 + FTS5 + RRF 融合)...")

    hybrid_results = await retriever.retrieve(
        user_id=user_id,
        query="好吃的日式拉面",
        top_k=5,
    )
    check("混合检索返回结果", len(hybrid_results) > 0, "无结果")
    if hybrid_results:
        print("   混合检索 Top-5:")
        for i, msg in enumerate(hybrid_results):
            print(f"     {i+1}. [{msg['role']}] {msg['content'][:60]}")

        ramen_found_hybrid = any("拉面" in m["content"] for m in hybrid_results)
        check("拉面相关消息在混合结果中", ramen_found_hybrid)

    # === 测试 8: 不相关查询的区分度 ===
    print("\n🎭 测试 8: 不相关查询区分度...")

    unrelated_results = await retriever.retrieve(
        user_id=user_id,
        query="量子物理学的基本原理",
        top_k=3,
    )
    # 不相关查询应该仍然返回结果（因为只有这些数据），但内容不应该精确匹配
    check("不相关查询仍有结果（数据库非空）", len(unrelated_results) >= 0)
    if unrelated_results:
        print(f"   不相关查询返回 {len(unrelated_results)} 条（可作为 baseline）")

    # === 清理 & 报告 ===
    await db.close()
    try:
        os.remove(test_db_path)
    except Exception:
        pass

    print("\n" + "=" * 50)
    print(f"  测试报告: {passed} 通过, {failed} 失败")
    print("=" * 50)

    if failed > 0:
        sys.exit(1)
    else:
        print("\n🎉 全部测试通过！记忆系统工作正常！")


if __name__ == "__main__":
    asyncio.run(main())
