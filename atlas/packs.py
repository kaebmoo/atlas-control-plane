from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from .db import ROLES, Database
from .workflows import (
    next_fire_at_for_trigger,
    validate_workflow_graph,
    validate_workflow_policy,
    validate_workflow_trigger_payload,
)

# Pack bundle format version (distinct from the DB schema_version). Bump only on a
# breaking change to the bundle shape. ponytail: one number, no negotiation layer.
PACK_SCHEMA_VERSION = 1
PACK_SIGNATURE_ALGORITHM = "HMAC-SHA256"
PACKS_DIR = Path(__file__).parent / "packs"


def _pack_signature(bundle: dict[str, Any], secret_key: str) -> str:
    """HMAC-SHA256 over the canonical bundle excluding the `signature` field, mirroring
    the usage-export signing approach in atlas/usage.py."""
    payload = {key: value for key, value in bundle.items() if key != "signature"}
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(secret_key.encode("utf-8"), encoded, hashlib.sha256).hexdigest()


def sign_pack(bundle: dict[str, Any], secret_key: str) -> dict[str, Any]:
    """Return a copy of the (valid) bundle carrying an HMAC signature."""
    if not secret_key:
        raise ValueError("ATLAS_SECRET_KEY is required to sign a pack")
    validate_pack(bundle)
    signed = {key: value for key, value in bundle.items() if key != "signature"}
    signed["signature"] = {"algorithm": PACK_SIGNATURE_ALGORITHM, "value": _pack_signature(signed, secret_key)}
    return signed


def verify_pack_signature(bundle: dict[str, Any], secret_key: str | None) -> bool:
    """True iff the bundle carries a valid HMAC signature for secret_key."""
    signature = bundle.get("signature")
    if not isinstance(signature, dict) or signature.get("algorithm") != PACK_SIGNATURE_ALGORITHM:
        return False
    value = signature.get("value")
    if not isinstance(value, str) or not secret_key:
        return False
    return hmac.compare_digest(value, _pack_signature(bundle, secret_key))


def validate_pack(bundle: Any) -> dict[str, Any]:
    """Validate a pack bundle. Returns it unchanged; raises ValueError with a clear
    message on any problem (bad graph/edge/role/trigger included)."""
    if not isinstance(bundle, dict):
        raise ValueError("pack bundle must be an object")
    if bundle.get("schema_version") != PACK_SCHEMA_VERSION:
        raise ValueError(f"pack schema_version must be {PACK_SCHEMA_VERSION}")
    name = bundle.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("pack requires a non-empty name")
    if not isinstance(bundle.get("version"), str) or not bundle["version"].strip():
        raise ValueError("pack requires a version string")

    workflows = bundle.get("workflows")
    if not isinstance(workflows, list) or not workflows:
        raise ValueError("pack requires a non-empty workflows list")
    for index, workflow in enumerate(workflows):
        if not isinstance(workflow, dict):
            raise ValueError(f"pack workflow at index {index} must be an object")
        if not isinstance(workflow.get("name"), str) or not workflow["name"].strip():
            raise ValueError(f"pack workflow at index {index} requires a name")
        # Run the real engine validators — never a bypass. Policy caps too, so an
        # imported pack cannot exceed limits the workflow API would reject.
        validate_workflow_graph(workflow.get("graph") or {}, workflow.get("policy"))
        validate_workflow_policy(workflow.get("policy"))

    roles = bundle.get("roles", [])
    if not isinstance(roles, list):
        raise ValueError("pack roles must be a list")
    for role in roles:
        if not isinstance(role, str) or role not in ROLES:
            raise ValueError(f"pack role is not a known RBAC role: {role}")

    triggers = bundle.get("triggers", [])
    if not isinstance(triggers, list):
        raise ValueError("pack triggers must be a list")
    for index, trigger in enumerate(triggers):
        if not isinstance(trigger, dict):
            raise ValueError(f"pack trigger at index {index} must be an object")
        target = trigger.get("workflow", 0)
        if not isinstance(target, int) or target < 0 or target >= len(workflows):
            raise ValueError(f"pack trigger at index {index} references unknown workflow {target}")
        validate_workflow_trigger_payload(trigger)

    return bundle


def _validate_pack_references(db: Database, bundle: dict[str, Any]) -> None:
    """Reject concrete worker/workspace ids that don't exist on this instance (they would
    dangle and fail later at routing), while allowing role-only nodes so packs stay
    portable across instances."""
    worker_ids = {worker["id"] for worker in db.list_workers()}
    workspace_ids = {workspace["id"] for workspace in db.list_workspaces()}
    for index, workflow in enumerate(bundle["workflows"]):
        graph = workflow.get("graph") or {}
        for node in graph.get("nodes") or []:
            worker_id = node.get("worker_id")
            if worker_id and worker_id not in worker_ids:
                raise ValueError(f"pack workflow {index} node {node.get('id')} references unknown worker_id: {worker_id}")
            workspace_id = node.get("workspace_id")
            if workspace_id and workspace_id not in workspace_ids:
                raise ValueError(f"pack workflow {index} node {node.get('id')} references unknown workspace_id: {workspace_id}")
        policy = workflow.get("policy") or {}
        for worker_id in policy.get("allowed_worker_ids") or []:
            if worker_id not in worker_ids:
                raise ValueError(f"pack workflow {index} policy allowed_worker_ids references unknown worker: {worker_id}")
        for workspace_id in policy.get("allowed_workspace_ids") or []:
            if workspace_id not in workspace_ids:
                raise ValueError(f"pack workflow {index} policy allowed_workspace_ids references unknown workspace: {workspace_id}")


