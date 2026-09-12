"""The two participant skills (issue #27).

A worker launched on a weak model read `skills/ccd-worker.md`, invented a task
about NixOS module systems — a topic that appears nowhere in this project —
spawned an agent for it and wrote a long design answer, all before announcing
anything. The skill described how to handle tasks and never said that none had
been given, so a model primed on "task" supplied one.

These check the properties that stop that, which is why they are mostly about
*ordering*: a weak model acts on what it reads first.

Deliberately not asserted: that the shared passages are byte-identical between
the two files. Each skill is written to be read start to finish by a model
that will not follow a pointer to another document, so the wording is
role-specific on purpose. What must not drift is checked below as facts, not
as text.
"""
from __future__ import annotations

import re

import pytest

SKILLS = ["ccd-worker", "ccd-dispatcher"]


def skill(repo_root, name: str) -> str:
    return (repo_root / "skills" / f"{name}.md").read_text(encoding="utf-8")


def frontmatter(text: str) -> str:
    assert text.startswith("---\n")
    return text.split("---\n", 2)[1]


def body(text: str) -> str:
    return text.split("---\n", 2)[2]


def flat(text: str) -> str:
    """Prose with its line wrapping collapsed.

    These files are hard-wrapped, so a sentence worth asserting on is usually
    split across two lines. Matching the wrapped form would make every
    assertion here break on a rewrap that changed nothing.
    """
    return " ".join(text.split())


# ----------------------------------------------------------------------
# both skills exist and are distinct roles
# ----------------------------------------------------------------------

@pytest.mark.parametrize("name", SKILLS)
def test_the_skill_exists(repo_root, name):
    assert (repo_root / "skills" / f"{name}.md").is_file()


@pytest.mark.parametrize("name", SKILLS)
def test_the_frontmatter_name_matches_the_filename(repo_root, name):
    assert re.search(rf"^name: {re.escape(name)}$", frontmatter(skill(repo_root, name)),
                     re.M)


def test_each_skill_points_at_the_other_for_the_role_it_is_not(repo_root):
    """A session that loaded the wrong one should be told which is right."""
    assert "ccd-dispatcher" in skill(repo_root, "ccd-worker")
    assert "ccd-worker" in skill(repo_root, "ccd-dispatcher")


# ----------------------------------------------------------------------
# "you have no task" comes before anything that could look like one
# ----------------------------------------------------------------------

@pytest.mark.parametrize("name", SKILLS)
def test_the_description_says_no_task_has_been_given(repo_root, name):
    """The description is the first thing a model sees, before the body."""
    desc = frontmatter(skill(repo_root, name))
    assert "NOT been given a task" in desc


@pytest.mark.parametrize("name", SKILLS)
def test_the_first_section_is_the_no_task_statement(repo_root, name):
    text = body(skill(repo_root, name))
    headings = re.findall(r"^## (.+)$", text, re.M)
    assert headings, "the skill has no sections"
    assert headings[0] == "You have not been given a task", headings[:3]


@pytest.mark.parametrize("name", SKILLS)
def test_nothing_task_shaped_precedes_it(repo_root, name):
    """Everything before that heading is the title and one identifying line.

    The failure this guards against is a model reading a plausible job off the
    top of the file. The shorter that region is, the less there is to misread.
    """
    text = body(skill(repo_root, name))
    before = text.split("## You have not been given a task", 1)[0]
    assert before.count("\n\n") <= 2, before
    for tempting in ("analyse", "design", "implement", "explore", "task:"):
        assert tempting not in before.lower(), (tempting, before)


@pytest.mark.parametrize("name", SKILLS)
def test_the_no_task_section_forbids_inventing_one(repo_root, name):
    section = body(skill(repo_root, name)).split(
        "## You have not been given a task", 1)[1].split("\n## ", 1)[0]
    assert "must not invent" in section
    # The specific behaviours the transcript showed, named so a model
    # pattern-matching on them is stopped rather than merely discouraged.
    for behaviour in ("analyse", "design", "explore", "spawn an agent"):
        assert behaviour in section.lower(), behaviour


@pytest.mark.parametrize("name", SKILLS)
def test_the_first_action_is_announce(repo_root, name):
    section = body(skill(repo_root, name)).split(
        "## You have not been given a task", 1)[1].split("\n## ", 1)[0]
    assert "first action" in section
    assert "\nccd announce\n" in section


# ----------------------------------------------------------------------
# the roles differ in where their input comes from
# ----------------------------------------------------------------------

def test_the_worker_lives_on_the_broker_queue(repo_root):
    text = skill(repo_root, "ccd-worker")
    assert "primary input is **the broker**" in flat(text)
    assert 'ccd recv "$CCD_HANDLE" -t 86400' in text
    assert "last tool call of every turn" in flat(text)


def test_the_worker_only_works_from_a_received_message(repo_root):
    text = skill(repo_root, "ccd-worker")
    assert "only after `ccd recv` has printed a message" in flat(text)


def test_the_dispatcher_lives_with_the_human(repo_root):
    text = skill(repo_root, "ccd-dispatcher")
    assert "primary input is the human" in flat(text)
    assert "Do not end every turn blocked in `ccd recv`" in flat(text)


def test_the_dispatcher_still_reads_its_own_queue(repo_root):
    """It collects worker replies — it just does not live there."""
    text = skill(repo_root, "ccd-dispatcher")
    assert 'ccd recv "$CCD_HANDLE"' in text


def test_only_the_dispatcher_hands_work_out(repo_root):
    """The commands that farm work out belong to one role.

    `ccd launch` is deliberately not in this list: the worker mentions it once,
    as the thing that started it ("if you were started by hand rather than by
    `ccd launch`"), which is a reference and not an instruction. The check
    below covers the instruction form instead.
    """
    dispatcher = skill(repo_root, "ccd-dispatcher")
    worker = skill(repo_root, "ccd-worker")
    for command in ("ccd claim", "ccd send <worker>"):
        assert command in dispatcher, command
        assert command not in worker, command


def test_the_worker_is_never_told_to_launch_anything(repo_root):
    worker = skill(repo_root, "ccd-worker")
    assert "ccd launch <" not in worker
    assert "ccd pick" not in worker


# ----------------------------------------------------------------------
# facts that must not drift between the two files
# ----------------------------------------------------------------------

@pytest.mark.parametrize("name", SKILLS)
def test_both_announce_with_no_arguments(repo_root, name):
    """`ccd launch` removes CCD_MODEL/CCD_EFFORT; restating them announced
    empty strings, which is the bug fixed in #13."""
    text = skill(repo_root, name)
    assert "$CCD_MAPPING" in text
    assert '"$CCD_MODEL"' not in text
    assert '"$CCD_EFFORT"' not in text


@pytest.mark.parametrize("name", SKILLS)
def test_both_retire_the_handle_on_the_way_out(repo_root, name):
    assert 'ccd ret "$CCD_HANDLE"' in skill(repo_root, name)


@pytest.mark.parametrize("name", SKILLS)
def test_both_describe_the_esc_requeue_the_same_way(repo_root, name):
    """An interrupt never drops a message — the claim both files make, and the
    one a reader most needs to be able to trust."""
    text = skill(repo_root, name)
    assert "re-queued to the front of the queue" in flat(text)
    assert "FIFO preserved" in flat(text)


@pytest.mark.parametrize("name", SKILLS)
def test_neither_reaches_around_the_cli(repo_root, name):
    assert "never touch `$CCD_SOCKET` directly" in flat(skill(repo_root, name))
