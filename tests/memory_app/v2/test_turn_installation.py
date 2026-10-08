"""Recognition composition owns wiring without importing the ASGI entry point."""

import os
from pathlib import Path
import subprocess
import sys
from threading import RLock
from types import SimpleNamespace


def test_turn_installation_registers_remote_and_local_tools_with_shared_authority():
    from backend.memory_app.packet_verification import _now, _verify_packet_current
    from backend.memory_app.turn_installation import install_recognition_turn_capability

    registrations = []
    registry = SimpleNamespace(register=lambda definition, tool: registrations.append((definition, tool)))
    payloads = object()
    state = SimpleNamespace(
        recognition_service=object(), recognition_models=object(),
        recognition_mutation_lock=RLock(), recognition_document_namespace="default",
    )
    routing = install_recognition_turn_capability(registry, payloads, state)

    assert [definition.capability_id for definition, _ in registrations] == [
        "recognition.task.execute", "recognition.task.execute.local",
    ]
    assert registrations[0][1] is registrations[1][1]
    authority = state.recognition_turn_authority
    assert authority.service is state.recognition_service
    assert authority.models is state.recognition_models
    assert authority.payloads is payloads
    assert authority.lock is state.recognition_mutation_lock
    assert authority.document_namespace == "default"
    assert authority.verify_current is _verify_packet_current
    assert authority.now is _now
    assert routing.models is state.recognition_models
    assert routing.payloads is payloads


def test_turn_installation_import_does_not_initialize_an_application(tmp_path):
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"),
        "CHRIPTMAS_APP_ROOT": str(tmp_path / "must-not-be-created"),
    }
    result = subprocess.run(
        [sys.executable, "-c", (
            "import sys; import backend.memory_app.turn_installation; "
            "import backend.memory_app.packet_verification; "
            "assert 'backend.memory_app.app' not in sys.modules; "
            "assert 'backend.api.app' not in sys.modules"
        )], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "must-not-be-created").exists()


def test_recognition_visibility_compatibility_exports_shared_owner():
    from backend.memory_app import document_visibility as compatibility
    from backend.shared import document_visibility as shared

    assert compatibility.LegacyDocumentVisibility is shared.LegacyDocumentVisibility
    assert compatibility.recognition_document_visible is shared.recognition_document_visible
