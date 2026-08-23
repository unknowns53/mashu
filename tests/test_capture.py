"""The unattended half (specifications 16.3, 25.1, 30 段 A/B).

What is being tested here is mostly what the worker refuses to do. It runs when
nobody is watching, so the interesting properties are negative: it does not
guess a scope, it does not let anything it inferred fall out of the store, it
does not claim to be the user, and it does not read the same transcript twice.
"""

from __future__ import annotations

import json

import pytest

from mashu import extract, proposals, routing, runs, store, transcript, worker
from mashu.models import EntityStatus, MemoryType, ProposalStatus, SourceType, VersionStatus

# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
CLAUDE_TURNS = [
    {"type": "user", "sessionId": "s-1", "cwd": "/work/proj", "message": {"content": "第一の質問"}},
    {
        "type": "assistant",
        "sessionId": "s-1",
        "cwd": "/work/proj",
        "message": {"content": [{"type": "text", "text": "第一の答え"}]},
    },
    {"type": "user", "sessionId": "s-1", "cwd": "/work/proj", "message": {"content": "第二の質問"}},
]


def write_claude(path, turns=None, name="s-1.jsonl"):
    file = path / name
    file.write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in (turns or CLAUDE_TURNS)) + "\n",
        encoding="utf-8",
    )
    return file


@pytest.fixture
def route(cur, scope_id):
    routing.add(cur, path_prefix="/work/proj", scope_id=scope_id, created_by="user")
    return scope_id


@pytest.fixture
def queued(cur, tmp_path):
    """One transcript claimed on the ledger, the way a session-end hook leaves it."""

    def _queued(file, cwd="/work/proj", session="s-1"):
        return runs.enqueue(
            cur,
            source_cli="claude",
            external_session_id=session,
            transcript_digest=transcript.digest(file),
            extractor_version=extract.EXTRACTOR_VERSION,
            transcript_path=str(file),
            cwd=cwd,
        )

    return _queued


def answer(proposals_=(), retirements=()):
    return json.dumps(
        {"proposals": list(proposals_), "retirements": list(retirements), "scratch": []},
        ensure_ascii=False,
    )


def a_proposal(**over):
    base = {
        "type": "decision",
        "title": "何を測るか先に決める",
        "content": "測定を起動する前に、結果ごとの行動を書き出す",
        "rationale": "判定基準 1",
        "commit_gate": "auto",
        "evidence": [],
        "duplicates": [],
    }
    return {**base, **over}


# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------
def test_the_longest_route_wins_so_a_subtree_can_be_split_off(cur, scope_id):
    other = store.create_scope(cur, name="inner", actor="user")
    routing.add(cur, path_prefix="/work", scope_id=scope_id, created_by="user")
    routing.add(cur, path_prefix="/work/proj/docs", scope_id=other, created_by="user")

    assert routing.resolve(cur, "/work/proj") == scope_id
    assert routing.resolve(cur, "/work/proj/docs/notes") == other


def test_a_route_matches_whole_segments_not_a_string_prefix(cur, scope_id):
    """/work/proj must not answer for /work/proj-old, which is a different project."""
    routing.add(cur, path_prefix="/work/proj", scope_id=scope_id, created_by="user")
    assert routing.resolve(cur, "/work/proj-old") is None
    assert routing.resolve(cur, "/work/proj/src") == scope_id


def test_an_unmapped_directory_resolves_to_nothing_rather_than_the_nearest_scope(cur, scope_id):
    routing.add(cur, path_prefix="/work/proj", scope_id=scope_id, created_by="user")
    assert routing.resolve(cur, "/elsewhere") is None
    assert routing.resolve(cur, None) is None


