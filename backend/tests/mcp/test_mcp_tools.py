"""
Integration tests for the MCP tool surface.

Covers the character-oriented RP tool API (the rewrite in 02f610a):
  browse_memory / search_memory / remember_child_memory / edit_memory /
  forget_memory / link_memory / tag_memory.

System-view assertions (system://boot, system://index, system://recent,
system://diagnostic) are pinned to the actual rendered output, and the
verbatim-storage / patch-fallback behaviour of edit_memory is verified
against the stored ground-truth content rather than fragile message
strings.
"""

import pytest


# =============================================================================
# System views (browse_memory)
# =============================================================================


@pytest.mark.asyncio
async def test_browse_memory_system_views(mcp_module, graph_service):
    """system://boot, index, recent render the seeded memories."""
    await graph_service.create_memory(
        parent_path="",
        content="Agent identity",
        priority=1,
        title="agent",
        disclosure="When booting",
    )
    await graph_service.create_memory(
        parent_path="",
        content="User identity",
        priority=1,
        title="my_user",
        disclosure="When booting",
    )

    boot = await mcp_module.browse_memory("system://boot")
    index_view = await mcp_module.browse_memory("system://index/core")
    recent = await mcp_module.browse_memory("system://recent/5")
    index_all = await mcp_module.browse_memory("system://index")

    assert "core://agent" in boot
    assert "core://my_user" in boot
    # The boot view advertises the index entry point.
    assert "system://index/<domain>" in boot
    assert "core://agent" in index_view
    assert "core://my_user" in recent
    # system://index without a domain renders the full index across all
    # domains instead of erroring.
    assert "core://agent" in index_all


@pytest.mark.asyncio
async def test_diagnostic_view_points_duplicate_aliases_to_forget_memory(mcp_module):
    """Duplicate aliases are surfaced and the hint references forget_memory."""
    await mcp_module.remember_child_memory(
        "core://",
        "Folder memory",
        importance=1,
        when="When testing diagnostics",
        title="folder",
    )
    await mcp_module.link_memory(
        target_uri="core://folder",
        new_uri="core://folder_copy",
        importance=2,
        when="When testing duplicate aliases",
    )

    diagnostic = await mcp_module.browse_memory("system://diagnostic/core")

    assert "### 3.2 Duplicate Aliases under Same Parent" in diagnostic
    assert "core://folder" in diagnostic
    assert "core://folder_copy" in diagnostic
    # The remediation hint must reference the current RP tool name
    # (forget_memory), not the legacy delete_memory verb.
    assert "forget_memory" in diagnostic
    assert "delete_memory" not in diagnostic


@pytest.mark.asyncio
async def test_mcp_tool_flow_covers_crud_alias_triggers_and_search(mcp_module, graph_service):
    created = await mcp_module.remember_child_memory(
        "core://",
        "Important Salem memory",
        importance=2,
        when="When testing MCP tools",
        title="salem_note",
    )
    updated = await mcp_module.edit_memory(
        "core://salem_note",
        append="\nGraphService handles aliases.",
    )
    triggers = await mcp_module.tag_memory(
        "core://salem_note",
        add=["Salem"],
    )
    search = await mcp_module.search_memory("GraphService")
    alias = await mcp_module.link_memory(
        target_uri="core://salem_note",
        new_uri="project://salem_alias",
        importance=3,
        when="When mirroring note",
    )
    deleted = await mcp_module.forget_memory("project://salem_alias")

    current = await graph_service.get_memory_by_path("salem_note", "core")
    removed_alias = await graph_service.get_memory_by_path("salem_alias", "project")

    assert "core://salem_note" in created
    assert "core://salem_note" in updated
    assert "Salem" in triggers
    assert "core://salem_note" in search
    assert "project://salem_alias" in alias
    assert "project://salem_alias" in deleted
    assert current["content"].endswith("GraphService handles aliases.")
    assert removed_alias is None


# =============================================================================
# Unit tests for normalize_with_positions
# =============================================================================

from text_patch import normalize_with_positions, try_normalized_patch


