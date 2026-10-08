"""Recognition-owned capability installation for the existing Turn runtime."""

from .packet_verification import _now, _verify_packet_current
from .turn_capability import RecognitionTaskCapability, recognition_task_capability_definition
from .turn_routing import RecognitionModelRoutingSnapshotAuthority
from .turn_task_authority import RecognitionTurnTaskAuthority


def install_recognition_turn_capability(registry, session_store, application_state):
    recognition_authority = RecognitionTurnTaskAuthority(
        service=application_state.recognition_service, models=application_state.recognition_models,
        payloads=session_store, mutation_lock=application_state.recognition_mutation_lock,
        verify_current=_verify_packet_current, now=_now,
        document_namespace=application_state.recognition_document_namespace,
    )
    recognition_task_tool = RecognitionTaskCapability(
        models=application_state.recognition_models, payloads=session_store,
        load_task=recognition_authority.load_task, commit_result=recognition_authority.commit_result,
    )
    registry.register(recognition_task_capability_definition(), recognition_task_tool)
    registry.register(recognition_task_capability_definition(local=True), recognition_task_tool)
    recognition_routing = RecognitionModelRoutingSnapshotAuthority(application_state.recognition_models, session_store)
    application_state.recognition_turn_authority = recognition_authority
    return recognition_routing
