"""What `stackwrench check` finds in a repository, and what it ignores."""
import pytest

from stackwrench.check import DiscoveryError, discover


def kinds(discovery):
    return sorted((source.kind, source.display) for source in discovery.sources)


def test_demo_discovery(demo):
    discovery = discover(".")
    assert kinds(discovery) == [
        ("compose", "compose.yaml"), ("compose", "infra/compose.override.yaml"), ("compose", "infra/compose.yaml"),
        ("devcontainer", ".devcontainer/devcontainer.json"), ("dockerfile", "Dockerfile"), ("dotenv", ".env"),
        ("justfile", "justfile"), ("mise", "mise.toml"), ("taskfile", "Taskfile.yml"), ("version-file", ".nvmrc"),
    ]
    root, infra = discovery.compose_groups
    assert [p.name for p in root.default] == ["compose.yaml"] and root.env_file is not None
    assert [p.name for p in infra.default] == ["compose.yaml", "compose.override.yaml"] and infra.env_file is None
    [build] = discovery.build_contexts
    assert build.context == discovery.root and build.dockerfile.name == "Dockerfile"
    assert build.origin == "compose.yaml (service web)"


def test_compose_variants_and_precedence(repo):
    repo.write("compose.yaml", "services: {}\n")
    repo.write("docker-compose.yml", "services: {}\n")
    repo.write("compose.override.yaml", "services: {}\n")
    repo.write("compose.prod.yaml", "services: {}\n")
    [group] = discover(".").compose_groups
    assert [p.name for p in group.default] == ["compose.yaml", "compose.override.yaml"]
    assert [p.name for p in group.variants] == ["compose.prod.yaml"]
    assert [p.name for p in group.standalone] == ["compose.yaml", "compose.prod.yaml"]


def test_skipped_directories(repo):
    repo.write("node_modules/pkg/Dockerfile", "FROM scratch\n")
    repo.write(".git/Dockerfile", "FROM scratch\n")
    repo.write(".cache/compose.yaml", "services: {}\n")
    repo.write(".github/actions/x/Dockerfile", "FROM scratch\n")
    repo.write("services/api/Dockerfile", "FROM scratch\n")
    discovery = discover(".")
    assert kinds(discovery) == [("dockerfile", "services/api/Dockerfile")]
    assert [b.context.name for b in discovery.build_contexts] == ["api"]


def test_content_sniffed_kinds(repo):
    repo.write("kind.yaml", "kind: Cluster\napiVersion: kind.x-k8s.io/v1alpha4\n")
    repo.write("deploy/app.yaml", "apiVersion: v1\nkind: Pod\nmetadata:\n  name: a\n")
    repo.write("nodes.json", '{"schemaVersion": 1, "cluster": "dev", "nodes": {}}')
    repo.write("package.json", '{"schemaVersion": 1, "cluster": "x", "nodes": {}}')
    assert kinds(discover(".")) == [("k8s-manifest", "deploy/app.yaml"), ("kind-config", "kind.yaml"),
                                    ("kind-inventory", "nodes.json")]


def test_manifests_ignored_without_kind(repo):
    repo.write("deploy/app.yaml", "apiVersion: v1\nkind: Pod\nmetadata:\n  name: a\n")
    assert kinds(discover(".")) == []


def test_workflows_only_with_mise(repo):
    repo.write(".github/workflows/ci.yml", "steps:\n  - uses: actions/setup-node@v4\n")
    assert kinds(discover(".")) == []
    repo.write("mise.toml", "[tools]\nnode = \"22.14.0\"\n")
    assert ("workflow", ".github/workflows/ci.yml") in kinds(discover("."))


def test_interpolated_build_context_is_not_guessed(repo):
    repo.write("compose.yaml", "services:\n  app:\n    build: ${APP_DIR}\n")
    repo.write("app/Dockerfile", "FROM scratch\n")
    discovery = discover(".")
    # app/Dockerfile still gets its own folder as context; the ${APP_DIR} entry is skipped.
    assert [b.origin for b in discovery.build_contexts] == ["app/Dockerfile"]


def test_not_a_directory(repo):
    repo.write("file.txt", "x")
    with pytest.raises(DiscoveryError):
        discover("file.txt")
    with pytest.raises(DiscoveryError):
        discover("missing")
