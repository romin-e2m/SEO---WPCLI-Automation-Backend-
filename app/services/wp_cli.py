from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Sequence

import paramiko


@dataclass(frozen=True)
class WpCliSshConfig:
    host: str
    user: str
    port: int = 22
    identity_file: str | None = None
    connect_timeout_seconds: int = 10


@dataclass(frozen=True)
class WpCliConfig:
    mode: str  # "local" | "ssh"
    wp_path: str | None = None
    ssh: WpCliSshConfig | None = None


class WpCliError(RuntimeError):
    pass


def _wp_base_args(*, wp_path: str | None) -> list[str]:
    args = ["wp"]
    if wp_path:
        args.append(f"--path={wp_path}")
    # Keep output clean for parsing.
    args.extend(["--quiet", "--no-color"])
    return args


def _ssh_args(ssh: WpCliSshConfig) -> list[str]:
    args = [
        "ssh",
        "-p",
        str(ssh.port),
        "-o",
        f"ConnectTimeout={int(ssh.connect_timeout_seconds)}",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if ssh.identity_file:
        args.extend(["-i", ssh.identity_file])
    args.append(f"{ssh.user}@{ssh.host}")
    return args


class WpCliRunner:
    def __init__(self, cfg: WpCliConfig) -> None:
        self._cfg = cfg

    def run(self, args: Sequence[str], *, timeout_seconds: int = 30) -> str:
        base = _wp_base_args(wp_path=self._cfg.wp_path)
        full = base + list(args)

        if self._cfg.mode == "local":
            proc = subprocess.run(
                full,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        elif self._cfg.mode == "ssh":
            if not self._cfg.ssh:
                raise WpCliError("wp_cli.mode=ssh requires ssh config.")
            remote = shlex.join(full)
            if shutil.which("ssh"):
                proc = subprocess.run(
                    _ssh_args(self._cfg.ssh) + [remote],
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
            else:
                stdout, stderr, rc = _run_paramiko(remote, self._cfg.ssh, timeout_seconds=timeout_seconds)
                proc = subprocess.CompletedProcess(args=["paramiko-ssh", remote], returncode=rc, stdout=stdout, stderr=stderr)
        else:
            raise WpCliError(f"Unsupported wp_cli mode: {self._cfg.mode}")

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise WpCliError(stderr or f"wp-cli failed (exit {proc.returncode})")
        return (proc.stdout or "").strip()

    def json(self, args: Sequence[str], *, timeout_seconds: int = 30) -> Any:
        out = self.run(args, timeout_seconds=timeout_seconds)
        try:
            return json.loads(out) if out else None
        except Exception as e:
            raise WpCliError("wp-cli output was not valid JSON.") from e

    def version(self) -> str:
        return self.run(["--version"], timeout_seconds=10)

    def active_plugins(self) -> list[str]:
        # `--format=json` exists and is stable.
        data = self.json(["plugin", "list", "--status=active", "--format=json"], timeout_seconds=20)
        if not isinstance(data, list):
            return []
        out: list[str] = []
        for row in data:
            name = row.get("name") if isinstance(row, dict) else None
            if isinstance(name, str) and name:
                out.append(name)
        return out

    def url_to_postid(self, url: str) -> int | None:
        # Use WP core utility for reliable mapping.
        expr = (
            "if (function_exists('url_to_postid')) { "
            f"$id = url_to_postid({url!r}); echo $id ? intval($id) : 0; "
            "} else { echo 0; }"
        )
        out = self.run(["eval", expr], timeout_seconds=20)
        try:
            v = int((out or "0").strip())
            return v if v > 0 else None
        except Exception:
            return None

    def attachment_id_from_url(self, url: str) -> int | None:
        # attachment_url_to_postid covers media library attachments.
        expr = (
            "if (function_exists('attachment_url_to_postid')) { "
            f"$id = attachment_url_to_postid({url!r}); echo $id ? intval($id) : 0; "
            "} else { echo 0; }"
        )
        out = self.run(["eval", expr], timeout_seconds=20)
        try:
            v = int((out or "0").strip())
            return v if v > 0 else None
        except Exception:
            return None

    def update_post_meta(self, post_id: int, meta_key: str, meta_value: str) -> None:
        self.run(
            ["post", "meta", "update", str(int(post_id)), meta_key, meta_value],
            timeout_seconds=20,
        )

    def get_post_meta(self, post_id: int, meta_key: str) -> str | None:
        """Return a single meta value, or None if missing / empty / error."""
        try:
            out = self.run(
                ["post", "meta", "get", str(int(post_id)), meta_key, "--single"],
                timeout_seconds=20,
            )
        except WpCliError:
            return None
        v = (out or "").strip()
        return v if v else None


def _run_paramiko(command: str, ssh: WpCliSshConfig, *, timeout_seconds: int) -> tuple[str, str, int]:
    """
    Execute a command over SSH without relying on an `ssh` binary.
    Supports key auth via:
    - ssh.identity_file (path inside the container), or
    - env WPCLI_SSH_PRIVATE_KEY (PEM text)
    """
    key_obj = None
    key_text = os.getenv("WPCLI_SSH_PRIVATE_KEY")
    if key_text:
        try:
            key_obj = paramiko.RSAKey.from_private_key(io.StringIO(key_text))
        except Exception:
            # Try Ed25519 as a common modern default
            try:
                key_obj = paramiko.Ed25519Key.from_private_key(io.StringIO(key_text))
            except Exception as e:
                raise WpCliError("Invalid WPCLI_SSH_PRIVATE_KEY format.") from e
    elif ssh.identity_file:
        try:
            key_obj = paramiko.RSAKey.from_private_key_file(ssh.identity_file)
        except Exception:
            try:
                key_obj = paramiko.Ed25519Key.from_private_key_file(ssh.identity_file)
            except Exception as e:
                raise WpCliError("Failed to read SSH identity_file inside backend container.") from e

    if key_obj is None:
        raise WpCliError(
            "SSH key not provided. Set site.wp_cli.ssh.identity_file (mounted into the container) "
            "or set WPCLI_SSH_PRIVATE_KEY in the backend environment."
        )

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=ssh.host,
            port=int(ssh.port),
            username=ssh.user,
            pkey=key_obj,
            timeout=float(timeout_seconds),
            banner_timeout=float(timeout_seconds),
            auth_timeout=float(timeout_seconds),
        )
        chan = client.get_transport().open_session()  # type: ignore[union-attr]
        chan.settimeout(float(timeout_seconds))
        chan.exec_command(command)
        stdout = chan.makefile("r", -1).read()
        stderr = chan.makefile_stderr("r", -1).read()
        rc = int(chan.recv_exit_status())
        return stdout, stderr, rc
    except WpCliError:
        raise
    except Exception as e:
        raise WpCliError(str(e)) from e
    finally:
        try:
            client.close()
        except Exception:
            pass