# --------------------------------------------------------------------------
# transcripts
# --------------------------------------------------------------------------
def test_a_claude_transcript_becomes_turns_carrying_their_record_ordinals(tmp_path):
    session = transcript.read(write_claude(tmp_path))
    assert [t.role for t in session.turns] == ["user", "assistant", "user"]
    assert [t.ordinal for t in session.turns] == [1, 2, 3]
    assert session.cwd == "/work/proj"
    assert session.external_id == "s-1"


def test_a_codex_transcript_reads_the_user_prompt_not_the_developer_preamble(tmp_path):
    file = tmp_path / "rollout.jsonl"
    file.write_text(
        "\n".join(
            json.dumps(r, ensure_ascii=False)
            for r in [
                {"type": "session_meta", "payload": {"session_id": "c-1", "cwd": "/work/proj"}},
                {
                    "type": "response_item",
                    "payload": {"type": "message", "role": "developer", "content": []},
                },
                {"type": "event_msg", "payload": {"type": "user_message", "message": "本当の質問"}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "答え"}},
            ]
        ),
        encoding="utf-8",
    )
    session = transcript.read(file)
    assert session.source_cli == "codex"
    assert session.external_id == "c-1"
    assert [t.text for t in session.turns] == ["本当の質問", "答え"]


def test_a_checkpoint_slices_the_file_so_a_session_is_not_paid_for_twice(tmp_path):
    session = transcript.read(write_claude(tmp_path))
    assert [t.text for t in session.since(2)] == ["第二の質問"]
    assert len(session.since(None)) == 3


def test_peek_finds_the_session_and_directory_without_reading_the_whole_file(tmp_path):
    """The hook runs in an exit path measured in seconds."""
    peeked = transcript.peek(write_claude(tmp_path))
    assert (peeked.external_id, peeked.cwd) == ("s-1", "/work/proj")
    assert peeked.turns == []


# --------------------------------------------------------------------------
# reading the model's answer
# --------------------------------------------------------------------------
def test_a_type_outside_the_vocabulary_is_refused_and_the_refusal_is_kept():
    got = extract.parse(answer([a_proposal(type="guess")]))
    assert got.proposals == []
    assert "not a memory type" in got.refused[0]


def test_a_retirement_aimed_outside_what_the_model_was_shown_is_refused():
    """Either a hallucinated id or one from another scope. Both reach past the input."""
    import uuid

    known, unknown = uuid.uuid4(), uuid.uuid4()
    got = extract.parse(
        answer(
            retirements=[
                {"memory_id": str(known), "target": "completed", "reason": "done"},
                {"memory_id": str(unknown), "target": "completed", "reason": "done"},
            ]
        ),
        known_ids={known},
    )
    assert [r.memory_id for r in got.retirements] == [known]
    assert "was not in the active set" in got.refused[0]


def test_disproven_without_a_reason_is_refused():
    import uuid

    known = uuid.uuid4()
    got = extract.parse(
        answer(retirements=[{"memory_id": str(known), "target": "disproven", "reason": ""}]),
        known_ids={known},
    )
    assert got.retirements == []
    assert "requires one" in got.refused[0]


def test_the_gate_the_model_names_is_recorded_and_not_obeyed():
    """A model that could nominate its own commit line would write its own rules."""
    got = extract.parse(answer([a_proposal(commit_gate="auto")]))
    assert got.proposals[0].claimed_gate == "auto"


def test_an_answer_wrapped_in_prose_or_a_fence_still_reads():
    raw = "ここに出力します。\n```json\n" + answer([a_proposal()]) + "\n```\nいかがでしょうか"
    assert len(extract.parse(raw).proposals) == 1


# --------------------------------------------------------------------------
# the worker
# --------------------------------------------------------------------------
def test_an_unmapped_directory_holds_the_run_instead_of_guessing(cur, tmp_path, queued):
    run = queued(write_claude(tmp_path))
    got = worker.process(cur, run, extractor=extract.StubExtractor(answer()))

    assert got.state == "held"
    assert "no scope route" in got.note
    assert runs.health(cur)["held"] == 1
    assert not runs.health(cur)["ok"]


