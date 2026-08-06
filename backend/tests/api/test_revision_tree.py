"""
Revision tree rollback tests.

Covers:
  1. approve produces an `author="ai"` revision.
  2. rollback produces a branch: an `ai` pre-snapshot + an `admin` rollback node.
  3. checkout rewinds to an ancestor revision and restores DB state.
  4. cross-branch checkout returns 501.

Uses the real MCP tool functions (remember_child_memory / edit_memory) which
record row-level before/after states into the ChangesetStore. Existing review
regression coverage lives in test_api_routes.py.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _groups(api_client):
    res = await api_client.get("/review/groups")
    assert res.status_code == 200
    return res.json()


async def _revisions(api_client):
    res = await api_client.get("/review/revisions")
    assert res.status_code == 200
    return res.json()


async def _create(mcp_module, title, content, when="When testing"):
    """Create a memory via the MCP layer (records to changeset). Returns URI."""
    result = await mcp_module.remember_child_memory(
        parent_uri="core://",
        content=content,
        importance=2,
        when=when,
        title=title,
    )
    assert "记住" in result, f"create failed: {result}"
    return f"core://{title}"


# ---------------------------------------------------------------------------
# 1. approve produces a revision
# ---------------------------------------------------------------------------

async def test_approve_produces_ai_revision(api_client, mcp_module):
    await _create(mcp_module, "approve_rev_test", "approve-test content")

    # No revisions before any approve.
    revs = await _revisions(api_client)
    assert revs["revisions"] == []

    groups = await _groups(api_client)
    node_uuid = groups[0]["node_uuid"]

    approved = await api_client.delete(f"/review/groups/{node_uuid}")
    assert approved.status_code == 200

    revs = await _revisions(api_client)
    assert len(revs["revisions"]) == 1
    rev = revs["revisions"][0]
    assert rev["author"] == "ai"
    assert rev["is_head"] is True
    assert revs["head_revision_id"] == rev["id"]


# ---------------------------------------------------------------------------
# 2. rollback produces a branch (ai snapshot + admin rollback)
# ---------------------------------------------------------------------------

async def test_rollback_produces_admin_branch(api_client, graph_service, mcp_module):
    # Create + approve (first revision), then edit to produce a reviewable change.
    await _create(mcp_module, "rollback_rev_test", "rollback original")
    groups = await _groups(api_client)
    await api_client.delete(f"/review/groups/{groups[0]['node_uuid']}")

    await mcp_module.edit_memory(
        "core://rollback_rev_test",
        old_text="rollback original",
        new_text="rollback original\npost-edit",
    )

    groups = await _groups(api_client)
    node_uuid = groups[0]["node_uuid"]

    rollback = await api_client.post(f"/review/groups/{node_uuid}/rollback")
    assert rollback.status_code == 200
    assert rollback.json()["success"] is True

    revs = await _revisions(api_client)
    revisions = revs["revisions"]
    assert len(revisions) >= 2

    # The latest revision is the admin rollback.
    admin_revs = [r for r in revisions if r["author"] == "admin"]
    assert len(admin_revs) >= 1
    admin_rev = admin_revs[-1]
    assert admin_rev["is_head"] is True
    assert revs["head_revision_id"] == admin_rev["id"]

    # The admin rollback's parent must be the ai pre-snapshot.
    assert admin_rev["parent_id"] is not None
    parent = next(r for r in revisions if r["id"] == admin_rev["parent_id"])
    assert parent["author"] == "ai"

    # DB content reverted to original.
    current = await graph_service.get_memory_by_path("rollback_rev_test", "core")
    assert current["content"] == "rollback original"

    # Groups cleared.
    groups_after = await _groups(api_client)
    assert groups_after == []


# ---------------------------------------------------------------------------
# 3. checkout rewinds to an ancestor
# ---------------------------------------------------------------------------

async def test_checkout_rewinds_to_ancestor(api_client, graph_service, mcp_module):
    # v1: create + approve -> rev1 (ai)
    await _create(mcp_module, "checkout_rev_test", "v1")
    groups = await _groups(api_client)
    await api_client.delete(f"/review/groups/{groups[0]['node_uuid']}")

    revs = await _revisions(api_client)
    assert len(revs["revisions"]) == 1
    rev1 = revs["revisions"][0]
    assert rev1["author"] == "ai"

    # v2: edit + approve -> rev2 (ai), parent=rev1
    await mcp_module.edit_memory(
        "core://checkout_rev_test",
        old_text="v1",
        new_text="v2",
    )
    groups = await _groups(api_client)
    await api_client.delete(f"/review/groups/{groups[0]['node_uuid']}")

    revs = await _revisions(api_client)
    assert len(revs["revisions"]) == 2
    rev2 = revs["revisions"][-1]
    assert rev2["parent_id"] == rev1["id"]
    assert revs["head_revision_id"] == rev2["id"]

    # Verify DB is at v2.
    current = await graph_service.get_memory_by_path("checkout_rev_test", "core")
    assert "v2" in current["content"]

    # Checkout rev1 (ancestor of HEAD=rev2).
    checkout = await api_client.post(f"/review/revisions/{rev1['id']}/checkout")
    assert checkout.status_code == 200
    assert checkout.json()["success"] is True

    # DB content restored to v1.
    current = await graph_service.get_memory_by_path("checkout_rev_test", "core")
    assert current["content"] == "v1"

    # HEAD now points at the new checkout revision; rev1 and rev2 still exist.
    revs = await _revisions(api_client)
    assert len(revs["revisions"]) == 3
    ids = [r["id"] for r in revs["revisions"]]
    assert rev1["id"] in ids
    assert rev2["id"] in ids
    head_id = revs["head_revision_id"]
    head_rev = next(r for r in revs["revisions"] if r["id"] == head_id)
    assert head_rev["author"] == "admin"
    assert head_rev["parent_id"] == rev1["id"]


# ---------------------------------------------------------------------------
# 4. cross-branch checkout returns 501
# ---------------------------------------------------------------------------

async def test_cross_branch_checkout_returns_501(api_client, mcp_module):
    # Build two sibling branches off rev1:
    #   rev1 (ai, root) -- approve create
    #   rev2 (ai)       -- pre-rollback snapshot of an edit
    #   rev3 (admin)    -- rollback, parent=rev2. HEAD=rev3.
    #   rev4 (admin)    -- checkout rev1, parent=rev1. HEAD=rev4 (new branch).
    # Now rev3 is on a sibling branch (rev1->rev2->rev3) that is NOT an
    # ancestor of HEAD=rev4 (rev1->rev4). Checkout to rev3 must 501.
    await _create(mcp_module, "cross_branch_test", "branch base")
    groups = await _groups(api_client)
    await api_client.delete(f"/review/groups/{groups[0]['node_uuid']}")  # rev1

    await mcp_module.edit_memory(
        "core://cross_branch_test",
        old_text="branch base",
        new_text="branch A",
    )
    groups = await _groups(api_client)
    await api_client.post(f"/review/groups/{groups[0]['node_uuid']}/rollback")  # rev2+rev3

    revs = await _revisions(api_client)
    rev1 = next(r for r in revs["revisions"] if r["parent_id"] is None)

    # Checkout rev1 to start a new branch.
    co = await api_client.post(f"/review/revisions/{rev1['id']}/checkout")
    assert co.status_code == 200

    # Now HEAD is on the rev1->rev4 branch. rev3 (on rev1->rev2->rev3) is a
    # sibling, not an ancestor. Checkout to rev3 must 501.
    rev3 = next(r for r in revs["revisions"] if r["author"] == "admin")
    checkout = await api_client.post(f"/review/revisions/{rev3['id']}/checkout")
    assert checkout.status_code == 501