class TestNormalizeWithPositions:
    def test_straight_quotes_unchanged(self):
        text = 'She said "hello" to him.'
        norm, pos_map = normalize_with_positions(text)
        assert norm == text
        assert len(pos_map) == len(norm)
        assert all(pos_map[i] == i for i in range(len(norm)))

    def test_curly_quotes_normalized(self):
        text = "She said \u201chello\u201d to him."
        norm, pos_map = normalize_with_positions(text)
        assert norm == 'She said "hello" to him.'
        assert pos_map[0] == 0  # 'S'
        assert pos_map[9] == 9  # curly open quote maps to original pos 9

    def test_dash_variants(self):
        text = "range\u2014end"
        norm, _ = normalize_with_positions(text)
        assert norm == "range-end"

    def test_trailing_whitespace_stripped(self):
        text = "hello   \nworld"
        norm, pos_map = normalize_with_positions(text)
        assert norm == "hello\nworld"
        assert pos_map[5] == 8  # '\n' at original position 8

    def test_consecutive_spaces_collapsed(self):
        text = "hello    world"
        norm, pos_map = normalize_with_positions(text)
        assert norm == "hello world"
        assert pos_map[5] == 5  # first space kept
        assert pos_map[6] == 9  # 'w' at original position 9

    def test_leading_spaces_preserved(self):
        text = "    - item"
        norm, pos_map = normalize_with_positions(text)
        assert norm == "    - item"
        assert len(pos_map) == len(norm)
        assert pos_map[0] == 0
        assert pos_map[3] == 3
        assert pos_map[4] == 4  # '-'

    def test_mixed_leading_and_inline_spaces(self):
        text = "    hello    world"
        norm, pos_map = normalize_with_positions(text)
        assert norm == "    hello world"
        assert len(norm) == 15
        assert pos_map[9] == 9  # space after hello
        assert pos_map[10] == 13 # 'w'

    def test_empty_string(self):
        norm, pos_map = normalize_with_positions("")
        assert norm == ""
        assert pos_map == []

    def test_multiline_with_mixed_issues(self):
        text = "line1   \n  \u201chello\u201d   \nend"
        norm, pos_map = normalize_with_positions(text)
        assert norm == 'line1\n  "hello"\nend'
        assert len(pos_map) == len(norm)

    def test_crlf_normalized_to_lf(self):
        text = "line1\r\nline2\r\nline3"
        norm, pos_map = normalize_with_positions(text)
        assert norm == "line1\nline2\nline3"
        assert len(pos_map) == len(norm)
        assert pos_map[5] == 6  # '\n' maps to the original '\n' which is at index 6
        assert pos_map[6] == 7  # 'l' in line2

    def test_backtick_is_not_normalized(self):
        text = "Run `update` command."
        norm, pos_map = normalize_with_positions(text)
        assert norm == "Run `update` command."


# =============================================================================
# Unit tests for try_normalized_patch
# =============================================================================


class TestTryNormalizedPatch:
    def test_curly_to_straight_quote_patch(self):
        content = "She said \u201chello\u201d to him."
        result = try_normalized_patch(
            content, 'She said "hello"', 'She said "goodbye"'
        )
        assert result is not None
        assert '"goodbye"' in result
        assert "to him." in result

    def test_dash_variant_patch(self):
        content = "range: 10\u201420"
        result = try_normalized_patch(
            content, "range: 10-20", "range: 10-30"
        )
        assert result is not None
        assert "10-30" in result

    def test_trailing_whitespace_patch(self):
        content = "hello   \nworld"
        result = try_normalized_patch(
            content, "hello\nworld", "goodbye\nworld"
        )
        assert result is not None
        assert result.startswith("goodbye\n")

    def test_space_collapse_patch(self):
        content = "A    B    C"
        result = try_normalized_patch(
            content, "A B", "X Y"
        )
        assert result is not None
        assert result.startswith("X Y")
        assert result.endswith("C")

    def test_no_match_returns_none(self):
        result = try_normalized_patch(
            "hello world", "completely different", "replacement"
        )
        assert result is None

    def test_ambiguous_match_returns_none(self):
        content = 'He said "yes". She said "yes".'
        result = try_normalized_patch(
            content, '"yes"', '"no"'
        )
        assert result is None

    def test_exact_match_still_needed_when_no_normalization_diff(self):
        content = "hello world"
        result = try_normalized_patch(
            content, "helo world", "replacement"
        )
        assert result is None


# =============================================================================
# Integration: edit_memory with normalized fallback
# =============================================================================


@pytest.mark.asyncio
async def test_edit_memory_falls_back_to_normalized_patch(
    mcp_module, graph_service
):
    original_content = "Nocturne said \u201cI will not kneel.\u201d That is final."
    await graph_service.create_memory(
        parent_path="",
        content=original_content,
        priority=1,
        title="norm_test",
    )

    result = await mcp_module.edit_memory(
        "core://norm_test",
        old_text='Nocturne said "I will not kneel."',
        new_text='Nocturne said "I refuse to kneel."',
    )
    assert "core://norm_test" in result
    assert "改好" in result

    memory = await graph_service.get_memory_by_path("norm_test", "core")
    assert '"I refuse to kneel."' in memory["content"]
    assert "That is final." in memory["content"]


@pytest.mark.asyncio
async def test_edit_memory_exact_match_takes_priority(
    mcp_module, graph_service
):
    """Exact match must be used when available, not normalized."""
    await graph_service.create_memory(
        parent_path="",
        content="Hello World",
        priority=1,
        title="exact_test",
    )
    result = await mcp_module.edit_memory(
        "core://exact_test",
        old_text="Hello",
        new_text="Goodbye",
    )
    assert "改好" in result
    memory = await graph_service.get_memory_by_path("exact_test", "core")
    assert memory["content"] == "Goodbye World"