def test_a_route_added_later_releases_what_was_held(cur, tmp_path, queued, scope_id):
    run = queued(write_claude(tmp_path))
    worker.process(cur, run, extractor=extract.StubExtractor(answer()))

    routing.add(cur, path_prefix="/work/proj", scope_id=scope_id, created_by="user")
    assert runs.release(cur, cwd_prefix="/work/proj") == 1
    assert runs.claim(cur, limit=1)[0]["run_id"] == run["run_id"]


def test_a_session_with_nothing_in_it_is_skipped_and_the_skip_is_recorded(
    cur, tmp_path, queued, route
):
    """A skip nobody can see is the same as a silent failure (16.3)."""
    file = write_claude(
        tmp_path,
        [{"type": "user", "sessionId": "s-1", "cwd": "/work/proj", "message": {"content": "ok"}}],
    )
    got = worker.process(cur, queued(file), extractor=extract.StubExtractor(answer()))

    assert got.state == "skipped"
    assert runs.health(cur)["skipped"] == 1


def test_an_explicit_request_to_remember_survives_the_size_heuristic(cur, tmp_path, queued, route):
    file = write_claude(
        tmp_path,
        [
            {
                "type": "user",
                "sessionId": "s-1",
                "cwd": "/work/proj",
                "message": {"content": "これは覚えておいて"},
            }
        ],
    )
    stub = extract.StubExtractor(answer([a_proposal()]))
    got = worker.process(cur, queued(file), extractor=stub)

    assert got.state == "succeeded"
    assert got.proposals_filed == 1


def test_what_the_worker_files_is_agent_sourced_and_waits(cur, tmp_path, queued, route):
    """16.3: reading a user's turn proves where text was written, not who wrote it."""
    stub = extract.StubExtractor(answer([a_proposal(type="preference", source_type="user")]))
    got = worker.process(cur, queued(write_claude(tmp_path)), extractor=stub)
    assert got.proposals_filed == 1

    cur.execute("SELECT * FROM proposal WHERE actor = %s", (worker.WORKER_ACTOR,))
    proposal = cur.fetchone()
    assert proposal["status"] == str(ProposalStatus.PENDING)

    entity = store.get_entity(cur, proposal["target_memory"])
    assert entity["active_version"] is None
    version = store.get_version(cur, store.get_entity(cur, entity["memory_id"])["latest_version"])
    assert version["source_type"] == str(SourceType.AGENT)


def test_a_task_completion_the_worker_inferred_does_not_commit_itself(cur, tmp_path, queued, route):
    """Section 17 puts a simple task completion on the auto line, for a person
    saying so. 段 B keeps the worker's inference off it: a wrong retirement is
    a thing that stopped appearing, and nobody searches for what they forgot.
    """
    memory_id, version_id = store.create_entity(
        cur,
        scope_id=route,
        type=MemoryType.TASK,
        title="移植を終える",
        content="native の記憶から移植する",
        source_type=SourceType.USER,
        created_by="user",
        actor="user",
        adopt=True,
    )
    stub = extract.StubExtractor(
        answer(retirements=[{"memory_id": str(memory_id), "target": "completed", "reason": "済"}])
    )
    got = worker.process(cur, queued(write_claude(tmp_path)), extractor=stub)

    assert got.retirements_filed == 1
    assert store.get_entity(cur, memory_id)["active_version"] == version_id
    assert store.get_version(cur, version_id)["status"] == str(VersionStatus.CANDIDATE)

    cur.execute("SELECT status, decision_reason FROM proposal WHERE operation = 'change_status'")
    row = cur.fetchone()
    assert row["status"] == str(ProposalStatus.PENDING)
    assert "段 B" in row["decision_reason"]


