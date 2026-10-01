"""A broad list of technology and practice terms.

Used for one thing: spotting what a posting asks for that your evidence bank
does not contain, so the gap can be *shown to you*. Terms from this list are
never added to a resume.
"""

from __future__ import annotations

COMMON_TERMS: tuple[str, ...] = (
    # languages
    "Python", "Java", "Go", "Rust", "C++", "C#", "JavaScript", "TypeScript", "Scala", "Kotlin",
    "Ruby", "PHP", "Swift", "SQL", "Bash", "PowerShell", "Node.js", ".NET",
    # cloud
    "AWS", "Azure", "GCP", "EKS", "AKS", "GKE", "Lambda", "EC2", "S3", "CloudFormation", "CDK",
    "Bicep", "OpenStack", "VMware", "Bare metal",
    # containers and orchestration
    "Kubernetes", "Docker", "Helm", "Istio", "Linkerd", "Envoy", "Cilium", "Calico", "OpenShift",
    "Rancher", "RKE2", "Nomad", "Service mesh", "Karpenter",
    # infrastructure as code and configuration
    "Terraform", "Pulumi", "Ansible", "Chef", "Puppet", "Crossplane", "Packer",
    # delivery
    "CI/CD", "Jenkins", "GitHub Actions", "GitLab", "CircleCI", "ArgoCD", "Flux", "Spinnaker",
    "Harness", "Tekton", "GitOps", "Artifactory", "Backstage",
    # observability
    "Prometheus", "Grafana", "Datadog", "Splunk", "New Relic", "OpenTelemetry", "Elasticsearch",
    "Loki", "Jaeger", "Dynatrace", "PagerDuty",
    # data and streaming
    "Kafka", "Flink", "Spark", "Airflow", "Snowflake", "Databricks", "dbt", "PostgreSQL", "MySQL",
    "MongoDB", "Redis", "Cassandra", "DynamoDB", "BigQuery", "Redshift", "Hadoop", "Tableau",
    "MicroStrategy", "Power BI", "Oracle", "SQL Server", "RabbitMQ", "Pulsar",
    # security and identity
    "Vault", "OPA", "Kyverno", "Gatekeeper", "SPIFFE", "SPIRE", "OIDC", "SAML", "OAuth", "IAM",
    "PKI", "mTLS", "SIEM", "Wiz", "Prisma Cloud", "CrowdStrike", "Okta", "ForgeRock", "Sigstore",
    "SLSA", "SBOM", "Zero trust", "SOC 2", "PCI", "HIPAA", "FedRAMP", "NIST", "CIS",
    "Policy as code", "DevSecOps", "Threat modeling", "Active Directory", "Entra ID",
    # AI / ML
    "LLM", "RAG", "PyTorch", "TensorFlow", "MLOps", "Kubeflow", "SageMaker", "Vertex AI",
    "Bedrock", "NVIDIA", "GPU", "CUDA", "Triton", "vLLM", "LangChain", "Generative AI",
    "Machine learning",
    # networking
    "BGP", "DNS", "TCP/IP", "CDN", "Load balancing", "VPN", "SD-WAN", "SASE", "Palo Alto",
    "Check Point", "Fortinet", "F5", "eBPF", "API gateway", "Apigee", "Kong", "NGINX",
    # practices
    "SRE", "FinOps", "Agile", "Scrum", "ITIL", "Microservices", "Event-driven",
    "Platform engineering", "Disaster recovery", "Incident management", "Capacity planning",
    "Chaos engineering", "Multi-region", "High availability", "Linux", "Windows Server",
)  # fmt: skip
