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
