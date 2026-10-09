"""
`timelapse server connect | status | disconnect`.

Connecting logs in once with the user's account, registers this Pi as a
camera, and stores the device token the server returns in the 0600 secrets
file. The account password is never written to disk.
"""

from __future__ import annotations

import socket
from typing import Optional

import click

from timelapse.config.loader import load_global_config, save_global_config
from timelapse.config.models import ServerConfig
from timelapse.server.client import ServerClient, ServerError
from timelapse.server.uploader import PhotoUploader
from timelapse.utils.secrets import get_secret, write_secret


def _normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = f"http://{url}"
    return url


def _pi_model() -> str:
    from timelapse.utils.pi_detect import get_model_string

    return get_model_string().strip("\x00 \n")


def connect(url: Optional[str] = None, name: Optional[str] = None) -> None:
    glob = load_global_config()
    if glob.server and not click.confirm(
        f"This Pi is already connected to {glob.server.url} as "
        f"'{glob.server.pi_name}'. Connect again (registers a new camera)?",
        default=False,
    ):
        return

    # ── server ────────────────────────────────────────────────────────────
    while True:
        url = _normalize_url(url or click.prompt("Server URL (e.g. http://pilapse.local:3000)"))
        client = ServerClient(url)
        try:
            info = client.info()
            break
        except ServerError as exc:
            click.echo(f"  ✗ {exc}")
            url = None
    click.echo(f"  ✓ Connected to PiLapse Server {info.get('version', '')}".rstrip())

    # ── account ───────────────────────────────────────────────────────────
    while True:
        try:
            if info.get("setupRequired"):
                click.echo("  This server has no accounts yet; creating the owner account.")
                user_name = click.prompt("Your name")
                email = click.prompt("Email")
                password = click.prompt(
                    "Password (8+ characters)", hide_input=True, confirmation_prompt=True
                )
                client.signup(user_name, email, password)
                click.echo("  ✓ Owner account created")
            else:
                email = click.prompt("Email")
                password = click.prompt("Password", hide_input=True)
                client.login(email, password)
                click.echo("  ✓ Logged in")
            break
        except ServerError as exc:
            click.echo(f"  ✗ {exc}")
            if exc.is_transient:
                raise click.ClickException("Server stopped responding; try again.")

    # ── camera ────────────────────────────────────────────────────────────
    while True:
        name = name or click.prompt("Name this camera", default=socket.gethostname())
        try:
            pi, token = client.add_pi(name, model=_pi_model())
            break
        except ServerError as exc:
            click.echo(f"  ✗ {exc}")
            if exc.is_transient:
                raise click.ClickException("Server stopped responding; try again.")
            name = None

    server_cfg = ServerConfig(url=url, pi_id=pi["_id"], pi_name=pi["name"])
    write_secret(server_cfg.secrets_file, server_cfg.token_env, token)
    glob.server = server_cfg
    save_global_config(glob)

    click.echo(f"  ✓ Registered \"{pi['name']}\". Photos from every profile will upload to the server.")
    click.echo("    Restart PiLapse to start uploading: sudo systemctl restart timelapse")


def status() -> None:
    glob = load_global_config()
    cfg = glob.server
    if cfg is None:
        click.echo("Not connected to a server. Run: timelapse server connect")
        return

    token = get_secret(cfg.token_env, cfg.secrets_file)
    client = ServerClient(cfg.url, device_token=token, timeout_s=cfg.timeout_s)
    queued = len(PhotoUploader(client, cfg.pi_id).pending())

    click.echo(f"  Server:   {cfg.url}")
    click.echo(f"  Camera:   {cfg.pi_name} ({cfg.pi_id})")
    click.echo(f"  Queued:   {queued} photo(s) waiting to upload")
    try:
        info = client.info()
        click.echo(f"  Reachable: yes (server {info.get('version', '?')})")
    except ServerError as exc:
        click.echo(f"  Reachable: no ({exc})")
        return
    if not token:
        click.echo(f"  Token:    missing (${cfg.token_env} / {cfg.secrets_file})")
        return
    try:
        pi = client.get_pi(cfg.pi_id)
        click.echo("  Token:    valid")
        if pi.get("intents"):
            click.echo(f"  Intents:  {', '.join(pi['intents'])}")
        if pi.get("lastSeenAt"):
            click.echo(f"  Last upload: {pi['lastSeenAt']}")
    except ServerError as exc:
        click.echo(f"  Token:    rejected ({exc}). Run: timelapse server connect")


def disconnect() -> None:
    glob = load_global_config()
    cfg = glob.server
    if cfg is None:
        click.echo("Not connected to a server.")
        return
    if not click.confirm(f"Stop uploading to {cfg.url}?", default=False):
        return
    write_secret(cfg.secrets_file, cfg.token_env, None)
    glob.server = None
    save_global_config(glob)
    click.echo("  ✓ Disconnected. Photos keep being captured locally.")
    click.echo(
        f"    The camera '{cfg.pi_name}' still exists on the server; revoke its token "
        "there if this Pi is being retired."
    )
