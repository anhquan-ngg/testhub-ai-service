"""Test-only HTTP host for the opt-in NestJS/Python transport contract test."""

import json
import socket

import uvicorn

from tests.test_nestjs_contract import build_contract_app


if __name__ == "__main__":
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen()
    print(json.dumps({"port": port}), flush=True)
    uvicorn.Server(uvicorn.Config(
        build_contract_app(), log_level="error", access_log=False,
    )).run(sockets=[listener])
