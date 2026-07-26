
import asyncio
import sys
import os
from pathlib import Path

# Add backend to path
backend_dir = Path(__file__).resolve().parent / "backend"
sys.path.append(str(backend_dir))

from db import get_db_manager, get_preset_service, close_db
from db.namespace import set_namespace
from system_views import generate_memory_slot_view
import config as _cfg

async def main():
    if len(sys.argv) < 2:
        print("Usage: python3 query_slot.py <slot_type> [namespace]")
        return

    slot_type = sys.argv[1]
    namespace = sys.argv[2] if len(sys.argv) > 2 else ""
    
    _cfg.ensure_config_exists()
    db_manager = get_db_manager()
    await db_manager.init_db()
    
    set_namespace(namespace)
    
    preset = get_preset_service()
    boot_uris = await preset.get_boot_uris(namespace=namespace)
    
    content = await generate_memory_slot_view(slot_type, boot_uris)
    print(content)
    
    await close_db()

if __name__ == "__main__":
    asyncio.run(main())
