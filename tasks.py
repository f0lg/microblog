import asyncio
import io
import shutil
import tarfile
from contextlib import contextmanager
from pathlib import Path
from typing import Generator
from typing import Optional

import httpx
from invoke import Context  # type: ignore
from invoke import run  # type: ignore
from invoke import task  # type: ignore


@task
def generate_db_migration(ctx, message):
    # type: (Context, str) -> None
    run(f'alembic revision --autogenerate -m "{message}"', echo=True)


@task
def migrate_db(ctx):
    # type: (Context) -> None
    run("alembic upgrade head", echo=True)


@task
def autoformat(ctx):
    # type: (Context) -> None
    run("black .", echo=True)
    run("isort -sl .", echo=True)


@task
def lint(ctx):
    # type: (Context) -> None
    run("black --check .", echo=True)
    run("isort -sl --check-only .", echo=True)
    run("flake8 .", echo=True)
    run("mypy .", echo=True)


@task
def compile_scss(ctx, watch=False):
    # type: (Context, bool) -> None
    from app.utils.favicon import build_favicon

    theme_file = Path("data/_theme.scss")
    if not theme_file.exists():
        theme_file.write_text("// override vars for theming here")

    favicon_file = Path("data/favicon.ico")
    if not favicon_file.exists():
        build_favicon()
    else:
        shutil.copy2(favicon_file, "app/static/favicon.ico")

    if watch:
        run("boussole watch", echo=True)
    else:
        run("boussole compile", echo=True)


@task
def uvicorn(ctx):
    # type: (Context) -> None
    run("uvicorn app.main:app --no-server-header", pty=True, echo=True)


@task
def process_outgoing_activities(ctx):
    # type: (Context) -> None
    from app.outgoing_activities import loop

    asyncio.run(loop())


@task
def process_incoming_activities(ctx):
    # type: (Context) -> None
    from app.incoming_activities import loop

    asyncio.run(loop())


@task
def tests(ctx, k=None):
    # type: (Context, Optional[str]) -> None
    pytest_args = " -vvv"
    if k:
        pytest_args += f" -k {k}"
    run(
        f"MICROBLOGPUB_CONFIG_FILE=tests.toml pytest tests{pytest_args}",
        pty=True,
        echo=True,
    )


@task
def generate_requirements_txt(ctx, where="requirements.txt"):
    # type: (Context, str) -> None
    run(
        f"poetry export -f requirements.txt --without-hashes > {where}",
        pty=True,
        echo=True,
    )


@task
def build_docs(ctx):
    # type: (Context) -> None
    with embed_version():
        run("PYTHONPATH=. python scripts/build_docs.py", pty=True, echo=True)


@task
def download_twemoji(ctx):
    # type: (Context) -> None
    resp = httpx.get(
        "https://github.com/twitter/twemoji/archive/refs/tags/v14.0.2.tar.gz",
        follow_redirects=True,
    )
    resp.raise_for_status()
    tf = tarfile.open(fileobj=io.BytesIO(resp.content))
    members = [
        member
        for member in tf.getmembers()
        if member.name.startswith("twemoji-14.0.2/assets/svg/")
    ]
    for member in members:
        emoji_name = Path(member.name).name
        with open(f"app/static/twemoji/{emoji_name}", "wb") as f:
            f.write(tf.extractfile(member).read())  # type: ignore


@task(download_twemoji, compile_scss)
def configuration_wizard(ctx):
    # type: (Context) -> None
    run("MICROBLOGPUB_CONFIG_FILE=tests.toml alembic upgrade head", echo=True)
    run(
        "MICROBLOGPUB_CONFIG_FILE=tests.toml PYTHONPATH=. python scripts/config_wizard.py",  # noqa: E501
        pty=True,
        echo=True,
    )


@task
def install_deps(ctx):
    # type: (Context) -> None
    run("poetry install", pty=True, echo=True)


@task(pre=[compile_scss], post=[migrate_db])
def update(ctx, update_deps=True):
    # type: (Context, bool) -> None
    if update_deps:
        run("poetry install", pty=True, echo=True)
    print("Done")


@task
def stats(ctx):
    # type: (Context) -> None
    from app.utils.stats import print_stats

    print_stats()