def test_a_retirement_a_reader_should_weigh_rides_along_with_the_content(
    cur, tmp_path, queued, route
):
    """21.1 / 段 B: still current, but somebody has argued it is finished."""
    from mashu import retrieval

    memory_id, _ = store.create_entity(
        cur,
        scope_id=route,
        type=MemoryType.TASK,
        title="移植を終える",
        content="native の記憶から移植する",
        source_type=SourceType.USER,
        created_by="user",
        actor="user",
        adopt=True,
    )
    stub = extract.StubExtractor(
        answer(
            retirements=[
                {"memory_id": str(memory_id), "target": "completed", "reason": "この夜に終えた"}
            ]
        )
    )
    worker.process(cur, queued(write_claude(tmp_path)), extractor=stub)

    got = retrieval.retrieve(cur, "native の記憶から移植する", actor="claude", scope_id=route)
    row = next(r for r in got.active if r["memory_id"] == memory_id)
    assert row["proposed_status"] == "completed"
    assert row["proposed_reason"] == "この夜に終えた"
    assert row["tag"] == retrieval.RETIREMENT_PROPOSED_TAG


def test_a_title_that_already_exists_becomes_provisional_rather_than_being_dropped(
    cur, tmp_path, queued, route
):
    """Nobody is present to make section 20's call, and provisional is what waits."""
    store.create_entity(
        cur,
        scope_id=route,
        type=MemoryType.DECISION,
        title="何を測るか先に決める",
        content="先に決める",
        source_type=SourceType.USER,
        created_by="user",
        actor="user",
        adopt=True,
    )
    stub = extract.StubExtractor(answer([a_proposal()]))
    got = worker.process(cur, queued(write_claude(tmp_path)), extractor=stub)

    assert got.proposals_filed == 1
    cur.execute("SELECT target_memory FROM proposal WHERE actor = %s", (worker.WORKER_ACTOR,))
    filed = store.get_entity(cur, cur.fetchone()["target_memory"])
    assert filed["status"] == str(EntityStatus.PROVISIONAL)


def test_a_successful_run_marks_how_far_it_read_and_empties_the_scratch(
    cur, tmp_path, queued, route
):
    file = write_claude(tmp_path)
    cur.execute(
        "INSERT INTO agent_session (agent, source_cli, external_session_id, scratch) "
        "VALUES ('claude', 'claude', 's-1', %s)",
        (json.dumps([{"kind": "note", "content": "書き留めたこと"}]),),
    )
    stub = extract.StubExtractor(answer([a_proposal()]))
    worker.process(cur, queued(file), extractor=stub)

    assert "書き留めたこと" in stub.prompts[0]
    assert runs.checkpoint_for(cur, source_cli="claude", external_session_id="s-1") == 3
    cur.execute("SELECT scratch FROM agent_session WHERE external_session_id = 's-1'")
    assert cur.fetchone()["scratch"] is None


def test_a_second_run_only_reads_what_arrived_after_the_first(cur, tmp_path, queued, route):
    file = write_claude(tmp_path)
    stub = extract.StubExtractor(answer())
    worker.process(cur, queued(file), extractor=stub)

    grown = CLAUDE_TURNS + [
        {
            "type": "user",
            "sessionId": "s-1",
            "cwd": "/work/proj",
            "message": {"content": "あとから来た長い話" * 120},
        }
    ]
    file = write_claude(tmp_path, grown)
    worker.process(cur, queued(file), extractor=stub)

    assert "第一の質問" not in stub.prompts[1]
    assert "あとから来た長い話" in stub.prompts[1]


def test_the_daily_budget_defers_rather_than_dropping_and_keeps_the_attempt(
    cur, tmp_path, queued, route, monkeypatch
):
    monkeypatch.setattr(runs, "DAILY_INPUT_BUDGET", 1)
    run = queued(write_claude(tmp_path))
    cur.execute("UPDATE extraction_run SET attempts = 1 WHERE run_id = %s", (run["run_id"],))

    got = worker.process(cur, run, extractor=extract.StubExtractor(answer()))
    assert got.state == "deferred"

    cur.execute("SELECT state, attempts FROM extraction_run WHERE run_id = %s", (run["run_id"],))
    row = cur.fetchone()
    assert (row["state"], row["attempts"]) == ("retrying", 0)