def import_pack(
    db: Database,
    bundle: dict[str, Any],
    secret_key: str | None = None,
    require_signature: bool = False,
) -> dict[str, Any]:
    """Validate then create the bundle's workflow definitions + triggers, reusing the
    existing db writers. Returns the created definitions and triggers.

    Signature policy: if the bundle carries a signature it MUST verify against
    secret_key (a tampered or unverifiable signed pack is rejected). An unsigned pack is
    accepted unless require_signature is set. See docs/specs/pack-format.md."""
    validate_pack(bundle)
    _validate_pack_references(db, bundle)
    if bundle.get("signature") is not None:
        if not verify_pack_signature(bundle, secret_key):
            raise ValueError("pack signature is invalid")
    elif require_signature:
        raise ValueError("pack is unsigned but a signature is required")
    definitions: list[dict[str, Any]] = []
    for workflow in bundle["workflows"]:
        definitions.append(
            db.create_workflow_definition(
                {
                    "name": workflow["name"],
                    "description": workflow.get("description") or "",
                    "version": int(workflow.get("version") or 1),
                    "status": workflow.get("status") or "active",
                    "graph": workflow.get("graph") or {},
                    "policy": workflow.get("policy") or {},
                }
            )
        )

    triggers: list[dict[str, Any]] = []
    for trigger in bundle.get("triggers", []):
        definition_id = definitions[trigger.get("workflow", 0)]["id"]
        trigger_payload: dict[str, Any] = {
            "workflow_definition_id": definition_id,
            "name": trigger.get("name") or "Trigger",
            "type": trigger.get("type") or "manual",
            "config": trigger.get("config") or {},
            "enabled": trigger.get("enabled", True),
        }
        # Mirror the trigger API: compute the first fire time so schedule triggers
        # actually become due (None for non-schedule types).
        trigger_payload["next_fire_at"] = next_fire_at_for_trigger(trigger_payload)
        triggers.append(db.create_workflow_trigger(trigger_payload))

    return {
        "pack": {"name": bundle["name"], "version": bundle["version"]},
        "workflows": definitions,
        "triggers": triggers,
    }


def export_pack(db: Database, definition_id: str) -> dict[str, Any]:
    """Serialize one workflow definition (and its triggers) back into a bundle."""
    definition = db.get_workflow_definition(definition_id)
    if not definition:
        raise ValueError(f"unknown workflow definition: {definition_id}")
    triggers = db.list_workflow_triggers(workflow_definition_id=definition_id)
    return {
        "schema_version": PACK_SCHEMA_VERSION,
        "name": definition["name"],
        "version": str(definition.get("version") or 1),
        "description": definition.get("description") or "",
        "roles": [],
        "sample_input": {},
        "docs": "",
        "workflows": [
            {
                "name": definition["name"],
                "description": definition.get("description") or "",
                "version": int(definition.get("version") or 1),
                "status": definition.get("status") or "active",
                "graph": definition.get("graph") or {},
                "policy": definition.get("policy") or {},
            }
        ],
        "triggers": [
            {
                "workflow": 0,
                "name": trigger["name"],
                "type": trigger["type"],
                "config": trigger.get("config") or {},
                "enabled": bool(trigger.get("enabled", True)),
            }
            for trigger in triggers
        ],
    }


def load_pack_file(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def list_available_packs(packs_dir: Path = PACKS_DIR) -> list[dict[str, Any]]:
    """Summarize the bundle files shipped under the packs directory. Invalid bundles
    are listed with an `error` so the UI can surface them rather than hiding them."""
    packs_dir = Path(packs_dir)
    if not packs_dir.is_dir():
        return []
    summaries: list[dict[str, Any]] = []
    for path in sorted(packs_dir.glob("*.json")):
        entry: dict[str, Any] = {"file": path.name}
        try:
            bundle = load_pack_file(path)
            validate_pack(bundle)
            entry.update(
                {
                    "name": bundle["name"],
                    "version": bundle["version"],
                    "schema_version": bundle["schema_version"],
                    "description": bundle.get("description") or "",
                    "workflows": len(bundle.get("workflows") or []),
                    "triggers": len(bundle.get("triggers") or []),
                    "signed": isinstance(bundle.get("signature"), dict),
                }
            )
        except (ValueError, json.JSONDecodeError) as exc:
            entry["error"] = str(exc)
        summaries.append(entry)
    return summaries


def main(argv: list[str] | None = None) -> None:
    import argparse

    from .config import Config

    parser = argparse.ArgumentParser(description="Sign or verify Atlas solution packs")
    subparsers = parser.add_subparsers(dest="command", required=True)
    sign_parser = subparsers.add_parser("sign", help="sign a pack bundle in place (or to --output)")
    sign_parser.add_argument("path", type=Path)
    sign_parser.add_argument("--output", type=Path)
    verify_parser = subparsers.add_parser("verify", help="verify a pack bundle signature")
    verify_parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)

    config = Config.from_env()
    if not config.secret_key:
        parser.error("ATLAS_SECRET_KEY is required")
    if args.command == "sign":
        signed = sign_pack(load_pack_file(args.path), config.secret_key)
        output = args.output or args.path
        Path(output).write_text(json.dumps(signed, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        print(output)
        return
    if not verify_pack_signature(load_pack_file(args.path), config.secret_key):
        raise SystemExit("pack signature is invalid or missing")
    print("pack signature is valid")


if __name__ == "__main__":
    main()
