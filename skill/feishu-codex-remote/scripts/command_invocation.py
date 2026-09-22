"""Deterministic shell transport; quoting is not a permission fallback."""
import base64
import shlex


def exec_spec(argv, *, windows):
    if not windows:
        return {"cmd": shlex.join([str(arg) for arg in argv]),
                "shell": "/bin/sh", "login": False, "tty": False}
    # Microsoft documents EncodedCommand as UTF-16LE. Only the transport is
    # encoded: literal quoting preserves Unicode/spaces without cmd expansion.
    literal = lambda value: "'" + str(value).replace("'", "''") + "'"
    program = "$ErrorActionPreference='Stop'; & " + " ".join(map(literal, argv)) + "; exit $LASTEXITCODE"
    encoded = base64.b64encode(program.encode("utf-16le")).decode("ascii")
    return {"cmd": "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand " + encoded,
            "shell": "cmd.exe", "login": False, "tty": False}
