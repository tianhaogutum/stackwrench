"""Extract supported pod specs at their actual Kubernetes API paths."""

from pathlib import Path

from .inputs import InputError, mapping, read_documents, string
from .references import parse_reference


_PATHS = {
    ("v1", "Pod"): ("spec",),
    ("apps/v1", "Deployment"): ("spec", "template", "spec"),
    ("apps/v1", "StatefulSet"): ("spec", "template", "spec"),
    ("apps/v1", "DaemonSet"): ("spec", "template", "spec"),
    ("batch/v1", "Job"): ("spec", "template", "spec"),
    ("batch/v1", "CronJob"): ("spec", "jobTemplate", "spec", "template", "spec"),
}


def read_manifests(paths: list[Path]) -> tuple[list[dict], list[dict]]:
    containers = []
    unassessed = []
    object_count = 0
    for path in paths:
        for doc_index, doc in enumerate(read_documents(path), 1):
            if doc is None:
                continue
            object_count += 1
            where = f"{path}: document {doc_index}"
            doc = mapping(doc, where)
            api = string(doc.get("apiVersion"), f"{where}.apiVersion")
            kind = string(doc.get("kind"), f"{where}.kind")
            pod_path = _PATHS.get((api, kind))
            metadata = mapping(doc.get("metadata", {}), f"{where}.metadata")
            name = metadata.get("name")
            if name is None and "generateName" in metadata:
                name = string(metadata["generateName"], f"{where}.metadata.generateName") + "*"
            if name is None and pod_path is None:
                name = "(unnamed)"
            identity = {
                "source": str(path), "document": doc_index, "apiVersion": api,
                "kind": kind, "namespace": string(metadata.get("namespace", "default"), f"{where}.namespace"),
                "name": string(name, f"{where}.metadata.name (or generateName)"),
            }
            if pod_path is None:
                unassessed.append({**identity, "path": "$", "reason": "unsupported kind/apiVersion; object not assessed"})
                continue
            pod_spec = doc
            walked = []
            for part in pod_path:
                walked.append(part)
                pod_spec = mapping(pod_spec.get(part), f"{where}.{'.'.join(walked)}")
            base = ".".join(pod_path)
            # Report newer image-bearing semantics rather than silently dropping them.
            if "ephemeralContainers" in pod_spec:
                value = pod_spec["ephemeralContainers"]
                if not isinstance(value, list):
                    raise InputError(f"{where}.{base}.ephemeralContainers: expected an array")
                if value:
                    unassessed.append({**identity, "path": base + ".ephemeralContainers",
                                       "reason": "ephemeral containers are outside the supported subset"})
            if "runtimeClassName" in pod_spec:
                string(pod_spec["runtimeClassName"], f"{where}.{base}.runtimeClassName")
                unassessed.append({**identity, "path": base + ".runtimeClassName",
                                   "reason": "runtime-handler-specific image availability is not assessed"})
            volumes = pod_spec.get("volumes", [])
            if not isinstance(volumes, list):
                raise InputError(f"{where}.{base}.volumes: expected an array")
            for index, volume in enumerate(volumes):
                volume = mapping(volume, f"{where}.{base}.volumes[{index}]")
                if "image" in volume:
                    unassessed.append({**identity, "path": f"{base}.volumes[{index}].image",
                                       "reason": "image volumes are outside the supported subset"})
            names = set()
            for field in ("initContainers", "containers"):
                items = pod_spec.get(field, [] if field == "initContainers" else None)
                if not isinstance(items, list) or (field == "containers" and not items):
                    raise InputError(f"{where}.{base}.{field}: expected an array" +
                                     (" with at least one container" if field == "containers" else ""))
                for index, container in enumerate(items):
                    item_path = f"{base}.{field}[{index}]"
                    container = mapping(container, f"{where}.{item_path}")
                    name = string(container.get("name"), f"{where}.{item_path}.name")
                    if name in names:
                        raise InputError(f"{where}: duplicate container name {name!r}")
                    names.add(name)
                    ref = parse_reference(container.get("image"), f"{where}.{item_path}.image")
                    explicit = "imagePullPolicy" in container
                    policy = container.get("imagePullPolicy", ref.default_policy)
                    if not isinstance(policy, str) or policy not in {"Always", "IfNotPresent", "Never"}:
                        raise InputError(f"{where}.{item_path}.imagePullPolicy: expected Always, IfNotPresent or Never")
                    containers.append({
                        **identity, "path": item_path, "container": name, "containerType": field,
                        "image": container["image"], "canonicalImage": ref.canonical,
                        "referenceType": "digest" if ref.digest else "tag",
                        "effectivePullPolicy": policy, "policySource": "explicit" if explicit else "default-on-create",
                    })
    if not object_count:
        raise InputError("manifests contain no Kubernetes objects")
    return containers, unassessed
