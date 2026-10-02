from __future__ import annotations

from jobportal.robots import RobotsRules

TOKEN = "YuktivaJobPortal"


def test_no_rules_allows_everything() -> None:
    assert RobotsRules.parse("", TOKEN).allowed("/anything")


def test_longest_match_wins_regardless_of_order() -> None:
    # Python's urllib.robotparser gets this wrong (first match wins).
    rules = RobotsRules.parse("User-agent: *\nDisallow: /\nAllow: /jobs/\n", TOKEN)
    assert rules.allowed("/jobs/123")
    assert not rules.allowed("/admin")


def test_allow_wins_a_tie() -> None:
    rules = RobotsRules.parse("User-agent: *\nDisallow: /jobs\nAllow: /jobs\n", TOKEN)
    assert rules.allowed("/jobs")


def test_real_workday_robots() -> None:
    text = (
        "Sitemap: https://x.wd5.myworkdayjobs.com/Site/siteMap.xml\n\n"
        "User-agent: *\nAllow: /Site/\nDisallow: /talentcommunity/\nDisallow: /refreshFacet/\n"
    )
    rules = RobotsRules.parse(text, TOKEN)
    assert rules.allowed("/wday/cxs/x/Site/jobs")
    assert rules.allowed("/Site/job/US/Engineer_JR1")
    assert not rules.allowed("/talentcommunity/apply")


def test_specific_group_replaces_wildcard_group() -> None:
    text = "User-agent: *\nDisallow: /\n\nUser-agent: yuktivajobportal\nDisallow: /private\n"
    rules = RobotsRules.parse(text, TOKEN)
    assert rules.allowed("/jobs")
    assert not rules.allowed("/private/x")


def test_named_group_with_no_rules_means_allowed() -> None:
    text = "User-agent: *\nDisallow: /\n\nUser-agent: YuktivaJobPortal\nDisallow:\n"
    assert RobotsRules.parse(text, TOKEN).allowed("/jobs")


def test_groups_for_other_bots_do_not_apply() -> None:
    text = "User-agent: GPTBot\nDisallow: /\n"
    assert RobotsRules.parse(text, TOKEN).allowed("/jobs")


def test_several_agents_share_one_group() -> None:
    text = "User-agent: a\nUser-agent: YuktivaJobPortal\nDisallow: /x\n"
    assert not RobotsRules.parse(text, TOKEN).allowed("/x/1")


def test_wildcards_and_end_anchor() -> None:
    rules = RobotsRules.parse("User-agent: *\nDisallow: /*.pdf$\nDisallow: /search?*q=\n", TOKEN)
    assert not rules.allowed("/files/a.pdf")
    assert rules.allowed("/files/a.pdf.html")
    assert not rules.allowed("/search?page=2&q=x")
    assert rules.allowed("/search")


def test_comments_and_unknown_lines_are_ignored() -> None:
    text = "# hello\nUser-agent: * # everyone\nCrawl-delay: 5\nDisallow: /a # no\n"
    rules = RobotsRules.parse(text, TOKEN)
    assert not rules.allowed("/a")
    assert rules.allowed("/b")


def test_percent_encoding_is_normalised() -> None:
    rules = RobotsRules.parse("User-agent: *\nDisallow: /a-b\n", TOKEN)
    assert not rules.allowed("/a%2Db")


def test_unreachable_means_disallow_and_robots_txt_itself_is_always_allowed() -> None:
    assert not RobotsRules.disallow_all().allowed("/jobs")
    rules = RobotsRules.parse("User-agent: *\nDisallow: /\n", TOKEN)
    assert rules.allowed("/robots.txt")


def test_a_byte_order_mark_does_not_hide_the_first_group() -> None:
    rules = RobotsRules.parse("﻿User-agent: *\nDisallow: /\n", TOKEN)
    assert not rules.allowed("/jobs")


def test_wildcard_matching_cannot_be_made_slow() -> None:
    import time

    pattern = "/" + "*a" * 40 + "*b"
    rules = RobotsRules.parse(f"User-agent: *\nDisallow: {pattern}\n", TOKEN)
    started = time.perf_counter()
    assert rules.allowed("/" + "a" * 2000)  # no "b": the old matcher took minutes on this
    assert not rules.allowed("/" + "a" * 2000 + "b")
    assert time.perf_counter() - started < 1.0


def test_an_escaped_slash_is_not_a_path_separator() -> None:
    rules = RobotsRules.parse("User-agent: *\nDisallow: /a/\nAllow: /a%2Fb\n", TOKEN)
    assert not rules.allowed("/a/b/private")  # "Allow: /a%2Fb" does not open up /a/b
    assert rules.allowed("/a%2Fb")
    assert rules.allowed("/a%2fb")  # hex case does not matter


def test_a_group_naming_us_with_a_version_applies() -> None:
    text = "User-agent: yuktivajobportal/0.1\nDisallow: /private\n\nUser-agent: *\nDisallow: /\n"
    rules = RobotsRules.parse(text, "YuktivaJobPortal")
    assert rules.allowed("/jobs") and not rules.allowed("/private/x")
    # ... and a similarly named other crawler's group does not.
    other = RobotsRules.parse("User-agent: yuktivajobportalbot\nDisallow: /\n", "YuktivaJobPortal")
    assert other.allowed("/jobs")


def test_the_number_of_rules_kept_is_bounded() -> None:
    from jobportal.robots import MAX_RULES

    text = "User-agent: *\nDisallow: /first\n" + "".join(
        f"Disallow: /p{index}\n" for index in range(MAX_RULES + 5000)
    )
    rules = RobotsRules.parse(text, TOKEN)
    assert len(rules.rules) == MAX_RULES and not rules.allowed("/first")


def test_end_anchors_and_wildcards_together() -> None:
    rules = RobotsRules.parse("User-agent: *\nDisallow: /*/print$\nDisallow: /x*y*z\n", TOKEN)
    assert not rules.allowed("/jobs/print")
    assert rules.allowed("/jobs/print/more")
    assert not rules.allowed("/x1y2z3") and rules.allowed("/x1z2y")
