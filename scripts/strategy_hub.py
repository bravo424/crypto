from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "strategies" / "registry.json"
DESIGN_PATH = ROOT / "design"


def load_registry() -> list[dict]:
    if not REGISTRY_PATH.exists():
        return []
    with REGISTRY_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError("Registry file must be a JSON list")
    return data


def save_registry(items: list[dict]) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with REGISTRY_PATH.open("w", encoding="utf-8") as handle:
        json.dump(items, handle, indent=2, ensure_ascii=False)


def find_bybit_accounts() -> list[str]:
    if not DESIGN_PATH.exists():
        return []
    accounts: list[str] = []
    for path in sorted(DESIGN_PATH.glob("_cred_bybit_*")):
        suffix = path.name.removeprefix("_cred_bybit_")
        if suffix:
            accounts.append(suffix)
    return accounts


def cmd_list(as_json: bool) -> None:
    items = load_registry()
    accounts = find_bybit_accounts()

    if as_json:
        payload = {
            "strategies": items,
            "available_bybit_accounts": accounts,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    print("=== Strategy Hub ===")
    if not items:
        print("No strategies registered yet.")
    for idx, item in enumerate(items, start=1):
        print(f"{idx}. {item.get('name', '-')}")
        print(f"   id: {item.get('id', '-')}")
        print(f"   type/status: {item.get('type', '-')} / {item.get('status', '-')}")
        print(f"   entry: {item.get('entry', '-')}")
        print(f"   source: {item.get('source', '-')}")
        print(f"   schedule: {item.get('schedule', '-')}")
        print(f"   description: {item.get('description', '-')}")

    print("\nAvailable Bybit accounts:")
    if accounts:
        for account in accounts:
            print(f"- {account}")
    else:
        print("- none found")


def cmd_add(args: argparse.Namespace) -> None:
    items = load_registry()
    if any(item.get("id") == args.id for item in items):
        raise ValueError(f"Strategy id already exists: {args.id}")

    items.append(
        {
            "id": args.id,
            "name": args.name,
            "type": args.type,
            "status": args.status,
            "entry": args.entry,
            "source": args.source,
            "schedule": args.schedule,
            "description": args.description,
        }
    )
    save_registry(items)
    print(f"Added strategy: {args.id}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="One-shot view and management for all trading strategies")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="Show all strategies")
    list_parser.add_argument("--json", action="store_true", help="Print JSON output")

    add_parser = sub.add_parser("add", help="Register a new strategy")
    add_parser.add_argument("--id", required=True, help="Unique strategy id")
    add_parser.add_argument("--name", required=True, help="Display name")
    add_parser.add_argument("--type", default="monitoring", help="execution|monitoring|analysis")
    add_parser.add_argument("--status", default="active", help="active|paused|draft")
    add_parser.add_argument("--entry", required=True, help="How to run it")
    add_parser.add_argument("--source", required=True, help="Primary source file")
    add_parser.add_argument("--schedule", default="manual", help="Scheduling note")
    add_parser.add_argument("--description", default="", help="What this strategy does")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "list":
        cmd_list(as_json=args.json)
        return

    if args.command == "add":
        cmd_add(args)
        return

    raise ValueError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
