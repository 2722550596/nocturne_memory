#!/usr/bin/env python3
"""Seed lw 引擎角色正典到 nocturne_memory boot 记忆。

用法：
    ./venv/bin/python scripts/seed_lw_memories.py --world qian-mian-ji
    ./venv/bin/python scripts/seed_lw_memories.py --world stoneford
    ./venv/bin/python scripts/seed_lw_memories.py --world otome-lamp
    ./venv/bin/python scripts/seed_lw_memories.py --world all

读 lw 引擎的 persona/正典文件，写入 nocturne 对应 URI（core://player/*, core://elena/*, core://world/*）。
写入后设为 boot_uri（config.json 的 boot_uris 已配置，这里只写记忆内容）。
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Setup paths
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

# Default to sibling directory if WORLDLINES_ROOT is not set
DEFAULT_LW_ROOT = Path(__file__).resolve().parent.parent.parent / "worldlines-mvp" / "app" / "cores"
LW_ROOT = Path(os.environ.get("WORLDLINES_ROOT", DEFAULT_LW_ROOT))

WORLDS = {
    "qian-mian-ji": LW_ROOT / "qian-mian-ji",
    "stoneford": LW_ROOT / "stoneford",
    "otome-lamp": LW_ROOT / "otome-lamp",
}

CONFIGS_DIR = Path(__file__).resolve().parent.parent / "configs"


def read_file(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def read_json(path: Path) -> str:
    """Read a JSON file and return it as a formatted string for memory content."""
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return json.dumps(data, ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, Exception):
        return read_file(path)


def build_player_content(world_dir: Path) -> dict:
    """Build memory content for player namespace from persona files."""
    p = world_dir / "protagonist" / "persona"
    g = world_dir / "game" / "player"

    identity_parts = []
    # core_traits + motivations form identity
    traits = read_json(p / "core_traits.json")
    if traits:
        identity_parts.append(f"## 核心性格\n{traits}")
    motiv = read_json(p / "motivations.json")
    if motiv:
        identity_parts.append(f"## 动机\n{motiv}")

    persona_parts = []
    # If there's a values.json (otome-lamp), include it
    values = read_json(p / "values.json")
    if values:
        persona_parts.append(f"## 价值观\n{values}")

    background = read_file(p / "background.md")
    action_line = read_file(g / "action_line.md")

    return {
        "core://player/identity": "\n\n".join(identity_parts) if identity_parts else "",
        "core://player/persona": "\n\n".join(persona_parts) if persona_parts else "（见 identity）",
        "core://player/background": background,
        "core://player/action_line": action_line,
    }


def build_elena_content(world_dir: Path) -> dict:
    """Build memory content for elena namespace from heroine persona files."""
    h = world_dir / "heroine"
    c = h / "character"
    p = h / "persona"

    identity_parts = []
    profile = read_json(c / "profile.json")
    if profile:
        identity_parts.append(f"## 角色档案\n{profile}")
    traits = read_json(p / "core_traits.json")
    if traits:
        identity_parts.append(f"## 核心性格\n{traits}")
    motiv = read_json(p / "motivations.json")
    if motiv:
        identity_parts.append(f"## 动机\n{motiv}")

    persona_parts = []
    # relationships
    rel = read_json(p / "relationships.json")
    if rel:
        persona_parts.append(f"## 人际关系\n{rel}")

    # values (otome-lamp)
    values = read_json(p / "values.json")
    if values:
        persona_parts.append(f"## 价值观\n{values}")

    # rules (otome-lamp has interaction_rules etc)
    rules_dir = h / "rules"
    if rules_dir.exists():
        for rule_file in ["interaction_rules.md", "decision_rules.md", "action_rules.md"]:
            r = read_file(rules_dir / rule_file)
            if r:
                persona_parts.append(f"## {rule_file.replace('.md', '').replace('_', ' ')}\n{r}")

    return {
        "core://elena/identity": "\n\n".join(identity_parts) if identity_parts else "",
        "core://elena/persona": "\n\n".join(persona_parts) if persona_parts else "（见 identity）",
        "core://elena/relationships": rel or "（见 persona）",
    }


def build_world_content(world_dir: Path) -> dict:
    """Build memory content for world namespace from cast.md."""
    cast = read_file(world_dir / "game" / "lore" / "cast.md")
    # Also check for world/notes.md (stoneford/otome-lamp)
    world_notes = read_file(world_dir / "game" / "world" / "notes.md")
    setting_parts = []
    if world_notes:
        setting_parts.append(world_notes)

    return {
        "core://world/cast": cast,
        "core://world/setting": "\n\n".join(setting_parts) if setting_parts else "（见 cast.md 世界规则部分）",
    }


async def seed_world(world_name: str):
    """Seed all boot memories for a single world."""
    import config as _cfg

    from db import get_db_manager, get_graph_service
    from db.namespace import namespace_scope

    world_dir = WORLDS[world_name]
    config_path = CONFIGS_DIR / world_name / "config.json"

    # Override config dynamically using the new API
    _cfg.set_config_path(config_path)
    print(f"[{world_name}] CONFIG_PATH: {_cfg.CONFIG_PATH}")

    # Init DB
    db = get_db_manager()
    await db.init_db()
    graph = get_graph_service()

    # Build all content
    all_content = {}
    for ns, builder in [("player", build_player_content), ("elena", build_elena_content), ("world", build_world_content)]:
        all_content[ns] = builder(world_dir)

    # Seed memories
    for ns, contents in all_content.items():
        async with namespace_scope(ns):
            for uri, content in contents.items():
                if not content:
                    print(f"  [{world_name}/{ns}] SKIP {uri} (empty content)")
                    continue

                # Parse URI: domain://path
                domain, path = uri.split("://", 1)

                # Check if already exists
                existing = await graph.get_memory_by_path(path, domain, namespace=ns)
                if existing:
                    # Update content
                    print(f"  [{world_name}/{ns}] UPDATE {uri}")
                    await graph.update_memory(
                        path, content, priority=0, domain=domain, namespace=ns
                    )
                else:
                    # Create parent path structure
                    parts = path.split("/")
                    # Create parent nodes if needed
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
                            pass  # Already exists

                    # Create the leaf memory
                    parent_path = "/".join(parts[:-1])
                    title = parts[-1]
                    try:
                        await graph.create_memory(
                            parent_path=parent_path,
                            content=content,
                            priority=0,
                            title=title,
                            domain=domain,
                            namespace=ns,
                        )
                        print(f"  [{world_name}/{ns}] CREATE {uri}")
                    except ValueError as e:
                        print(f"  [{world_name}/{ns}] EXISTS {uri}: {e}")

    await db.close()

    # Reset db manager singleton
    import db as _dbmod
    _dbmod._db_manager = None

    print(f"[{world_name}] Done.")


async def main():
    parser = argparse.ArgumentParser(description="Seed lw 引擎角色正典到 nocturne_memory")
    parser.add_argument("--world", required=True, choices=list(WORLDS.keys()) + ["all"],
                        help="Which world to seed")
    args = parser.parse_args()

    if args.world == "all":
        for w in WORLDS:
            await seed_world(w)
    else:
        await seed_world(args.world)


if __name__ == "__main__":
    asyncio.run(main())
