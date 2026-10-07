#!/usr/bin/env python3
"""Finish a sign-in challenge on the deployed scraper, in one command, from your own machine.

    python3 deploy/reauth.py root@your-server --only google_classroom
    python3 deploy/reauth.py --local --only google_classroom      # podman/docker on this machine

It finds the scraper container, starts deploy/container/login.sh in it (a headed scrape
on a virtual display), and opens the browser window in your VNC viewer (Screen Sharing on
macOS). Finish the sign-in there; the scrape then carries on and the session is saved in
the data volume. Ctrl-C stops everything on the server too.

How it stays private: VNC listens only on the container's loopback, and this script
reaches it by piping each VNC connection through `docker exec` over your existing SSH
login, so no port is opened on the server or the container network. On this machine it
listens on 127.0.0.1 only, on a random port. The VNC password is random for each session.

Standard library only, so it runs without the project's virtualenv.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

LOGIN_SH = "/app/deploy/container/login.sh"

# Runs inside the container for each VNC connection: joins stdin/stdout to x11vnc.
PIPE = r"""
import os, socket, sys, threading
s = socket.create_connection(("127.0.0.1", 5900))
def up():
    while b := os.read(0, 65536):
        s.sendall(b)
    s.shutdown(socket.SHUT_WR)
threading.Thread(target=up, daemon=True).start()
while b := s.recv(65536):
    sys.stdout.buffer.write(b)
    sys.stdout.buffer.flush()