@contextmanager
def embed_version() -> Generator[None, None, None]:
    from app.utils.version import get_version_commit

    version_file = Path("app/_version.py")
    version_file.unlink(missing_ok=True)
    version_commit = get_version_commit()
    version_file.write_text(f'VERSION_COMMIT = "{version_commit}"')
    try:
        yield
    finally:
        version_file.unlink()


@task
def build_docker_image(ctx):
    # type: (Context) -> None
    with embed_version():
        run("docker build -t microblogpub/microblogpub .")


@task
def prune_old_data(ctx):
    # type: (Context) -> None
    from app.prune import run_prune_old_data

    asyncio.run(run_prune_old_data())


@task
def webfinger(ctx, account):
    # type: (Context, str) -> None
    import traceback

    from loguru import logger

    from app.source import _MENTION_REGEX
    from app.webfinger import get_actor_url

    logger.disable("app")
    if not account.startswith("@"):
        account = f"@{account}"
    if not _MENTION_REGEX.match(account):
        print(f"Invalid acccount {account}")
        return

    print(f"Resolving {account}")
    try:
        maybe_actor_url = asyncio.run(get_actor_url(account))
        if maybe_actor_url:
            print(f"SUCCESS: {maybe_actor_url}")
        else:
            print(f"ERROR: Failed to resolve {account}")
    except Exception as exc:
        print(f"ERROR: Failed to resolve {account}")
        print("".join(traceback.format_exception(exc)))


@task
def move_to(ctx, moved_to):
    # type: (Context, str) -> None
    import traceback

    from loguru import logger

    from app.actor import LOCAL_ACTOR
    from app.actor import fetch_actor
    from app.boxes import send_move
    from app.database import async_session
    from app.source import _MENTION_REGEX
    from app.webfinger import get_actor_url

    logger.disable("app")

    if not moved_to.startswith("@"):
        moved_to = f"@{moved_to}"
    if not _MENTION_REGEX.match(moved_to):
        print(f"Invalid acccount {moved_to}")
        return

    async def _send_move():
        print(f"Initiating move to {moved_to}")
        async with async_session() as db_session:
            try:
                moved_to_actor_id = await get_actor_url(moved_to)
            except Exception as exc:
                print(f"ERROR: Failed to resolve {moved_to}")
                print("".join(traceback.format_exception(exc)))
                return

            if not moved_to_actor_id:
                print("ERROR: Failed to resolve {moved_to}")
                return

            new_actor = await fetch_actor(db_session, moved_to_actor_id)

            if LOCAL_ACTOR.ap_id not in new_actor.ap_actor.get("alsoKnownAs", []):
                print(
                    f"{new_actor.handle}/{moved_to_actor_id} is missing "
                    f"{LOCAL_ACTOR.ap_id} in alsoKnownAs"
                )
                return

            await send_move(db_session, new_actor.ap_id)

        print("Done")

    asyncio.run(_send_move())


@task
def self_destruct(ctx):
    # type: (Context) -> None
    from loguru import logger

    from app.boxes import send_self_destruct
    from app.database import async_session

    logger.disable("app")

    async def _send_self_destruct():
        if input("Initiating self destruct, type yes to confirm: ") != "yes":
            print("Aborting")

        async with async_session() as db_session:
            await send_self_destruct(db_session)

        print("Done")

    asyncio.run(_send_self_destruct())


@task
def yunohost_config(
    ctx,
    domain,
    username,
    name,
    summary,
    password,
):
    # type: (Context, str, str, str, str, str) -> None
    from app.utils import yunohost

    yunohost.setup_config_file(
        domain=domain,
        username=username,
        name=name,
        summary=summary,
        password=password,
    )


@task
def reset_password(ctx):
    # type: (Context) -> None
    import bcrypt
    from prompt_toolkit import prompt

    new_password = bcrypt.hashpw(
        prompt("New admin password: ", is_password=True).encode(), bcrypt.gensalt()
    ).decode()

    print()
    print("Update data/profile.toml with:")
    print(f'admin_password = "{new_password}"')


@task
def check_config(ctx):
    # type: (Context) -> None
    import sys
    import traceback

    from loguru import logger

    logger.disable("app")

    try:
        from app import config  # noqa: F401
    except Exception as exc:
        print("Config error, please fix data/profile.toml:\n")
        print("".join(traceback.format_exception(exc)))
        sys.exit(1)
    else:
        print("Config is OK")
