from data_reader import LivingMemoryReader

config = {
    "livingmemory_compat": {
        "enabled": True,
        "db_path": "/root/astrbot-data/data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db",
        "identity_mappings": [
            {"unified_name": "李超凡", "umos": "aiocqhttp:110016396", "text_aliases": "EGO"}
        ]
    }
}

reader = LivingMemoryReader(config)
memories = reader.read_memories(limit=5)
print(f"读取到 {len(memories)} 条记忆\n")
print(reader.format_for_prompt(memories))
