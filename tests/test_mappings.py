"""The mapping manifest contract (ADR-0009, issue #13).

Everything here runs against manifests written into a temporary directory, so
the suite never reads the machine's real `~/.config/ccd/mappings.json` and
never needs a broker socket.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ccd_mappings import manifest as m

# The worked example from README.md. `test_readme_example_*` re-reads it out
# of the README and compares, so the two cannot drift apart without this
# suite saying so. It carries all four shapes the contract has to handle:
# first-party with an effort, first-party with none, a remapped model with
# the `[1m]` suffix, and a remapped model with no effort.
EXAMPLE = {
    "schema": 1,
    "mappings": [
        {
            "launcher": "claude",
            "model": "claude-sonnet-5",
            "effort": "medium",
            "billing": "sub",
        },
        {
            "launcher": "claude",
            "model": "claude-opus-5",
            "effort": "high",
            "billing": "sub",
        },
        {
            "launcher": "claude",
            "model": "claude-haiku-4-5",
            "billing": "sub",
            "notes": "no effort: this model never receives one, so claiming a value would be fiction",
        },
        {
            "launcher": "claude-openrouter",
            "model": "moonshotai/kimi-k3[1m]",
            "effort": "max",
            "billing": "api",
            "notes": "effort set by hand from benchmark reading; [1m] lifts the assumed 200k window",
        },
        {
            "launcher": "claude-openrouter",
            "model": "deepseek/deepseek-v4.1-flash",
            "billing": "api",
        },
    ],
}


def entry(**over) -> dict:
    base = {"launcher": "claude", "model": "claude-sonnet-5", "effort": "medium"}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def doc(*items) -> dict:
    return {"schema": 1, "mappings": list(items) or [entry()]}


def only(problems: list, needle: str) -> bool:
    return len(problems) == 1 and needle in problems[0]


def write(tmp: Path, name: str, text) -> Path:
    path = tmp / name
    path.write_text(
        text if isinstance(text, str) else json.dumps(text, indent=2),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def readme_text(repo_root) -> str:
    return (repo_root / "README.md").read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# the worked example in README.md is valid
# ----------------------------------------------------------------------

def test_example_validates_clean():
    assert m.validate(EXAMPLE) == [], repr(m.validate(EXAMPLE))


def test_readme_carries_exactly_one_json_block(readme_text):
    blocks = re.findall(r"```json\n(.*?)```", readme_text, re.S)
    assert len(blocks) == 1, f"found {len(blocks)}"


def test_readmes_example_is_the_one_tested_here(readme_text):
    blocks = re.findall(r"```json\n(.*?)```", readme_text, re.S)
    assert len(blocks) == 1, f"found {len(blocks)}"
    published = json.loads(blocks[0])
    assert published == EXAMPLE, "README.md and EXAMPLE have drifted apart"
    assert m.validate(published) == [], repr(m.validate(published))


def test_it_carries_a_first_party_entry():
    assert any(e["launcher"] == "claude" for e in EXAMPLE["mappings"])


def test_it_carries_a_remapped_entry():
    assert any(e["launcher"] != "claude" for e in EXAMPLE["mappings"])


def test_it_carries_an_entry_with_no_effort():
    assert any("effort" not in e for e in EXAMPLE["mappings"])


def test_no_entry_stores_an_id_a_label_or_a_slot():
    assert not any(
        k in e for e in EXAMPLE["mappings"] for k in ("id", "display", "slot"))


def test_readme_shows_the_rendered_picker_listing(readme_text):
    listing = re.search(r"A picker\nrenders it as:\n\n```\n(.*?)```", readme_text, re.S)
    assert listing is not None
    shown = [line.split(". ", 1)[1]
             for line in listing.group(1).strip().splitlines()]
    derived = m.labels(EXAMPLE)
    assert shown == derived, f"README {shown!r} != derived {derived!r}"


@pytest.mark.parametrize("ident", m.ids(EXAMPLE))
def test_readme_names_every_derived_id(readme_text, ident):
    assert f"`{ident}`" in readme_text


# ----------------------------------------------------------------------
# the model is named directly; there is no slot
# ----------------------------------------------------------------------
# ADR-0009's revision: a full provider slug is a valid --model argument, so
# the slot was only ever one route to a model. The field is gone, and a file
# still carrying it predates this contract.

def test_slot_is_not_a_stored_key():
    assert "slot" not in m.REQUIRED + m.OPTIONAL


def test_a_leftover_slot_is_refused_not_ignored():
    problems = m.validate(doc(entry(slot="opus")))
    assert only(problems, "'slot' is no longer a field")
    assert "regenerate" in problems[0]


def test_a_leftover_id_is_refused():
    assert only(m.validate(doc(entry(id="sonnet-5-medium"))), "'id' is no longer")


def test_a_leftover_display_is_refused():
    assert only(m.validate(doc(entry(display="Opus/High"))), "'display' is no longer")


def test_a_provider_slug_is_an_ordinary_model_value():
    assert m.validate(doc(entry(model="deepseek/deepseek-v4.1-flash"))) == []


# ----------------------------------------------------------------------
# effort is optional, because for some models it is never sent
# ----------------------------------------------------------------------

def test_an_entry_with_no_effort_is_valid():
    assert m.validate(doc(entry(effort=None))) == []


@pytest.mark.parametrize("effort", m.EFFORTS)
def test_every_valid_effort_is_accepted(effort):
    assert m.validate(doc(entry(effort=effort))) == []


def test_effort_extreme_refused():
    assert only(m.validate(doc(entry(effort="extreme"))), "'effort'")


def test_effort_capital_max_refused_case_matters():
    assert only(m.validate(doc(entry(effort="Max"))), "'effort'")


def test_an_empty_effort_is_refused_rather_than_treated_as_absent():
    # Absent means "none is sent". An empty string is a value that says
    # nothing, which is the fiction the optional field exists to avoid.
    assert only(m.validate(doc(entry(effort=""))), "omitted entirely")


def test_a_null_effort_means_absent_like_an_omitted_key():
    # `null` is how a transcript records "no effort was sent", so it says the
    # same thing as an absent key and is accepted as such.
    assert m.validate(doc({"launcher": "claude", "model": "x",
                           "effort": None})) == []


def test_a_null_effort_renders_as_an_entry_with_no_effort():
    assert m.display({"model": "x", "effort": None}) == "x"


# ----------------------------------------------------------------------
# billing is optional, and stored rather than derived (hosting#131 phase 4
# follow-up: ADR-0002's handle shape needs it back, but as a fact about the
# route, not a slot). Same null-absent/empty-refused shape as effort above,
# by the same rule and for the same reason.
# ----------------------------------------------------------------------

def test_an_entry_with_no_billing_is_valid():
    assert m.validate(doc(entry(billing=None))) == []


@pytest.mark.parametrize("billing", m.BILLING)
def test_every_valid_billing_is_accepted(billing):
    assert m.validate(doc(entry(billing=billing))) == []


def test_billing_uppercase_refused_case_matters():
    # Lowercase throughout, matching hosting's own ccd-launch.j2 literals
    # (`billing="sub"`/`billing="api"`) rather than ADR-0002's prose casing
    # (`Sub`/`API`) — refused rather than silently normalised, the same
    # choice already made for effort ('Max' is refused, not folded to 'max').
    assert only(m.validate(doc(entry(billing="Sub"))), "'billing'")
    assert only(m.validate(doc(entry(billing="API"))), "'billing'")


def test_billing_bogus_value_refused():
    assert only(m.validate(doc(entry(billing="prepaid"))), "'billing'")


def test_an_empty_billing_is_refused_rather_than_treated_as_absent():
    assert only(m.validate(doc(entry(billing=""))), "omitted entirely")


def test_a_null_billing_means_absent_like_an_omitted_key():
    assert m.validate(doc({"launcher": "claude", "model": "x",
                           "billing": None})) == []


def test_billing_does_not_participate_in_id_derivation():
    # The id identifies a model and an effort. Two entries agreeing on
    # launcher, model and effort but differing only in billing are still a
    # true duplicate — a manifest bug, not two things for billing to tell
    # apart — the same as two entries agreeing on everything material
    # already are.
    same_billing_missing = doc(entry(), entry(billing="sub"))
    assert any("both derive the id" in p for p in m.validate(same_billing_missing))

    different_billing = doc(entry(billing="sub"), entry(billing="api"))
    assert any("both derive the id" in p for p in m.validate(different_billing))
    assert m.entry_id(entry(billing="sub")) == m.entry_id(entry(billing="api"))


def test_billing_does_not_participate_in_label_derivation():
    assert m.display(entry(billing="sub")) == m.display(entry(billing="api")) \
        == m.display(entry())


# ----------------------------------------------------------------------
# the id is derived from model and effort
# ----------------------------------------------------------------------

def test_id_is_model_plus_effort():
    assert m.entry_id(entry()) == "claude-sonnet-5-medium"


def test_id_is_model_alone_when_there_is_no_effort():
    assert m.entry_id(entry(effort=None)) == "claude-sonnet-5"


def test_id_drops_the_provider_prefix():
    assert m.entry_id(entry(model="moonshotai/kimi-k3", effort="max")) == "kimi-k3-max"


def test_id_folds_uppercase_down():
    assert m.entry_id(entry(model="deepseek/DeepSeek-V4", effort="low")) == "deepseek-v4-low"


def test_the_1m_suffix_survives_into_the_id():
    assert m.entry_id(entry(model="moonshotai/kimi-k3[1m]", effort="max")) == "kimi-k3-1m-max"


def test_1m_and_plain_are_distinct_mappings_not_a_collision():
    # An entry with the suffix and one without are different mappings; if the
    # suffix were stripped they would collide and be reported as duplicates.
    suffixed = doc(entry(model="moonshotai/kimi-k3", effort="max"),
                   entry(model="moonshotai/kimi-k3[1m]", effort="max"))
    assert m.validate(suffixed) == []
    assert len(set(m.ids(suffixed))) == 2, repr(m.ids(suffixed))


def test_every_derived_id_matches_the_id_pattern():
    assert all(m.ID_RE.match(i) for i in m.ids(EXAMPLE)), repr(m.ids(EXAMPLE))


def test_a_model_yielding_no_usable_id_is_refused():
    assert only(m.validate(doc(entry(model="!!!"))), "usable id")


# ----------------------------------------------------------------------
# colliding ids take the launcher — all of them, not just the second
# ----------------------------------------------------------------------

def _clashing_doc():
    return doc(entry(launcher="claude-openrouter", model="moonshotai/kimi-k3",
                     effort="max"),
               entry(launcher="claude-alt", model="moonshotai/kimi-k3",
                     effort="max"))


def test_both_colliding_entries_carry_their_launcher():
    clash = _clashing_doc()
    assert m.ids(clash) == ["kimi-k3-max-claude-openrouter",
                            "kimi-k3-max-claude-alt"], repr(m.ids(clash))


def test_a_colliding_file_still_validates():
    assert m.validate(_clashing_doc()) == []


def test_reordering_a_colliding_file_does_not_rename_anything():
    # Order-independence is the point: if only the later entry were renamed,
    # reordering the file would rename an entry that anything may have
    # recorded.
    clash = _clashing_doc()
    reversed_clash = {"schema": 1, "mappings": list(reversed(clash["mappings"]))}
    assert sorted(m.ids(clash)) == sorted(m.ids(reversed_clash)), \
        repr(m.ids(reversed_clash))


def test_a_non_colliding_entry_keeps_the_short_id():
    assert m.ids(doc(entry(launcher="claude-openrouter",
                           model="moonshotai/kimi-k3", effort="max"))) \
        == ["kimi-k3-max"]


def test_a_true_duplicate_is_refused_and_names_the_entry_it_duplicates():
    dupe = doc(entry(), entry(notes="same launcher, model and effort"))
    problems = m.validate(dupe)
    assert any("both derive the id" in p for p in problems), repr(problems)
    assert any("mappings[0]" in p for p in problems)


# ----------------------------------------------------------------------
# the label is derived, so it cannot contradict the fields
# ----------------------------------------------------------------------

def test_label_is_model_and_effort():
    assert m.display(entry(model="moonshotai/kimi-k3", effort="max")) == "kimi-k3/max"


def test_label_is_model_alone_when_no_effort_is_sent():
    assert m.display(entry(model="deepseek/deepseek-v4.1-flash", effort=None)) \
        == "deepseek-v4.1-flash"


def test_first_party_label_reads_the_same_way():
    assert m.display(entry()) == "claude-sonnet-5/medium"


def test_the_1m_suffix_is_shown_in_the_label():
    # Shown since it is part of the model asked for.
    assert m.display(entry(model="moonshotai/kimi-k3[1m]", effort="max")) \
        == "kimi-k3[1m]/max"


def test_a_stored_display_field_is_ignored_not_honoured():
    assert m.display(entry(display="haiku/low")) == "claude-sonnet-5/medium"


@pytest.mark.parametrize("model_id", [
    "moonshotai/kimi-k3", "z-ai/glm-5.3-flash", "GLM-4.6",
    "deepseek/DeepSeek-V4", "mistral-large-2512",
])
def test_the_model_id_is_rendered_exactly_as_its_provider_writes_it(model_id):
    # The label has to stay greppable: this same string is the manifest's
    # `model`, `ccd ls`'s resolved-model column, and the transcript.
    # Restyling it would need no table but would invent a name.
    assert m.display({"model": model_id, "effort": "max"}).split("/max")[0] \
        == model_id.rsplit("/", 1)[-1]


# ----------------------------------------------------------------------
# colliding labels take the launcher, exactly as ids do
# ----------------------------------------------------------------------

def test_outside_a_collision_the_label_stays_short():
    # A label exists so a human can choose from it; two identical rows mean
    # the choice cannot be made from the label at all.
    alone = doc(entry(launcher="claude-openrouter", model="moonshotai/kimi-k3",
                      effort="max"),
                entry())
    assert m.labels(alone) == ["kimi-k3/max", "claude-sonnet-5/medium"], \
        repr(m.labels(alone))


def _both_colliding_and_plain():
    return doc(entry(launcher="claude-openrouter", model="moonshotai/kimi-k3",
                     effort="max"),
               entry(launcher="claude-alt", model="moonshotai/kimi-k3",
                     effort="max"),
               entry())


def test_in_a_collision_every_member_carries_its_launcher():
    both = _both_colliding_and_plain()
    assert m.labels(both) == ["kimi-k3/max (claude-openrouter)",
                              "kimi-k3/max (claude-alt)",
                              "claude-sonnet-5/medium"], repr(m.labels(both))


def test_the_uninvolved_entry_is_untouched():
    both = _both_colliding_and_plain()
    assert m.labels(both)[2] == "claude-sonnet-5/medium"


def test_no_two_labels_in_a_file_are_equal():
    both = _both_colliding_and_plain()
    assert len(set(m.labels(both))) == len(m.labels(both))


def test_reordering_the_file_does_not_relabel_anything():
    # Same property the ids have, and for the same reason: anything that
    # quoted a label must not be invalidated by a later reordering.
    both = _both_colliding_and_plain()
    flipped = {"schema": 1, "mappings": list(reversed(both["mappings"]))}
    assert sorted(m.labels(both)) == sorted(m.labels(flipped)), repr(m.labels(flipped))


def test_labels_and_ids_collide_on_the_same_entries():
    both = _both_colliding_and_plain()
    assert [l.endswith(")") for l in m.labels(both)] \
        == [i.endswith(("-claude-openrouter", "-claude-alt")) for i in m.ids(both)], \
        f"{m.labels(both)!r} vs {m.ids(both)!r}"


# ----------------------------------------------------------------------
# launcher is a bare name, resolved on $PATH at launch
# ----------------------------------------------------------------------

def test_a_path_launcher_is_refused():
    assert only(m.validate(doc(entry(launcher="/usr/bin/claude"))), "bare name")


def test_a_relative_path_launcher_is_refused():
    assert only(m.validate(doc(entry(launcher="./claude"))), "bare name")


def test_a_leading_dash_launcher_is_refused():
    assert only(m.validate(doc(entry(launcher="--model"))), "option")


def test_a_bare_name_launcher_is_accepted():
    assert m.validate(doc(entry(launcher="claude-x"))) == []


def test_validate_does_not_require_the_launcher_to_exist():
    # Shape is host-independent; availability is not. A manifest naming a
    # launcher this machine lacks is still a valid manifest.
    assert m.validate(doc(entry(launcher="definitely-not-installed"))) == []


def test_resolve_launcher_rejects_a_missing_launcher():
    with pytest.raises(m.ManifestError) as excinfo:
        m.resolve_launcher(entry(launcher="definitely-not-installed"))
    assert "$PATH" in str(excinfo.value)
    assert "claude-sonnet-5-medium" in str(excinfo.value), str(excinfo.value)


def test_resolve_launcher_finds_a_real_one():
    assert m.resolve_launcher(entry(launcher="sh")).endswith("sh")


# ----------------------------------------------------------------------
# required fields, and unknown ones
# ----------------------------------------------------------------------

@pytest.mark.parametrize("field", m.REQUIRED)
def test_missing_required_field_refused(field):
    missing = {k: v for k, v in entry().items() if k != field}
    problems = m.validate(doc(missing))
    assert any(f"'{field}'" in p for p in problems), repr(problems)


@pytest.mark.parametrize("field", m.REQUIRED)
def test_blank_required_field_refused(field):
    problems = m.validate(doc(entry(**{field: "  "})))
    assert any(f"'{field}'" in p for p in problems), repr(problems)


def test_notes_is_optional():
    assert m.validate(doc(entry())) == []


def test_notes_must_be_a_string_when_present():
    assert only(m.validate(doc(entry(notes=7))), "'notes'")


def test_an_unknown_entry_key_is_ignored_not_refused():
    # The file is machine-rendered: a producing layer that learns a new field
    # must not break every older reader. Retired keys are the exception.
    assert m.validate(doc(entry(colour="blue"))) == []


def test_an_unknown_top_level_key_is_ignored_not_refused():
    assert m.validate({"schema": 1, "mappings": [entry()], "generated_by": "x"}) == []


# ----------------------------------------------------------------------
# the envelope
# ----------------------------------------------------------------------

def test_a_bare_array_is_refused_no_schema_to_version_it():
    assert only(m.validate([entry()]), "top level")


def test_missing_schema_refused():
    problems = m.validate({"mappings": [entry()]})
    assert any("'schema'" in p for p in problems)


def test_a_future_schema_refused_with_an_upgrade_hint():
    problems = m.validate({"schema": m.SCHEMA + 1, "mappings": [entry()]})
    assert any("upgrade ccd" in p for p in problems)


def test_mappings_must_be_an_array():
    assert only(m.validate({"schema": 1, "mappings": {}}), "'mappings'")


def test_an_empty_mappings_is_refused():
    assert only(m.validate({"schema": 1, "mappings": []}), "nothing to pick")


# ----------------------------------------------------------------------
# every problem is reported, not just the first
# ----------------------------------------------------------------------

def test_three_faults_in_one_entry_give_three_problems():
    bad = m.validate(doc(entry(slot="opus", effort="nope", notes=7)))
    assert len(bad) == 3, repr(bad)


# ----------------------------------------------------------------------
# where the manifest lives
# ----------------------------------------------------------------------

def test_ccd_mappings_env_wins(tmp_path):
    env = {"CCD_MAPPINGS": str(tmp_path / "explicit.json")}
    assert m.manifest_path(env) == tmp_path / "explicit.json"


def test_else_xdg_config_home(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path / "cfg")}
    assert m.manifest_path(env) == tmp_path / "cfg" / "ccd" / "mappings.json"


def test_else_dot_config_ccd_mappings():
    assert m.manifest_path({}) == Path("~/.config/ccd/mappings.json").expanduser()


def test_an_empty_ccd_mappings_does_not_win(tmp_path):
    env = {"CCD_MAPPINGS": "", "XDG_CONFIG_HOME": str(tmp_path)}
    assert m.manifest_path(env) == tmp_path / "ccd" / "mappings.json"


# ----------------------------------------------------------------------
# loading
# ----------------------------------------------------------------------

@pytest.fixture
def loaded_example(tmp_path):
    good = write(tmp_path, "good.json", EXAMPLE)
    return m.load(good)


def test_a_good_manifest_loads(loaded_example):
    assert len(m.entries(loaded_example)) == 5


def test_file_order_is_presentation_order(loaded_example):
    assert [e["model"] for e in m.entries(loaded_example)] \
        == [e["model"] for e in EXAMPLE["mappings"]]


def test_find_resolves_a_derived_id(loaded_example):
    assert m.find(loaded_example, "kimi-k3-1m-max")["model"] == "moonshotai/kimi-k3[1m]"


def test_find_resolves_an_id_with_no_effort_in_it(loaded_example):
    assert m.find(loaded_example, "claude-haiku-4-5")["model"] == "claude-haiku-4-5"


def test_find_returns_none_for_a_stranger(loaded_example):
    assert m.find(loaded_example, "nope") is None


def test_a_missing_manifest_raises_file_not_found_error(tmp_path):
    # "You have not set this up" and "what you set up is wrong" are
    # different messages to a human, so they are different exceptions.
    with pytest.raises(FileNotFoundError) as excinfo:
        m.load(tmp_path / "absent.json")
    assert "absent.json" in str(excinfo.value)


def test_malformed_json_raises_manifest_error(tmp_path):
    broken = write(tmp_path, "broken.json", "{not json")
    with pytest.raises(m.ManifestError) as excinfo:
        m.load(broken)
    assert "not valid JSON" in str(excinfo.value)


def test_an_invalid_manifest_raises_manifest_error_with_every_problem(tmp_path):
    invalid = write(tmp_path, "invalid.json", doc(entry(effort="nope", notes=7)))
    with pytest.raises(m.ManifestError) as excinfo:
        m.load(invalid)
    exc = excinfo.value
    assert len(exc.problems) == 2, repr(exc.problems)
    assert str(invalid) in str(exc)
    assert str(exc).count("\n  - ") == 2, str(exc)