"""


class Remote:
    """Runs container-engine commands on the server over SSH (sharing one SSH connection),
    or on this machine with --local."""

    def __init__(self, host: str | None, docker: list[str]):
        self.host = host
        self.docker = docker
        self.ssh: list[str] = []
        if host:
            # Short path: macOS caps unix socket paths at 104 bytes, and $TMPDIR is long.
            self.ctl_dir = tempfile.mkdtemp(prefix="lifeapi-", dir="/tmp")
            self.ssh = ["ssh", "-o", "ControlMaster=auto", "-o", f"ControlPath={self.ctl_dir}/%C",
                        "-o", "ControlPersist=60", host]

        self.sudo_password: str | None = None

    def wrap(self, cmd: list[str]) -> list[str]:
        # ssh joins its arguments into one string for the remote shell, so quote them.
        return [*self.ssh, shlex.join(cmd)] if self.host else cmd

    def argv(self, *args: str) -> list[str]:
        return self.wrap([*self.docker, *args])

    @property
    def stdin_prefix(self) -> bytes:
        """What to write to a command's stdin before anything else: the sudo password, which
        `sudo -S` reads one byte at a time up to the newline, leaving the rest for docker."""
        return f"{self.sudo_password}\n".encode() if self.sudo_password is not None else b""

    def output(self, *args: str) -> str:
        return subprocess.run(self.argv(*args), check=True, capture_output=True, text=True,
                              input=self.stdin_prefix.decode()).stdout

    def use_sudo(self, force: bool) -> None:
        """Run the engine through sudo if it needs root (the user isn't in the docker group).
        Over SSH there's no terminal for sudo to prompt on, so a password is asked for here
        and passed with `sudo -S`."""
        if not force:
            probe = subprocess.run(self.argv("ps", "-q"), capture_output=True, text=True)
            if probe.returncode == 0:
                return
            if "permission denied" not in probe.stderr.lower():
                sys.exit(f"reauth: {shlex.join(probe.args)} failed:\n{probe.stderr.strip()}")
        # -k ignores cached credentials, so this succeeds only when sudo needs no password for
        # the engine, and a password line sent later can never leak into docker's stdin.
        if subprocess.run(self.wrap(["sudo", "-k", "-n", *self.docker, "ps", "-q"]),
                          capture_output=True).returncode == 0:
            self.docker = ["sudo", "-n", *self.docker]
            return
        where = f" on {self.host}" if self.host else ""
        self.sudo_password = getpass.getpass(f"reauth: {self.docker[0]} needs sudo{where}. "
                                             "sudo password: ")
        self.docker = ["sudo", "-k", "-S", "-p", "", *self.docker]
        try:
            self.output("ps", "-q")
        except subprocess.CalledProcessError as e:
            sys.exit(f"reauth: sudo {self.docker[-1]} failed:\n{e.stderr.strip()}")

    def close(self) -> None:
        if self.host:
            subprocess.run(["ssh", "-o", f"ControlPath={self.ctl_dir}/%C", "-O", "exit", self.host],
                           capture_output=True)
            shutil.rmtree(self.ctl_dir, ignore_errors=True)


def find_container(remote: Remote) -> str:
    names: list[str] = []
    # docker compose (and Coolify) set the first label; podman-compose sets the second.
    for label in ("com.docker.compose.service=scraper", "io.podman.compose.service=scraper"):
        out = remote.output("ps", "--filter", f"label={label}", "--format", "{{.Names}}")
        names = [n for n in out.split() if n]
        if names:
            break
    if len(names) != 1:
        found = ", ".join(names) or "none"
        sys.exit(f"reauth: expected one running scraper container, found {found}. "
                 "Pass --container NAME.")
    return names[0]


def sock_to_pipe(conn: socket.socket, pipe) -> None:
    try:
        while data := conn.recv(65536):
            pipe.write(data)
            pipe.flush()
    except OSError:
        pass
    finally:
        pipe.close()


def pipe_to_sock(pipe, conn: socket.socket) -> None:
    try:
        while data := pipe.read1(65536):
            conn.sendall(data)
    except OSError:
        pass
    finally:
        conn.close()


def serve_vnc(remote: Remote, container: str, listener: socket.socket) -> None:
    """Forward every connection to the listener into the container's VNC server."""
    while True:
        try:
            conn, _ = listener.accept()
        except OSError:
            return  # listener closed
        proc = subprocess.Popen(remote.argv("exec", "-i", container, "python", "-c", PIPE),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                start_new_session=True)
        if remote.stdin_prefix:
            proc.stdin.write(remote.stdin_prefix)
            proc.stdin.flush()

        def run(conn=conn, proc=proc):
            threading.Thread(target=sock_to_pipe, args=(conn, proc.stdin), daemon=True).start()
            pipe_to_sock(proc.stdout, conn)
            proc.kill()

        threading.Thread(target=run, daemon=True).start()


def open_viewer(port: int, password: str) -> None:
    url = f"vnc://:{password}@127.0.0.1:{port}"
    print(f"reauth: VNC is at 127.0.0.1:{port}, password {password}", flush=True)
    if sys.platform == "darwin":
        subprocess.run(["open", url])  # Screen Sharing
    else:
        print("reauth: connect a VNC viewer to it.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Finish a sign-in challenge on the deployed scraper over VNC.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("host", nargs="?", help="SSH destination of the server, e.g. root@vps")
    target.add_argument("--local", action="store_true", help="the container runs on this machine")
    parser.add_argument("--container", help="scraper container name (default: find it by label)")
    parser.add_argument("--docker", help='container engine command (default: "docker" over SSH; '
                        'podman or docker with --local)')
    parser.add_argument("--sudo", action="store_true",
                        help="run the engine with sudo (default: only if it gets permission "
                        "denied); asks for the sudo password unless sudo needs none")
    parser.add_argument("--only", nargs="+", metavar="SOURCE",
                        help="scrape just these sources (default: every enabled source)")
    parser.add_argument("-v", "--verbose", action="store_true", help="scraper debug logging")
    args = parser.parse_args()
    scraper_args = (["--only", *args.only] if args.only else []) + (["-v"] if args.verbose else [])

    if args.docker:
        docker = shlex.split(args.docker)
    elif args.local:
        docker = ["podman" if shutil.which("podman") else "docker"]
    else:
        docker = ["docker"]
    if docker[0] == "sudo":  # --docker "sudo docker": sudo needs the handling in use_sudo
        docker = docker[1:]
        args.sudo = True
    remote = Remote(None if args.local else args.host, docker)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    session = None
    try:
        remote.use_sudo(force=args.sudo)
        container = args.container or find_container(remote)
        print(f"reauth: starting a headed scrape in {container}", flush=True)
        # stdin stays open and unused: when this script (or the SSH connection) goes away,
        # login.sh sees EOF and stops the scrape.
        session = subprocess.Popen(
            remote.argv("exec", "-i", "-e", "LIFEAPI_LOGIN_STOP_ON_EOF=1", container,
                        LOGIN_SH, *scraper_args),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)  # Ctrl-C reaches only us; we stop it via stdin below
        session.stdin.write(remote.stdin_prefix.decode())
        session.stdin.flush()
        threading.Thread(target=serve_vnc, args=(remote, container, listener), daemon=True).start()

        password = None
        for line in session.stdout:
            if line.startswith("login.sh: VNC password: "):
                password = line.split(": ", 2)[2].strip()
                continue  # printed by open_viewer
            print(line, end="", flush=True)
            if line.startswith("login.sh: VNC ready") and password:
                open_viewer(port, password)
        status = session.wait()
        print("reauth: done." if status == 0 else
              f"reauth: the scrape exited with {status}; check the output above.")
        return status
    except KeyboardInterrupt:
        print("\nreauth: stopping the session on the server", flush=True)
        return 130
    except subprocess.CalledProcessError as e:
        sys.exit(f"reauth: {shlex.join(e.cmd)} failed:\n{e.stderr.strip()}")
    finally:
        listener.close()
        if session and session.poll() is None:
            session.stdin.close()  # login.sh stops on EOF
            try:
                session.wait(timeout=15)
            except subprocess.TimeoutExpired:
                session.kill()
        remote.close()


if __name__ == "__main__":
    sys.exit(main())
