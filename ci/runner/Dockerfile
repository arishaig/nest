# Custom GitHub Actions runner image for the ARC "arc-lint" scale set.
#
# Bakes the exact tooling the stateless lint/validate jobs in
# .github/workflows/lint.yml need, at the SAME paths the workflows already use
# (notably the Ansible venv at /opt/ansible-runner/venv) so the workflow steps
# run unchanged whether on the LXC self-hosted runner or here.
#
# Versions mirror playbooks/provision/runner.yml and are Renovate-managed via
# the `# renovate:` ARG annotations below. Built/pushed by the build-ci-runner
# job in .github/workflows/deploy.yml.
FROM ghcr.io/actions/actions-runner:latest

# renovate: datasource=github-releases depName=opentofu/opentofu
ARG OPENTOFU_VERSION=1.12.0
# renovate: datasource=github-releases depName=prometheus/prometheus
ARG PROMETHEUS_VERSION=3.12.0
# renovate: datasource=github-releases depName=prometheus/alertmanager
ARG ALERTMANAGER_VERSION=0.32.2
# renovate: datasource=github-releases depName=kubernetes-sigs/kustomize extractVersion=^kustomize/v(?<version>.+)$
ARG KUSTOMIZE_VERSION=5.8.1
# renovate: datasource=github-releases depName=yannh/kubeconform
ARG KUBECONFORM_VERSION=0.7.0
# renovate: datasource=github-releases depName=kubernetes/kubernetes
ARG KUBECTL_VERSION=1.32.0
# shellcheck pinned to 0.10.0: the SC2015 exclude in the shellcheck lint job is
# tuned to it (0.11.0 dropped that warning). renovate: datasource=github-releases depName=koalaman/shellcheck
ARG SHELLCHECK_VERSION=0.10.0

USER root

# Base packages + Docker CLI/Compose plugin (the config-validate job runs
# `docker compose config`, which parses locally and needs no daemon).
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl jq unzip xz-utils graphviz gnupg sshpass \
        python3 python3-pip python3-venv ca-certificates \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
        https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# CI binaries — same download/extract pattern as runner.yml, installed to
# /usr/local/bin.
RUN set -eux; \
    tmp="$(mktemp -d)"; cd "$tmp"; \
    # OpenTofu
    curl -fsSL -o tofu.zip "https://github.com/opentofu/opentofu/releases/download/v${OPENTOFU_VERSION}/tofu_${OPENTOFU_VERSION}_linux_amd64.zip"; \
    unzip -q tofu.zip tofu; install -m 0755 tofu /usr/local/bin/tofu; \
    # promtool
    curl -fsSL -o prometheus.tar.gz "https://github.com/prometheus/prometheus/releases/download/v${PROMETHEUS_VERSION}/prometheus-${PROMETHEUS_VERSION}.linux-amd64.tar.gz"; \
    tar -xzf prometheus.tar.gz; install -m 0755 "prometheus-${PROMETHEUS_VERSION}.linux-amd64/promtool" /usr/local/bin/promtool; \
    # amtool
    curl -fsSL -o alertmanager.tar.gz "https://github.com/prometheus/alertmanager/releases/download/v${ALERTMANAGER_VERSION}/alertmanager-${ALERTMANAGER_VERSION}.linux-amd64.tar.gz"; \
    tar -xzf alertmanager.tar.gz; install -m 0755 "alertmanager-${ALERTMANAGER_VERSION}.linux-amd64/amtool" /usr/local/bin/amtool; \
    # kustomize
    curl -fsSL -o kustomize.tar.gz "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv${KUSTOMIZE_VERSION}/kustomize_v${KUSTOMIZE_VERSION}_linux_amd64.tar.gz"; \
    tar -xzf kustomize.tar.gz; install -m 0755 kustomize /usr/local/bin/kustomize; \
    # kubeconform
    curl -fsSL -o kubeconform.tar.gz "https://github.com/yannh/kubeconform/releases/download/v${KUBECONFORM_VERSION}/kubeconform-linux-amd64.tar.gz"; \
    tar -xzf kubeconform.tar.gz kubeconform; install -m 0755 kubeconform /usr/local/bin/kubeconform; \
    # kubectl
    curl -fsSL -o kubectl "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl"; \
    install -m 0755 kubectl /usr/local/bin/kubectl; \
    # shellcheck (pinned binary release, not apt)
    curl -fsSL -o shellcheck.tar.xz "https://github.com/koalaman/shellcheck/releases/download/v${SHELLCHECK_VERSION}/shellcheck-v${SHELLCHECK_VERSION}.linux.x86_64.tar.xz"; \
    tar -xJf shellcheck.tar.xz; install -m 0755 "shellcheck-v${SHELLCHECK_VERSION}/shellcheck" /usr/local/bin/shellcheck; \
    cd /; rm -rf "$tmp"

# Ansible venv at the path the workflows `source`. Versions float to match
# runner.yml (which does not pin them either).
RUN python3 -m venv /opt/ansible-runner/venv \
    && /opt/ansible-runner/venv/bin/pip install --no-cache-dir \
        ansible ansible-lint yamllint diagrams pyyaml kubernetes \
    && /opt/ansible-runner/venv/bin/ansible-galaxy collection install \
        ansible.posix community.general kubernetes.core \
    && chown -R runner:runner /opt/ansible-runner

USER runner
