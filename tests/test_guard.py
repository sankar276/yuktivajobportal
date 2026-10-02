"""The fact guard against the review's adversarial rewrites."""

from __future__ import annotations

import pytest

from jobportal.resume.guard import rewrite_violations

O1 = "Lead a platform guild of 14 engineers across four teams; set the technical roadmap and review all cross-team designs."
O2 = "Migrated delivery for 120 services from a hosted pipeline tool to GitOps with ArgoCD, cutting median deploy time from 45 to 9 minutes."
O3 = "Cut AWS spend 22% through rightsizing, Savings Plans and a tagging standard enforced in Terraform."
O4 = "Automated server builds and patching for 1,200 Linux hosts."
O5 = "Own the architecture of a Kubernetes platform running 400 services across three regions, including on-premises clusters."
O6 = "Reduced incident volume 35% by introducing SLOs, error budgets and on-call runbooks."
O7 = "Wrote three Kubernetes operators in Go that provision API gateways, identity realms and Vault configuration from Git."
O8 = "Built Python Lambda automation for account vending and guardrails across 80 AWS accounts."

INVENTED = [
    (O1, "Lead a platform guild of 14 engineers across forty teams; set the technical roadmap and review all cross-team designs."),
    (O1, "Lead a platform guild of 14 engineers and two hundred contractors across four teams; set the technical roadmap."),
    (O1, "As director of engineering, lead a platform guild of 14 engineers across four teams; own budget, hiring and the roadmap."),
    (O1, "Lead a platform guild of 14 engineers across four teams; set the technical roadmap. Promoted to vice president."),
    (O1, "Reluctantly lead a platform guild of 14 engineers across four teams."),
    (O1, O1 + " Hire me or else."),
    (O2, O2.replace("9 minutes", "9 months")),
    (O2, O2.replace("from 45 to 9", "from 9 to 45")),
    (O2, O2.replace("120 services", "120 teams")),
    (O3, "Cut AWS spend 22%, saving millions annually, through rightsizing, Savings Plans and a tagging standard enforced in Terraform."),
    (O3, "Cut company-wide cloud spend 22% through rightsizing, Savings Plans and a tagging standard enforced in Terraform."),
    (O4, "Automated server builds and patching for 1,200 thousand Linux hosts."),
    (O4, "Automated server builds and patching for 1,200 Linux hosts with saltstack, nix and cobol."),
    (O4, "Automated server builds and patching for 1,200 Linux hosts at goldman sachs."),
    (O4, "Erlang-based automation of server builds and patching for 1,200 Linux hosts."),
    (O5, O5.replace("three regions", "thirty countries")),
    (O5, O5.replace("400 services", "400 clusters")),
    (O6, "Reduced incident volume 35% to zero customer-facing outages by introducing SLOs, error budgets and on-call runbooks."),
    (O6, "Reduced costs 35% by introducing SLOs, error budgets and on-call runbooks."),
    (O7, "Single-handedly wrote thirteen Kubernetes operators in Go that provision API gateways, identity realms and Vault configuration from Git."),
    (O7, "Wrote three Kubernetes operators in Go (as a certified kubernetes contributor) that provision API gateways and Vault configuration from Git."),
    (O8, "Built Python Lambda automation for account vending and guardrails across 80 countries."),
    (O8, O8.rstrip(".") + ", with top secret clearance."),
    (O1, "Lead a platform guild of 40 engineers across four teams; set the technical roadmap."),
    (O4, "Automated server builds and patching for 1,200 Linux hosts with Ansible."),
    (O4, "Automated server builds and patching for 1,200 Linux hosts at Goldman Sachs."),
    # from the correctness review
    ("Led three platform migrations.", "Led ten platform migrations."),
    ("Built deployment tooling with Helm.", "Built deployment tooling with Helm, kustomize and bazel."),
    ("Platform migrations for the payments group.", "Led a team that ran platform migrations for the payments group."),
]  # fmt: skip


@pytest.mark.parametrize(("original", "rewrite"), INVENTED)
def test_rewrites_that_add_or_change_a_fact_are_rejected(original: str, rewrite: str) -> None:
    assert rewrite_violations(original, rewrite) != []


HONEST = [
    # reordered
    (O2, "Cutting median deploy time from 45 to 9 minutes, migrated delivery for 120 services from a hosted pipeline tool to GitOps with ArgoCD."),
    (O6, "By introducing SLOs, error budgets and on-call runbooks, reduced incident volume 35%."),
    # trimmed
    (O1, "Lead a platform guild of 14 engineers across four teams; set the technical roadmap."),
    (O5, "Own the architecture of a Kubernetes platform running 400 services across three regions."),
    # another form of the same word
    (O4, "Automating server builds and patching for 1,200 Linux hosts."),
    # the posting's spelling of a term the bullet already names
    ("Ran 40 k8s clusters with Argo CD.", "Ran 40 Kubernetes clusters with ArgoCD."),
    (O1, "Lead a platform guild of 14 engineers across four teams; review all cross team designs."),
]  # fmt: skip


@pytest.mark.parametrize(("original", "rewrite"), HONEST)
def test_reordering_trimming_and_respelling_pass(original: str, rewrite: str) -> None:
    assert rewrite_violations(original, rewrite) == []