def test_a_transcript_that_left_disk_is_recorded_as_skipped_not_retried_forever(
    cur, tmp_path, queued, route
):
    file = write_claude(tmp_path)
    run = queued(file)
    file.unlink()
    got = worker.process(cur, run, extractor=extract.StubExtractor(answer()))
    assert got.state == "skipped"


def test_evidence_named_inside_one_batch_is_joined_up(cur, tmp_path, queued, route):
    stub = extract.StubExtractor(
        answer(
            [
                a_proposal(
                    title="観測", type="observation", content="測定が二度とも同じ値になった"
                ),
                a_proposal(
                    title="解釈",
                    type="interpretation",
                    content="装置側の問題ではない",
                    evidence=["観測"],
                ),
            ]
        )
    )
    worker.process(cur, queued(write_claude(tmp_path)), extractor=stub)

    cur.execute("SELECT memory_id, latest_version FROM memory_entity WHERE title = '解釈'")
    row = cur.fetchone()
    grounds = store.evidence_for(cur, row["latest_version"])
    assert [g["title"] for g in grounds] == ["観測"]


def test_the_gate_is_read_from_the_source_even_when_the_model_asks_otherwise(
    cur, tmp_path, queued, route
):
    """The one path that would let an agent write its own standing instructions."""
    stub = extract.StubExtractor(
        answer(
            [
                a_proposal(
                    type="preference",
                    commit_gate="auto",
                    title="常に日本語で答える",
                    content="応答は日本語で書く",
                )
            ]
        )
    )
    worker.process(cur, queued(write_claude(tmp_path)), extractor=stub)

    cur.execute("SELECT status FROM proposal WHERE actor = %s", (worker.WORKER_ACTOR,))
    assert cur.fetchone()["status"] == str(ProposalStatus.PENDING)


# --------------------------------------------------------------------------
# what a reviewer does with it afterwards
# --------------------------------------------------------------------------
def test_putting_a_proposal_off_is_a_decision_with_a_reason(cur, scope_id):
    made = proposals.propose(
        cur,
        actor="claude",
        operation=proposals.ProposalOperation.CREATE,
        payload={
            "scope_id": str(scope_id),
            "type": str(MemoryType.FACT),
            "title": "何か",
            "content": "本文",
            "source_type": str(SourceType.AGENT),
        },
    )
    proposal_id = made["proposal"]["proposal_id"]
    proposals.defer(cur, proposal_id, reviewer="user", note="根拠を確かめてから")

    bundle = proposals.session_queue(cur)[0]
    item = next(i for i in bundle["proposals"] if i["proposal_id"] == proposal_id)
    assert item["deferred_at"] is not None
    assert item["review_note"] == "根拠を確かめてから"
    assert bundle["deferred"] == bundle["count"]


def test_a_session_too_long_for_one_call_is_read_in_windows_from_the_front(
    cur, tmp_path, queued, route, monkeypatch
):
    """Windows at turn boundaries, with the ledger's mark as the cursor (16.3).

    From the front rather than the tail: the cheap version reads the last of a
    long evening and silently loses the beginning, which is where the decisions
    the rest of it rests on were made.
    """
    monkeypatch.setattr(worker, "MAX_INPUT_TOKENS", 200)
    long_turns = [
        {
            "type": "user",
            "sessionId": "s-1",
            "cwd": "/work/proj",
            "message": {"content": f"{n} 番目の話 " + "本文 " * 80},
        }
        for n in range(4)
    ]
    stub = extract.StubExtractor(answer())
    run = queued(write_claude(tmp_path, long_turns))

    first = worker.process(cur, run, extractor=stub)
    assert first.state == "windowed"
    assert "0 番目の話" in stub.prompts[0]
    assert "3 番目の話" not in stub.prompts[0]

    claimed = runs.claim(cur, limit=1)[0]
    second = worker.process(cur, claimed, extractor=stub)
    assert "0 番目の話" not in stub.prompts[1]
    assert second.state in ("windowed", "succeeded")


