from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from jobportal.config import (
    ConfigError,
    Seniority,
    UserConfig,
    load_profile,
    load_search,
    load_user_config,
)
from jobportal.resume.model import load_resume_bank
from tests.conftest import CONFIG


def _edit(path: Path, change) -> None:
    data = yaml.safe_load(path.read_text())
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def test_example_config_loads(user_config: UserConfig) -> None:
    profile, search, resume = user_config.profile, user_config.search, user_config.resume
    assert profile.first_name == "Alex" and profile.last_name == "Example"
    assert profile.work_authorization.authorized is True
    assert profile.eeo.gender == "decline"
    assert [lane.key for lane in search.lanes] == ["career", "contract"]
    assert search.lanes[0].seniority.min is Seniority.staff
    assert search.policy.mode == "review"  # nothing is sent unattended out of the box
    assert set(resume.variants) == {"default", "security", "contract"}
    assert all(bullet.id for role in resume.experience for bullet in role.bullets)
    assert "Kubernetes" in resume.vocabulary() and "leadership" in resume.vocabulary()


def test_signature_defaults_to_name_phone_linkedin(user_config: UserConfig) -> None:
    assert user_config.profile.email_signature().splitlines() == [
        "Alex Example",
        "+1 512 555 0142",
        "https://www.linkedin.com/in/alex-example",
    ]


def test_missing_file_says_how_to_fix_it(settings) -> None:
    with pytest.raises(ConfigError, match="jobportal init"):
        load_profile(settings.data_dir)


def test_unknown_keys_are_rejected_with_their_location(data_dir: Path) -> None:
    _edit(data_dir / "search.yaml", lambda d: d["policy"]["auto"].update(min_scroe=70))
    with pytest.raises(ConfigError) as caught:
        load_search(data_dir)
    assert "policy.auto.min_scroe" in str(caught.value)


def test_invalid_values_are_reported(data_dir: Path) -> None:
    _edit(data_dir / "profile.yaml", lambda d: d.update(email="not-an-email"))
    with pytest.raises(ConfigError, match="email"):
        load_profile(data_dir)
    _edit(data_dir / "search.yaml", lambda d: d["lanes"][0]["seniority"].update(min="wizard"))
    with pytest.raises(ConfigError, match="unknown seniority"):
        load_search(data_dir)


def test_broken_yaml_is_a_config_error(data_dir: Path) -> None:
    (data_dir / "search.yaml").write_text("lanes: [unclosed")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_search(data_dir)


def test_duplicate_lane_keys_are_rejected(data_dir: Path) -> None:
    _edit(data_dir / "search.yaml", lambda d: d["lanes"][1].update(key="career"))
    with pytest.raises(ConfigError, match="duplicate lane keys"):
        load_search(data_dir)


def test_lane_must_reference_an_existing_resume_variant(data_dir: Path) -> None:
    _edit(data_dir / "search.yaml", lambda d: d["lanes"][0].update(resume="executive"))
    with pytest.raises(ConfigError, match="executive"):
        load_user_config(data_dir)


def test_resume_bullets_accept_plain_strings_and_reject_unknown_variants(data_dir: Path) -> None:
    path = data_dir / "resume.yaml"
    _edit(path, lambda d: d["experience"][0]["bullets"].append("Mentored six engineers."))
    bank = load_resume_bank(path)
    assert bank.experience[0].bullets[-1].text == "Mentored six engineers."
    _edit(path, lambda d: d["experience"][0]["bullets"][0].update(variants=["nope"]))
    with pytest.raises(ConfigError, match="unknown variants"):
        load_resume_bank(path)


def test_work_authorization_defaults_to_unanswered() -> None:
    from jobportal.config import Profile

    profile = Profile(name="Sam Doe", email="sam@example.com")
    assert profile.work_authorization.authorized is None
    assert profile.work_authorization.needs_sponsorship is None


def test_a_key_written_twice_is_an_error_not_a_silent_replacement(data_dir: Path) -> None:
    path = data_dir / "search.yaml"
    path.write_text(path.read_text() + "\nblocked_companies:\n  - Initech\n")
    with pytest.raises(ConfigError, match="'blocked_companies' appears twice"):
        load_search(data_dir)
    # The same key in two different lanes is fine.
    shutil.copy(CONFIG / "search.example.yaml", path)
    assert len(load_search(data_dir).lanes) == 2


def test_currency_is_a_three_letter_code_in_any_case(data_dir: Path) -> None:
    path = data_dir / "search.yaml"
    _edit(path, lambda d: d["lanes"][0]["compensation"].update(currency="usd"))
    assert load_search(data_dir).lanes[0].compensation.currency == "USD"
    _edit(path, lambda d: d["lanes"][0]["compensation"].update(currency="dollars"))
    with pytest.raises(ConfigError, match="three-letter code"):
        load_search(data_dir)


@pytest.mark.parametrize("written", ["none", "None", "N/A", "no", None, False, " "])
def test_saying_you_hold_no_clearance_means_none(data_dir: Path, written: object) -> None:
    _edit(data_dir / "profile.yaml", lambda d: d.update(security_clearance=written))
    assert load_profile(data_dir).security_clearance == ""


def test_a_named_clearance_is_kept_as_written(data_dir: Path) -> None:
    _edit(data_dir / "profile.yaml", lambda d: d.update(security_clearance="Top Secret"))
    assert load_profile(data_dir).security_clearance == "Top Secret"
