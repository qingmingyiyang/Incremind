import socket
import sys
from time import monotonic

import httpx
import pytest

from backend.shared.llm.model_transport import (
    ModelSteerInterrupt, ModelTransportTimeout, OwnedAttemptInterruption,
    _OwnedHTTPAttempt, is_provider_closed_witness,
)
from backend.shared.llm.openai_responses import ResponsesCompletion


def test_pending_control_blocks_before_http_and_witness_requires_owned_close():
    control = OwnedAttemptInterruption(turn_id='turn1',invocation_id='answer1',attempt_id='attempt1')
    control.request()
    client = httpx.Client()
    deadline = monotonic()+10
    timeout = ModelTransportTimeout(remaining=lambda: deadline-monotonic(),
        limits={'header_timeout': 2,'idle_timeout': 2},interruption=control)
    attempt = _OwnedHTTPAttempt(client,timeout)
    try:
        with pytest.raises(ModelSteerInterrupt) as before:
            attempt.check()
        assert before.value.identity == ('turn1','answer1','attempt1')
        assert before.value.attempt_generation == 1
        assert before.value.close_witness is None
        attempt.close()
        assert client.is_closed
        with pytest.raises(ModelSteerInterrupt) as after:
            attempt.check()
        assert is_provider_closed_witness(after.value.close_witness)
        assert control.registration is None
    finally:
        attempt.close()


def test_actual_socket_interruption_leaves_independent_connection_open():
    control = OwnedAttemptInterruption(turn_id='turn1',invocation_id='answer1',attempt_id='attempt1')
    sender,receiver = socket.socketpair()
    other_sender,other_receiver = socket.socketpair()
    try:
        receiver.settimeout(1)
        other_receiver.settimeout(1)
        first = control.register(lambda: sender.shutdown(socket.SHUT_RDWR))
        control.unregister(first)
        second = control.register(lambda: sender.shutdown(socket.SHUT_RDWR))
        control.unregister(first)
        assert control.registration[0] is second
        assert control.generation_for(second) == 2
        control.request()
        assert receiver.recv(1) == b''
        other_sender.sendall(b'x')
        assert other_receiver.recv(1) == b'x'
        control.unregister(second)
    finally:
        for connection in (sender,receiver,other_sender,other_receiver):
            connection.close()


def test_native_responses_pending_interrupt_closes_actual_owner_before_witness():
    clients,connections = [],[]
    def client_factory():
        client = httpx.Client(trust_env=False,follow_redirects=False)
        clients.append(client)
        return client
    def audit(event, arguments):
        if event == 'socket.connect':
            connections.append(arguments[1])
    sys.addaudithook(audit)
    control = OwnedAttemptInterruption(turn_id='turn-responses',invocation_id='answer1',attempt_id='attempt1')
    control.request()
    deadline = monotonic()+5
    timeout = ModelTransportTimeout(remaining=lambda:deadline-monotonic(),
        limits={'header_timeout':1,'idle_timeout':1},owned_client_factory=client_factory,interruption=control)
    stream = ResponsesCompletion(api_base='http://127.0.0.1:18111/v1')(
        model='openai/transport-check',messages=[{'role':'user','content':'检查中断'}],
        api_key='unused',timeout=timeout,stream=True)
    with pytest.raises(ModelSteerInterrupt) as interrupted:
        next(stream)
    assert connections == []
    assert clients and all(client.is_closed for client in clients)
    assert is_provider_closed_witness(interrupted.value.close_witness)
    assert interrupted.value.attempt_generation == 1
    assert control.registration is None