def test_a_window_costs_no_retry_attempt(cur, tmp_path, queued, route, monkeypatch):
    """The attempt counter bounds broken transcripts, not long ones."""
    monkeypatch.setattr(worker, "MAX_INPUT_TOKENS", 200)
    long_turns = [
        {
            "type": "user",
            "sessionId": "s-1",
            "cwd": "/work/proj",
            "message": {"content": f"{n} 番目 " + "本文 " * 80},
        }
        for n in range(6)
    ]
    run = queued(write_claude(tmp_path, long_turns))
    for _ in range(3):
        claimed = runs.claim(cur, limit=1)
        if not claimed:
            break
        worker.process(cur, claimed[0], extractor=extract.StubExtractor(answer()))

    cur.execute("SELECT attempts, state FROM extraction_run WHERE run_id = %s", (run["run_id"],))
    row = cur.fetchone()
    assert row["attempts"] <= runs.MAX_ATTEMPTS


def test_a_night_of_extraction_is_one_bundle_rather_than_loose_items(cur, tmp_path, queued, route):
    """18.1 reviews a session at a time; without one, every item is read cold."""
    stub = extract.StubExtractor(
        answer([a_proposal(title="ひとつ目"), a_proposal(title="ふたつ目", content="別の本文")])
    )
    worker.process(cur, queued(write_claude(tmp_path)), extractor=stub)

    bundles = proposals.session_queue(cur)
    assert len(bundles) == 1
    assert bundles[0]["session_id"] is not None
    assert bundles[0]["count"] == 2


def test_the_sweeper_leaves_subagent_transcripts_alone(tmp_path, monkeypatch, committing_dsn):
    """A sidechain is not a session; its records are dropped and the file arrives empty."""
    root = tmp_path / "projects" / "proj"
    (root / "subagents").mkdir(parents=True)
    write_claude(root, name="parent.jsonl")
    write_claude(root / "subagents", name="agent-1.jsonl")

    found = worker.sweep(committing_dsn, roots={"claude": str(tmp_path / "projects")})
    assert [r["transcript_path"] for r in found] == [str(root / "parent.jsonl")]


def test_a_dry_run_gives_the_claim_back_instead_of_stranding_it(cur, tmp_path, queued, route):
    """No query counts a running row, so one left behind is never read again."""
    run = queued(write_claude(tmp_path))
    cur.execute(
        "UPDATE extraction_run SET state = 'running', attempts = 1 WHERE run_id = %s",
        (run["run_id"],),
    )
    got = worker.process(cur, run, extractor=extract.StubExtractor(answer()), dry_run=True)

    assert got.state == "dry-run"
    cur.execute("SELECT state, attempts FROM extraction_run WHERE run_id = %s", (run["run_id"],))
    assert cur.fetchone() == {"state": "queued", "attempts": 0}


def test_a_worker_that_died_holding_a_run_does_not_hide_it_forever(cur, tmp_path, queued):
    """The one silent failure the ledger could produce by itself."""
    run = queued(write_claude(tmp_path))
    cur.execute(
        "UPDATE extraction_run SET state = 'running', "
        "created_at = now() - interval '6 hours' WHERE run_id = %s",
        (run["run_id"],),
    )
    state = runs.health(cur)
    assert state["stranded"] == 1
    assert not state["ok"]
    assert "a worker died holding them" in state["warning"]
    assert [r["run_id"] for r in runs.claim(cur, limit=1)] == [run["run_id"]]