@pytest.mark.asyncio
async def test_edit_memory_normalized_ambiguous_is_not_found(
    mcp_module, graph_service
):
    """An ambiguous normalized match is rejected: edit_memory reports the
    text as not found rather than silently guessing."""
    content = "He said \u201cyes\u201d. She said \u201cyes\u201d."
    await graph_service.create_memory(
        parent_path="",
        content=content,
        priority=1,
        title="ambig_test",
    )
    result = await mcp_module.edit_memory(
        "core://ambig_test",
        old_text='"yes"',
        new_text='"no"',
    )
    # The ambiguous patch is rejected; edit_memory surfaces a not-found style
    # message and leaves the stored content untouched.
    assert "没找到" in result or "找不到" in result
    memory = await graph_service.get_memory_by_path("ambig_test", "core")
    assert memory["content"] == content


@pytest.mark.asyncio
async def test_remember_child_memory_preserves_all_content_verbatim(
    mcp_module, graph_service
):
    """remember_child_memory never normalizes - whatever you pass is stored as-is."""
    for title_suffix, content in [
        ("backslash", r"Windows path: C:\tmp\test and regex token: foo\\nbar"),
        ("escaped_multiline", "# Title\\n\\n- alpha\\n- beta"),
        ("single_token", r"Use \n to represent a newline in regex examples."),
        ("code_literal", 'payload = "line1\\n\\nline2"'),
        ("regex", "^(?:[A-Z]\\n)+$"),
        ("plain_prose", "line one\\nline two"),
    ]:
        result = await mcp_module.remember_child_memory(
            "core://",
            content,
            importance=1,
            when="When verifying verbatim storage",
            title=f"verbatim_{title_suffix}",
        )
        assert "core://" in result, f"Failed for {title_suffix}"
        assert "记住" in result, f"Failed for {title_suffix}"
        memory = await graph_service.get_memory_by_path(f"verbatim_{title_suffix}", "core")
        assert memory["content"] == content, f"Content mismatch for {title_suffix}"


@pytest.mark.asyncio
async def test_edit_memory_preserves_literal_backslash_sequences(
    mcp_module, graph_service
):
    original_content = r"Store C:\tmp\test and regex foo\\nbar literally."
    await graph_service.create_memory(
        parent_path="",
        content=original_content,
        priority=1,
        title="literal_backslash_update",
    )

    result = await mcp_module.edit_memory(
        "core://literal_backslash_update",
        old_text=r"C:\tmp\test",
        new_text=r"D:\logs\test",
    )

    assert "改好" in result
    memory = await graph_service.get_memory_by_path("literal_backslash_update", "core")
    assert memory["content"] == r"Store D:\logs\test and regex foo\\nbar literally."


@pytest.mark.asyncio
async def test_edit_memory_normalizes_escaped_newlines_on_patch_fallback(
    mcp_module, graph_service
):
    """When stored content has real newlines but old_text has literal \\n,
    exact match fails, then the literal-newline normalization fallback kicks in
    and applies the replacement against the ground-truth stored text."""
    await graph_service.create_memory(
        parent_path="",
        content="# Title\n\n- alpha\n- beta",
        priority=1,
        title="escaped_multiline_update",
    )

    result = await mcp_module.edit_memory(
        "core://escaped_multiline_update",
        old_text="# Title\\n\\n- alpha\\n- beta",
        new_text="# Title\\n\\n- gamma\\n- delta",
    )

    assert "改好" in result
    memory = await graph_service.get_memory_by_path("escaped_multiline_update", "core")
    assert memory["content"] == "# Title\n\n- gamma\n- delta"


@pytest.mark.asyncio
async def test_edit_memory_exact_match_wins_over_normalization(
    mcp_module, graph_service
):
    """When stored content literally contains \\n sequences, exact match
    succeeds and no normalization occurs."""
    literal_content = 'payload = "line1\\n\\nline2"'
    await graph_service.create_memory(
        parent_path="",
        content=literal_content,
        priority=1,
        title="literal_exact_match",
    )

    result = await mcp_module.edit_memory(
        "core://literal_exact_match",
        old_text='payload = "line1\\n\\nline2"',
        new_text='payload = "line1\\n\\nline2\\nline3"',
    )

    assert "改好" in result
    memory = await graph_service.get_memory_by_path("literal_exact_match", "core")
    assert memory["content"] == 'payload = "line1\\n\\nline2\\nline3"'


@pytest.mark.asyncio
async def test_edit_memory_append_preserves_content_verbatim(
    mcp_module, graph_service
):
    """Append mode never normalizes - whatever you pass is appended as-is."""
    await graph_service.create_memory(
        parent_path="",
        content="Base content",
        priority=1,
        title="append_verbatim",
    )

    result = await mcp_module.edit_memory(
        "core://append_verbatim",
        append="\\n\\nCode: foo\\n\\nbar",
    )

    assert "改好" in result
    memory = await graph_service.get_memory_by_path("append_verbatim", "core")
    assert memory["content"] == "Base content\\n\\nCode: foo\\n\\nbar"
