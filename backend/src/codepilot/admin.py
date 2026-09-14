from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
from typing import Any

from codepilot.auth import AuthStore, AuthStoreError, UserRole
from codepilot.config import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        store = _store(args)
        result = _run(store, args)
    except AuthStoreError as exc:
        parser.error(f"{exc} ({exc.code})")
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CodePilot 本机管理员 CLI")
    parser.add_argument("--codepilot-home", help="覆盖配置中的 codepilot_home")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init-admin", help="创建首个管理员")
    init.add_argument("--username", required=True)

    create = commands.add_parser("user-create", help="创建用户")
    create.add_argument("--username", required=True)
    create.add_argument("--role", choices=[item.value for item in UserRole], default=UserRole.USER.value)

    password = commands.add_parser("user-set-password", help="修改密码并撤销该用户会话")
    password.add_argument("user")

    for name in ("user-enable", "user-disable", "user-revoke-sessions"):
        command = commands.add_parser(name)
        command.add_argument("user")

    commands.add_parser("user-list")
    publish = commands.add_parser("shared-agent-publish", help="发布或更新共享只读 Agent")
    publish.add_argument("markdown_file")
    publish.add_argument("--actor", required=True, help="执行发布的管理员")
    archive = commands.add_parser("shared-agent-archive", help="归档共享 Agent")
    archive.add_argument("agent_id")
    archive.add_argument("--actor", required=True, help="执行归档的管理员")
    migrate = commands.add_parser("migrate-multi-user", help="预览或执行旧单用户数据迁移")
    migrate.add_argument("--admin", required=True, help="旧数据归属的初始管理员")
    migrate.add_argument("--apply", action="store_true", help="创建备份并执行迁移")
    return parser


def _store(args: argparse.Namespace) -> AuthStore:
    backend_dir = Path(__file__).resolve().parents[2]
    settings = load_settings(backend_dir / "config.yaml")
    home = Path(args.codepilot_home or settings.storage.codepilot_home).expanduser()
    return AuthStore(
        home / "auth.sqlite3",
        idle_timeout_seconds=settings.auth.idle_timeout_seconds,
        absolute_timeout_seconds=settings.auth.absolute_timeout_seconds,
    )


def _run(store: AuthStore, args: argparse.Namespace) -> dict[str, Any] | None:
    if args.command == "migrate-multi-user":
        principal = store.find_user(args.admin)
        if principal.role != UserRole.ADMIN:
            raise AuthStoreError("迁移归属用户必须是管理员", code="migration_admin_required")
        context = _app_context(args)
        from codepilot.migration import MultiUserMigration

        migration = MultiUserMigration(
            codepilot_home=context.workspace.codepilot_home,
            workspace_dir=context.workspace.workspace_dir,
        )
        if not args.apply:
            return migration.preview(principal.user_id)

        def profile_by_name(name: str) -> Any | None:
            matches = [
                profile
                for profile in context.agent_config_service.list_active_profile_snapshots(principal.user_id)
                if profile.name == name
            ]
            return matches[0] if len(matches) == 1 else None

        return migration.apply(principal.user_id, profile_by_name=profile_by_name)
    if args.command in {"shared-agent-publish", "shared-agent-archive"}:
        actor = store.find_user(args.actor)
        if actor.role != UserRole.ADMIN:
            raise AuthStoreError("只有管理员可以发布共享 Agent", code="admin_required")
        service = _shared_agent_service(args)
        if args.command == "shared-agent-publish":
            result = service.publish_markdown(Path(args.markdown_file))
            store.record_audit("shared_agent.published", actor_user_id=actor.user_id, data={"agent_id": result["agent_id"]})
            return result
        result = service.archive(args.agent_id)
        store.record_audit("shared_agent.archived", actor_user_id=actor.user_id, data={"agent_id": args.agent_id})
        return result
    if args.command == "init-admin":
        if store.has_users():
            raise AuthStoreError("认证库已经存在用户", code="auth_already_initialized")
        principal = store.create_user(args.username, _prompt_password(), role=UserRole.ADMIN)
        return principal.model_dump(mode="json")
    if args.command == "user-create":
        _require_initialized(store)
        principal = store.create_user(args.username, _prompt_password(), role=UserRole(args.role))
        return principal.model_dump(mode="json")
    if args.command == "user-list":
        _require_initialized(store)
        return {"users": store.list_users()}

    principal = store.find_user(args.user)
    if args.command == "user-set-password":
        store.set_password(principal.user_id, _prompt_password())
    elif args.command == "user-enable":
        store.set_enabled(principal.user_id, True)
    elif args.command == "user-disable":
        store.set_enabled(principal.user_id, False)
    elif args.command == "user-revoke-sessions":
        store.revoke_user_sessions(principal.user_id)
    return {"ok": True, "user_id": principal.user_id}


def _shared_agent_service(args: argparse.Namespace) -> Any:
    return _app_context(args).agent_config_service.shared


def _app_context(args: argparse.Namespace) -> Any:
    if args.codepilot_home:
        os.environ["CODEPILOT_HOME"] = str(Path(args.codepilot_home).expanduser())
    # 延迟导入避免普通账号命令初始化 LLM、MCP 与 workspace 运行时。
    from codepilot.main import app

    return app.state.context


def _require_initialized(store: AuthStore) -> None:
    if not store.has_users():
        raise AuthStoreError("请先执行 init-admin", code="auth_not_initialized")


def _prompt_password() -> str:
    first = getpass.getpass("密码：")
    second = getpass.getpass("再次输入密码：")
    if first != second:
        raise AuthStoreError("两次输入的密码不一致", code="password_confirmation_mismatch")
    return first


if __name__ == "__main__":
    raise SystemExit(main())
