"""SDK仅处理协议；实际stdio进程统一交原owner绑定和关闭。"""
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

import anyio
from anyio.from_thread import BlockingPortal
from mcp import types
from mcp.shared.message import SessionMessage

from .external_process import ProcessInputChannel, run_process


@asynccontextmanager
async def owned_stdio_client(server, errlog=None, *, timeout=30):
    if type(timeout) not in {int, float} or not 0 < timeout <= 120:
        raise ValueError('external_runner_memory_unavailable')
    incoming_send, incoming = anyio.create_memory_object_stream(8)
    outgoing, outgoing_receive = anyio.create_memory_object_stream(8)
    channel = ProcessInputChannel()
    results = []
    async with incoming_send, incoming, outgoing, outgoing_receive, BlockingPortal() as portal:
        async def deliver(message):
            with anyio.fail_after(.5):
                await incoming_send.send(message)

        def line(value):
            message = types.JSONRPCMessage.model_validate_json(value)
            portal.call(deliver, SessionMessage(message))

        async def consume():
            async for message in outgoing_receive:
                channel.send(message.message.model_dump_json(by_alias=True, exclude_none=True) + '\n')

        async def process():
            try:
                result = await anyio.to_thread.run_sync(partial(run_process,
                    (server.command, *server.args), cwd=Path(server.cwd),
                    environment=server.env, input_text='', input_channel=channel,
                    timeout=timeout, output_limit=262144, on_line=line,
                    secret_values=tuple(value for key, value in server.env.items() if 'KEY' in key)))
                results.append(result)
            finally:
                await incoming_send.aclose()

        async with anyio.create_task_group() as group:
            group.start_soon(process)
            group.start_soon(consume)
            try:
                yield incoming, outgoing
            finally:
                channel.close()
                await outgoing.aclose()
                group.cancel_scope.cancel()
    if len(results) != 1 or results[0].status != 'completed' or results[0].exit_code != 0:
        raise ValueError('external_runner_memory_unavailable')
