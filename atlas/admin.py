from __future__ import annotations

import argparse
import getpass
import json

from .config import Config
from .db import Database, ROLES


def _password() -> str:
    password = getpass.getpass("Password: ")
    if not password:
        raise ValueError("password is required")
    return password


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Manage Atlas users and API tokens")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_admin = subparsers.add_parser("create-admin", help="create an administrator and one-time API token")
    create_admin.add_argument("username")
    create_admin.add_argument("--token-name", default="bootstrap")

    create_user = subparsers.add_parser("create-user", help="create a user")
    create_user.add_argument("username")
    create_user.add_argument("--role", choices=sorted(ROLES), default="viewer")

    create_token = subparsers.add_parser("create-token", help="create a one-time API token")
    create_token.add_argument("username")
    create_token.add_argument("--name", default="cli")

    revoke_token = subparsers.add_parser("revoke-token", help="revoke an API token by id")
    revoke_token.add_argument("token_id")

    subparsers.add_parser("list-users", help="list users")
    args = parser.parse_args(argv)

    config = Config.from_env()
    db = Database(config.db_path, secret_key=config.secret_key)

    if args.command == "create-admin":
        with db.as_actor("atlas-admin-cli"):
            user = db.create_user(args.username, _password(), role="admin")
            token, raw_token = db.create_api_token(user["id"], args.token_name)
        print(f"Created admin {user['username']} ({user['id']})")
        print(f"Token id: {token['id']}")
        print(f"One-time token: {raw_token}")
        return
    if args.command == "create-user":
        with db.as_actor("atlas-admin-cli"):
            user = db.create_user(args.username, _password(), role=args.role)
        print(json.dumps(user, ensure_ascii=True))
        return
    if args.command == "create-token":
        user = db.get_user_by_username(args.username)
        if not user:
            raise SystemExit(f"Unknown username: {args.username}")
        with db.as_actor("atlas-admin-cli"):
            token, raw_token = db.create_api_token(user["id"], args.name)
        print(f"Token id: {token['id']}")
        print(f"One-time token: {raw_token}")
        return
    if args.command == "revoke-token":
        with db.as_actor("atlas-admin-cli"):
            if not db.revoke_api_token(args.token_id):
                raise SystemExit(f"Unknown or already revoked token: {args.token_id}")
        print(f"Revoked {args.token_id}")
        return
    print(json.dumps({"users": db.list_users()}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
