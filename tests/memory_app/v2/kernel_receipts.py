from backend.memory_app.v2.memory_turn import MemoryTurn


def wire_receipts(records):
    store = MemoryTurn.store_for(records)
    return [store.get(event["data"]["receipt_ref"])
        for row in records.list("v2_memory_turn_keys")
        for event in store.events_after(row.object_id)
        if event["type"] == "model.attempt.terminal"]


def requests(records):
    store = MemoryTurn.store_for(records)
    return [store.get_request(row.object_id) for row in records.list("v2_memory_turn_keys")]
