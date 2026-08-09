#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
import yaml

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import config as _cfg
from db import get_db_manager, get_graph_service
from db.namespace import namespace_scope
import db as _dbmod

DEFAULT_LW_ROOT = Path(__file__).resolve().parent.parent.parent / "worldlines-mvp" / "app" / "cores"
LW_ROOT = Path(os.environ.get("WORLDLINES_ROOT", DEFAULT_LW_ROOT))
CONFIGS_DIR = Path(__file__).resolve().parent.parent / "configs"

def parse_md(md_path: Path):
    if not md_path.exists():
        return None, ""
    content = md_path.read_text(encoding="utf-8")
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                fm = yaml.safe_load(parts[1])
                return fm, parts[2].strip()
            except Exception:
                pass
    return None, content.strip()

async def seed_world(world_name: str):
    world_dir = LW_ROOT / world_name
    manifest_path = world_dir / "memory_manifest.yaml"
    # Prefer NOCTURNE_DATA_DIR (project-local data) over legacy CONFIGS_DIR
    _data_dir = os.environ.get("NOCTURNE_DATA_DIR")
    if _data_dir:
        config_path = Path(_data_dir) / world_name / "config.json"
    else:
        config_path = CONFIGS_DIR / world_name / "config.json"
    
    if not manifest_path.exists():
        print(f"[{world_name}] No memory_manifest.yaml found.")
        return

    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = yaml.safe_load(f)
        
    _cfg.set_config_path(config_path)
    print(f"[{world_name}] CONFIG_PATH: {_cfg.CONFIG_PATH}")
    
    db = get_db_manager()
    await db.init_db()
    graph = get_graph_service()

    new_boot_uris = {}

    for ns, data in manifest.get('namespaces', {}).items():
        new_boot_uris[ns] = []
        all_uris = {}
        for b_uri, path_str in data.get('boot_uris', {}).items():
            all_uris[b_uri] = {"path": path_str, "is_boot": True}
            new_boot_uris[ns].append(b_uri)
            
        for a_uri, path_str in data.get('archive_uris', {}).items():
            all_uris[a_uri] = {"path": path_str, "is_boot": False}
            
        async with namespace_scope(ns):
            for uri, info in all_uris.items():
                md_path = world_dir / info["path"]
                fm, content = parse_md(md_path)
                if not content:
                    print(f"  [{world_name}/{ns}] SKIP {uri} (empty or missing)")
                    continue
                
                domain, path = uri.split("://", 1)
                title = path.split("/")[-1]
                priority = 0
                if fm:
                    title = fm.get("title", title)
                    priority = fm.get("priority", priority)
                
                existing = await graph.get_memory_by_path(path, domain, namespace=ns)
                if existing:
                    print(f"  [{world_name}/{ns}] UPDATE {uri}")
                    await graph.update_memory(
                        path, content, priority=priority, domain=domain, namespace=ns
                    )
                else:
                    parts = path.split("/")
                    for i in range(len(parts) - 1):
                        parent = "/".join(parts[:i])
                        child = parts[i]
                        try:
                            await graph.create_memory(
                                parent_path=parent,
                                content=f"（{child} 目录）",
                                priority=5,
                                title=child,
                                domain=domain,
                                namespace=ns,
                            )
                        except ValueError:
                            pass
                    
                    parent_path = "/".join(parts[:-1])
                    try:
                        await graph.create_memory(
                            parent_path=parent_path,
                            content=content,
                            priority=priority,
                            title=title,
                            domain=domain,
                            namespace=ns,
                        )
                        print(f"  [{world_name}/{ns}] CREATE {uri}")
                    except ValueError as e:
                        print(f"  [{world_name}/{ns}] EXISTS {uri}: {e}")

    await db.close()
    _dbmod._db_manager = None
    
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            cfg_data = json.load(f)
        cfg_data['boot_uris'] = new_boot_uris
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(cfg_data, f, indent=2, ensure_ascii=False)
        print(f"[{world_name}] Updated config.json with boot_uris.")
        
    print(f"[{world_name}] Done.")

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--world", required=True)
    args = parser.parse_args()
    
    if args.world == "all":
        # We only support qian-mian-ji currently or those with manifest
        for w in ["qian-mian-ji", "stoneford", "otome-lamp"]:
            await seed_world(w)
    else:
        await seed_world(args.world)

if __name__ == "__main__":
    asyncio.run(main())